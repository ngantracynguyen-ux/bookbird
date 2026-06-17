import contextvars
import json
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_core.tools import tool

from fastapi import FastAPI, Request, Body
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

AVATAR_PATH = os.path.join(os.path.dirname(__file__), "avatar.jpg")
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
TEAM_NAME = os.environ.get("TEAM_NAME", "Chim Báo Bão").strip()
TEAM_BU = os.environ.get("TEAM_BU", "Zalopay").strip()

import rooms as rooms_cfg
import calendar_service as cal
import email_service as mailer
import booking as bk
import memory_store as mem
import contacts as contacts_dir

load_dotenv()

# user hiện tại của request (để gắn organizer & memory cho tool chạy trong agent)
_current_user = contextvars.ContextVar("current_user", default="anon")

# ---------- LLM ----------
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
if not LLM_MODEL or not LLM_BASE_URL or not LLM_API_KEY:
    raise ValueError("Cần cấu hình LLM_MODEL, LLM_BASE_URL, LLM_API_KEY.")

llm = ChatOpenAI(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=60, max_retries=2, temperature=0)

REVIEW_START = "[[BOOKING_REVIEW]]"
REVIEW_END = "[[/BOOKING_REVIEW]]"


def _llm_compose_mail(purpose, content="", room="", date="", start="", end="", attendees=None, language="vi") -> str:
    """Dùng LLM soạn THÂN email mời họp theo mục đích. Trả về text (không kèm Hi/Best regards)."""
    attendees = attendees or []
    lang_name = "tiếng Việt" if (language or "vi").startswith("vi") else "tiếng Anh (English)"
    info = [f"- Mục đích: {purpose or '(chưa rõ)'}"]
    if content:
        info.append(f"- Nội dung/agenda: {content}")
    if date:
        info.append(f"- Thời gian: {date} {start}–{end}")
    if room:
        info.append(f"- Phòng: {room}")
    if attendees:
        info.append(f"- Người tham dự: {', '.join(attendees)}")
    prompt = (
        f"Bạn là trợ lý soạn email mời họp chuyên nghiệp. Viết phần THÂN email mời họp bằng {lang_name}, "
        "lịch sự, rõ ràng, ngắn gọn (2–4 câu).\n" + "\n".join(info) +
        "\n\nYÊU CẦU: KHÔNG viết lời chào kiểu 'Hi ...' và KHÔNG viết 'Best regards'/chữ ký "
        "(hệ thống tự thêm). Chỉ trả về phần thân email, không dùng markdown."
    )
    return _strip_thinking(llm.invoke(prompt).content)


_LANG_NAMES = {
    "en": "tiếng Anh (English)",
    "zh": "tiếng Trung giản thể (简体中文)",
    "ja": "tiếng Nhật (日本語)",
    "vi": "tiếng Việt",
}


def _resolve_lang(target) -> str:
    """Chuẩn hoá mã ngôn ngữ đích từ nhiều cách diễn đạt."""
    t = str(target or "").lower()
    if t.startswith("zh") or "trung" in t or "chinese" in t or "中" in t:
        return "zh"
    if t.startswith("ja") or "nhật" in t or "nhat" in t or "japan" in t or "日" in t:
        return "ja"
    if t.startswith("vi") or "việt" in t or "viet" in t:
        return "vi"
    return "en"


def _llm_translate(text, target="en") -> str:
    """Dịch nội dung email sang ngôn ngữ đích (Anh/Trung/Nhật/Việt)."""
    if not (text or "").strip():
        return ""
    tgt = _LANG_NAMES[_resolve_lang(target)]
    prompt = (
        f"Dịch đoạn nội dung email sau sang {tgt}, giữ văn phong lịch sự, trang trọng. "
        f"Chỉ trả về bản dịch, không giải thích:\n\n{text}"
    )
    return _strip_thinking(llm.invoke(prompt).content)


def build_draft(
    room: str,
    date: str,
    start_time: str,
    end_time: str,
    attendees: list[str],
    purpose: str,
    content: str = "",
    note: str = "",
    online_link: str = "",
    docs_link: str = "",
    num_people: int = 1,
    organizer: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    message: str = "",
    signature: str = "",
) -> tuple[dict | None, str | None]:
    """Validate + kiểm tra trống + tạo draft. Trả về (review_data, error).

    room có thể là 'AUTO' để tự chọn phòng nhỏ nhất còn trống đủ sức chứa.
    """
    attendees = [a.strip() for a in (attendees or []) if a.strip()]
    # RULE: luôn thêm email người đặt (user) vào danh sách người tham dự nếu chưa có
    org = (organizer or "").strip()
    if "@" in org and org.lower() not in [a.lower() for a in attendees]:
        attendees.insert(0, org)
    np = max(int(num_people or 0), len(attendees) or 1)

    try:
        start = bk.parse_dt(date, start_time)
        end = bk.parse_dt(date, end_time)
    except ValueError:
        return None, "Định dạng ngày/giờ không hợp lệ."
    # lịch bận cả ngày 1 LẦN (freebusy batch) → xét phòng cục bộ, không gọi is_available nhiều lần
    busy_map = cal.busy_map_for_day(bk.parse_dt(date, "00:00"), bk.parse_dt(date, "23:59"))

    def _room_free(name):
        return not any(start < b[1] and b[0] < end for b in busy_map.get(name, []))

    # chọn phòng tự động
    if (room or "").strip().upper() == "AUTO":
        chosen = None
        for r in rooms_cfg.rooms_fitting(np):
            errs = bk.validate(r["name"], date, start_time, end_time, np)
            non_cap = [e for e in errs if "chứa" not in e]
            if non_cap:
                return None, "Không thể đặt: " + " ".join(non_cap)
            if not errs and _room_free(r["name"]):
                chosen = r["name"]
                break
        if not chosen:
            return None, f"Không còn phòng trống phù hợp cho {np} người trong khung {date} {start_time}–{end_time}."
        room = chosen

    room_obj = rooms_cfg.get_room(room)
    if not room_obj:
        return None, f"Không tồn tại phòng '{room}'."

    errs = bk.validate(room, date, start_time, end_time, np)
    if errs:
        return None, "Không thể đặt: " + " ".join(errs)

    if not _room_free(room):
        return None, f"Phòng {room_obj['name']} đã BẬN trong khung {date} {start_time}–{end_time}."

    # chống đặt trùng GIỜ của chính người đặt (dù khác phòng) — không cho 1 người họp 2 nơi cùng lúc
    if organizer and cal.busy_for([organizer], start, end):
        return None, (f"Bạn đã có một cuộc họp khác trùng khung {date} {start_time}–{end_time} rồi. "
                      "Không thể đặt trùng giờ — bạn chọn khung giờ khác giúp mình nhé.")

    data = {
        "room": room_obj["name"], "capacity": room_obj["capacity"],
        "date": date, "start_time": start_time, "end_time": end_time,
        "attendees": attendees, "purpose": purpose, "content": content,
        "note": note, "online_link": online_link, "docs_link": docs_link,
        "diagram_url": room_obj.get("diagram_url", ""),
        "organizer": organizer,
        "cc": [c.strip() for c in (cc or []) if c.strip()],
        "bcc": [b.strip() for b in (bcc or []) if b.strip()],
        "message": message, "signature": signature,
    }
    draft_id = bk.create_draft(data)
    return {"draft_id": draft_id, **data}, None


def _scope_range(scope: str):
    """Trả về (start_dt, end_dt) cho day/week/month tính từ hôm nay."""
    n = bk.now()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    scope = (scope or "week").lower().strip()
    if scope == "day":
        end = start + timedelta(days=1)
    elif scope == "month":
        end = start + timedelta(days=31)
    else:
        end = start + timedelta(days=7)
    return start, end


def _pick_room_for(date, start_time, end_time, np, preferred=None):
    """Chọn phòng trống đủ sức chứa cho 1 buổi. Ưu tiên preferred nếu còn trống."""
    try:
        start = bk.parse_dt(date, start_time)
        end = bk.parse_dt(date, end_time)
    except ValueError:
        return None
    fitting = rooms_cfg.rooms_fitting(np)
    order = []
    if preferred:
        po = rooms_cfg.get_room(preferred)
        if po and po["capacity"] >= np:
            order.append(po)
    order += [r for r in fitting if not preferred or r["name"] != preferred]
    for r in order:
        if not bk.validate(r["name"], date, start_time, end_time, np) and cal.is_available(r["name"], start, end):
            return r["name"]
    return None


def build_series(room, date, start_time, end_time, attendees, purpose, content, note,
                 online_link, docs_link, num_people, organizer, pattern, count,
                 cc=None, bcc=None, message="", signature=""):
    """Tạo draft cho lịch định kỳ. Giữ cùng 1 phòng xuyên suốt nếu được;
    buổi nào trùng thì né sang phòng tương đương; buổi không còn phòng → conflict."""
    attendees = [a.strip() for a in (attendees or []) if a.strip()]
    org = (organizer or "").strip()
    if "@" in org and org.lower() not in [a.lower() for a in attendees]:
        attendees.insert(0, org)
    np = max(int(num_people or 0), len(attendees) or 1)
    try:
        dates = bk.generate_occurrences(date, pattern, count)
    except ValueError as e:
        return None, str(e)
    if not dates:
        return None, "Không có buổi nào hợp lệ trong hạn 14 ngày."

    # chọn phòng cố định cho cả chuỗi = phòng trống ở buổi đầu (ưu tiên phòng user chọn)
    fixed = _pick_room_for(dates[0], start_time, end_time, np, None if (room or "AUTO").upper() == "AUTO" else room)
    if not fixed:
        return None, f"Buổi đầu ({dates[0]}) không còn phòng phù hợp cho {np} người."

    occurrences = []
    for d in dates:
        start = bk.parse_dt(d, start_time)
        end = bk.parse_dt(d, end_time)
        if cal.is_available(fixed, start, end):
            occurrences.append({"date": d, "room": fixed, "status": "ok"})
        else:
            alt = _pick_room_for(d, start_time, end_time, np, None)
            if alt:
                occurrences.append({"date": d, "room": alt, "status": "moved"})
            else:
                occurrences.append({"date": d, "room": None, "status": "conflict"})

    data = {
        "is_series": True, "pattern": pattern, "fixed_room": fixed,
        "date": dates[0], "start_time": start_time, "end_time": end_time,
        "attendees": attendees, "purpose": purpose, "content": content, "note": note,
        "online_link": online_link, "docs_link": docs_link, "organizer": organizer,
        "capacity": rooms_cfg.get_room(fixed)["capacity"], "room": fixed,
        "diagram_url": rooms_cfg.get_room(fixed).get("diagram_url", ""),
        "cc": [c.strip() for c in (cc or []) if c.strip()],
        "bcc": [b.strip() for b in (bcc or []) if b.strip()],
        "message": message, "signature": signature,
        "occurrences": occurrences,
    }
    draft_id = bk.create_draft(data)
    return {"draft_id": draft_id, **data}, None


# ---------- Tools ----------
@tool
def current_date() -> str:
    """Trả về ngày giờ hiện tại (giờ Việt Nam) để tính hạn đặt phòng (tối đa 14 ngày tới)."""
    from datetime import timedelta
    n = bk.now()
    last = (n + timedelta(days=rooms_cfg.MAX_ADVANCE_DAYS)).date()
    return f"Hôm nay là {n.strftime('%Y-%m-%d')} ({n.strftime('%H:%M')}). Được đặt phòng đến hết ngày {last.isoformat()}."


@tool
def check_rooms(date: str, start_time: str, end_time: str, num_people: int) -> str:
    """Kiểm tra và gợi ý các phòng họp còn trống thỏa điều kiện.

    Args:
        date: Ngày sử dụng, định dạng YYYY-MM-DD.
        start_time: Giờ bắt đầu HH:MM (24h).
        end_time: Giờ kết thúc HH:MM (24h).
        num_people: Số người tham dự.
    """
    candidates = rooms_cfg.rooms_fitting(num_people)
    if not candidates:
        return f"Không có phòng nào chứa được {num_people} người (lớn nhất là F: 30)."

    try:
        start = bk.parse_dt(date, start_time)
        end = bk.parse_dt(date, end_time)
    except ValueError:
        return "Định dạng ngày/giờ không hợp lệ. Dùng YYYY-MM-DD và HH:MM."
    # lịch bận cả ngày 1 LẦN (freebusy batch) rồi xét cục bộ — thay vì 6 lượt is_available
    busy_map = cal.busy_map_for_day(bk.parse_dt(date, "00:00"), bk.parse_dt(date, "23:59"))

    available, unavailable = [], []
    for room in candidates:
        errs = bk.validate(room["name"], date, start_time, end_time, num_people)
        if errs:
            # lỗi điều kiện chung (giờ/ngày) — báo 1 lần rồi dừng
            non_capacity = [e for e in errs if "sức chứa" not in e and "chứa tối đa" not in e]
            if non_capacity:
                return "Không thể đặt: " + " ".join(non_capacity)
            continue
        free = not any(start < b[1] and b[0] < end for b in busy_map.get(room["name"], []))
        if free:
            available.append(room)
        else:
            unavailable.append(room)

    if not available:
        busy = ", ".join(r["name"] for r in unavailable) or "—"
        return f"Khung {date} {start_time}–{end_time} các phòng phù hợp đều BẬN (busy): {busy}. Hãy thử khung giờ khác."

    lines = [f"Các phòng TRỐNG cho {date} {start_time}–{end_time} ({num_people} người):"]
    for r in available:
        lines.append(f"- Phòng {r['name']} — sức chứa {r['capacity']} ({r['type']})")
    lines.append("Bạn chọn phòng nào? Cho mình thêm: email người tham dự, mục đích, nội dung cuộc họp.")
    return "\n".join(lines)


@tool
def prepare_booking(
    room: str,
    date: str,
    start_time: str,
    end_time: str,
    attendees: list[str],
    purpose: str,
    content: str = "",
    note: str = "",
    online_link: str = "",
    docs_link: str = "",
    num_people: int = 1,
) -> str:
    """Tạo bản nháp đặt phòng để người dùng REVIEW trước khi gửi mail.
    Chỉ gọi khi đã có đủ: phòng, ngày, giờ, email người tham dự, mục đích.

    Args:
        room: Tên phòng (A-F).
        date: YYYY-MM-DD.
        start_time: HH:MM.
        end_time: HH:MM.
        attendees: Danh sách email người tham dự.
        purpose: Mục đích cuộc họp.
        content: Nội dung cuộc họp.
        note: Ghi chú thêm cho nội dung mail.
        online_link: Link tham dự online (nếu có).
        docs_link: Link tài liệu (nếu có).
        num_people: Số người tham dự.
    """
    try:
        payload, error = build_draft(
            room, date, start_time, end_time, attendees, purpose,
            content, note, online_link, docs_link, num_people,
            organizer=_current_user.get(),
        )
    except Exception:  # noqa: BLE001 — dữ liệu không hợp lệ: hỏi lại, KHÔNG báo lỗi hệ thống
        return ("Mình chưa tạo được bản đặt phòng. Bạn kiểm tra giúp mình ngày (YYYY-MM-DD), "
                "giờ bắt đầu/kết thúc và số người nhé — rồi mình thử lại ngay.")
    if error:
        return error + " Hãy chọn phòng/khung giờ khác."
    return (
        "Đây là thông tin đặt phòng, vui lòng review:\n"
        f"{REVIEW_START}{json.dumps(payload, ensure_ascii=False)}{REVIEW_END}"
    )


@tool
def book_recurring(
    pattern: str,
    date: str,
    start_time: str,
    end_time: str,
    attendees: list[str],
    purpose: str,
    count: int = 8,
    room: str = "AUTO",
    num_people: int = 1,
    content: str = "",
) -> str:
    """Đặt phòng cho cuộc họp ĐỊNH KỲ (lặp lại). Giữ cùng phòng xuyên suốt, tự né khi trùng.

    Args:
        pattern: daily (hàng ngày) | weekly (hàng tuần) | biweekly (2 tuần/lần).
        date: Ngày buổi đầu tiên YYYY-MM-DD.
        start_time: HH:MM.
        end_time: HH:MM.
        attendees: Email người tham dự.
        purpose: Mục đích.
        count: Số buổi muốn đặt (mặc định 8, hệ thống tự giới hạn trong 14 ngày).
        room: Phòng cố định, hoặc AUTO để tự chọn.
        num_people: Số người.
        content: Nội dung.
    """
    try:
        payload, error = build_series(
            room, date, start_time, end_time, attendees, purpose, content, "",
            "", "", num_people, _current_user.get(), pattern, count,
        )
    except Exception:  # noqa: BLE001 — dữ liệu không hợp lệ: hỏi lại, KHÔNG báo lỗi hệ thống
        return ("Mình chưa tạo được lịch định kỳ. Bạn kiểm tra giúp mình ngày bắt đầu, giờ, "
                "kiểu lặp (daily/weekly/biweekly) và số buổi nhé.")
    if error:
        return "Không thể đặt lịch định kỳ: " + error
    return (
        "Đây là lịch định kỳ, vui lòng review:\n"
        f"{REVIEW_START}{json.dumps(payload, ensure_ascii=False)}{REVIEW_END}"
    )


@tool
def my_schedule(scope: str = "week") -> str:
    """Xem lịch họp sắp tới của tôi theo ngày/tuần/tháng.

    Args:
        scope: day (hôm nay) | week (tuần này) | month (tháng này).
    """
    start, end = _scope_range(scope)
    items = cal.list_range(start, end, organizer=_current_user.get())
    if not items:
        return f"Bạn không có lịch họp nào trong phạm vi '{scope}'."
    lines = [f"Lịch họp của bạn ({scope}):"]
    for it in items:
        s = it["start"][11:16] if "T" in it["start"] else it["start"]
        d = it["start"][:10]
        lines.append(f"- {d} {s} · Phòng {it['room']} · {it['summary']}")
    return "\n".join(lines)


def _find_user_event(organizer: str, date: str, start_time: str, room: str = "") -> dict | None:
    """Tìm booking của user theo ngày + giờ bắt đầu (lọc phòng nếu có)."""
    try:
        s = bk.parse_dt(date, "00:00")
        e = bk.parse_dt(date, "23:59")
    except ValueError:
        return None
    for it in cal.list_range(s, e, organizer=organizer):
        it_start = it["start"][11:16] if "T" in it["start"] else ""
        if it_start == start_time and (not room or it["room"].upper() == room.upper()):
            return it
    return None


def _others_event_at(date: str, start_time: str, room: str = "") -> dict | None:
    """Cuộc họp tại khung này do NGƯỜI KHÁC đặt (toàn công ty dùng chung lịch phòng)."""
    try:
        s = bk.parse_dt(date, "00:00")
        e = bk.parse_dt(date, "23:59")
    except ValueError:
        return None
    me = _current_user.get()
    for it in cal.list_range(s, e):
        it_start = it["start"][11:16] if "T" in it["start"] else ""
        if it_start == start_time and (not room or it["room"].upper() == room.upper()):
            if (it.get("organizer") or "") != me:
                return it
    return None


@tool
def cancel_booking(date: str, start_time: str, room: str = "") -> str:
    """Huỷ một cuộc họp của tôi theo ngày và giờ bắt đầu.

    Args:
        date: Ngày YYYY-MM-DD.
        start_time: Giờ bắt đầu HH:MM.
        room: (tuỳ chọn) tên phòng nếu có nhiều cuộc cùng giờ.
    """
    user = _current_user.get()
    ev = _find_user_event(user, date, start_time, room)
    if not ev:
        other = _others_event_at(date, start_time, room)
        if other:
            return (f"Khung {date} {start_time} (phòng {other['room']}) là lịch do người khác đặt — "
                    "bạn chỉ huỷ/dời được cuộc họp do chính mình tạo nhé.")
        return f"Không tìm thấy cuộc họp của bạn lúc {date} {start_time}."
    ok, msg = cal.cancel_event(ev["room"], ev["id"], organizer=user)
    if ok:
        return f"Đã huỷ cuộc họp phòng {ev['room']} ngày {date} {start_time}."
    return f"Không huỷ được: {msg}"


@tool
def reschedule_booking(date: str, start_time: str, new_date: str, new_start: str, new_end: str, room: str = "") -> str:
    """Dời một cuộc họp của tôi sang ngày/giờ mới (giữ nguyên phòng & người dự nếu được).

    Args:
        date: Ngày hiện tại của cuộc họp YYYY-MM-DD.
        start_time: Giờ bắt đầu hiện tại HH:MM.
        new_date: Ngày mới YYYY-MM-DD.
        new_start: Giờ bắt đầu mới HH:MM.
        new_end: Giờ kết thúc mới HH:MM.
        room: (tuỳ chọn) phòng của cuộc họp cần dời.
    """
    user = _current_user.get()
    ev = _find_user_event(user, date, start_time, room)
    if not ev:
        other = _others_event_at(date, start_time, room)
        if other:
            return (f"Khung {date} {start_time} (phòng {other['room']}) là lịch do người khác đặt — "
                    "bạn chỉ huỷ/dời được cuộc họp do chính mình tạo nhé.")
        return f"Không tìm thấy cuộc họp của bạn lúc {date} {start_time}."
    ok, msg = _do_reschedule(ev, new_date, new_start, new_end, user)
    return msg


def _do_reschedule(ev: dict, new_date: str, new_start: str, new_end: str, organizer: str) -> tuple[bool, str]:
    rm = ev["room"]
    attendees = ev.get("attendees", [])
    np = len(attendees) or 1
    errs = bk.validate(rm, new_date, new_start, new_end, np)
    if errs:
        return False, "Không thể dời: " + " ".join(errs)
    old = cal.get_event(rm, ev["id"]) or ev
    ok, msg = cal.cancel_event(rm, ev["id"], organizer=organizer)
    if not ok:
        return False, msg
    ns = bk.parse_dt(new_date, new_start)
    ne = bk.parse_dt(new_date, new_end)
    if not cal.is_available(rm, ns, ne):
        # khôi phục booking cũ
        try:
            os_ = datetime.fromisoformat(old["start"]); oe_ = datetime.fromisoformat(old["end"])
            cal.create_event(rm, os_, oe_, old.get("summary", "Cuộc họp"), old.get("description", ""), attendees, organizer=organizer)
        except Exception:  # noqa: BLE001
            pass
        return False, f"Phòng {rm} đã bận ở khung giờ mới. Giữ nguyên lịch cũ."
    cal.create_event(rm, ns, ne, old.get("summary", "Cuộc họp"), old.get("description", ""), attendees, organizer=organizer)
    return True, f"Đã dời cuộc họp phòng {rm} sang {new_date} {new_start}–{new_end}."


@tool
def compose_email(purpose: str, content: str = "", language: str = "vi") -> str:
    """Soạn sẵn nội dung (thân) email mời họp theo mục đích cuộc họp.

    Args:
        purpose: Mục đích cuộc họp.
        content: Nội dung/agenda (nếu có).
        language: 'vi' (tiếng Việt, mặc định) hoặc 'en' (tiếng Anh).
    """
    try:
        return _llm_compose_mail(purpose, content, language=language)
    except Exception:  # noqa: BLE001 — LLM trục trặc tạm thời: degrade sang mẫu, KHÔNG báo lỗi hệ thống
        body = f"Kính mời anh/chị tham dự cuộc họp{(' ' + purpose) if purpose else ''}."
        if content:
            body += f" Nội dung chính: {content}."
        body += " Rất mong anh/chị sắp xếp thời gian tham dự. Trân trọng."
        return body


@tool
def translate_email(text: str, target_language: str = "en") -> str:
    """Dịch nội dung email sang ngôn ngữ khác.

    Args:
        text: Nội dung cần dịch.
        target_language: 'en' (Anh), 'zh' (Trung), 'ja' (Nhật) hoặc 'vi' (Việt). Mặc định 'en'.
    """
    try:
        return _llm_translate(text, target_language)
    except Exception:  # noqa: BLE001 — dịch lỗi tạm thời: trả nguyên văn thay vì báo lỗi hệ thống
        return text


@tool
def suggest_time(date: str, duration_minutes: int = 60, attendees: list[str] | None = None, num_people: int = 1, prefer: str = "") -> str:
    """Gợi ý khung giờ vừa CÓ PHÒNG vừa KHÔNG TRÙNG lịch của người mời và người tham dự.

    Nếu ngày yêu cầu đã kín, tự quét sang các ngày kế (trong hạn 14 ngày).
    Trả về phương án phù hợp nhất + vài lựa chọn khác; nếu không có, gợi ý phòng đủ sức chứa.

    Args:
        date: Ngày YYYY-MM-DD.
        duration_minutes: Thời lượng họp (phút), mặc định 60.
        attendees: Email người tham dự (để tránh trùng lịch của họ).
        num_people: Số người (để lọc phòng đủ chứa).
        prefer: Ưu tiên buổi: 'morning'/'sáng' (trước 12h) hoặc 'afternoon'/'chiều' (từ 13h). Để trống = không ưu tiên.
    """
    pref = str(prefer or "").lower()
    pref = "morning" if ("morning" in pref or "sáng" in pref or "sang" in pref) else ("afternoon" if ("afternoon" in pref or "chiều" in pref or "chieu" in pref) else "")

    def _order(lst):
        if pref == "morning":
            return sorted(lst, key=lambda a: (a[0] >= "12:00", a[0]))
        if pref == "afternoon":
            return sorted(lst, key=lambda a: (a[0] < "13:00", a[0]))
        return lst

    attendees = attendees or []
    people = [_current_user.get()] + [a for a in attendees if a]
    np = max(int(num_people or 0), len(attendees) or 1)
    dur = max(15, int(duration_minutes or 60))
    try:
        base = bk.parse_dt(date, "00:00")
    except ValueError:
        return "Ngày không hợp lệ (định dạng YYYY-MM-DD)."

    fitting = rooms_cfg.rooms_fitting(np)
    if not fitting:
        return f"Không có phòng nào chứa được {np} người (lớn nhất là F: 30)."

    # Lấy dữ liệu mỗi ngày 1 LẦN (1 list_range = 6 calendar) rồi tính cục bộ — thay vì
    # hàng trăm lượt is_available/freebusy mỗi slot → nhanh hơn nhiều ở Google mode.
    _day_cache: dict = {}
    tset = {p.strip().lower() for p in people if p and p.strip()}

    def _day_data(d_str):
        if d_str in _day_cache:
            return _day_cache[d_str]
        ds = bk.parse_dt(d_str, f"{rooms_cfg.OPEN_HOUR:02d}:00")
        de = bk.parse_dt(d_str, f"{rooms_cfg.CLOSE_HOUR:02d}:00")
        room_busy: dict = {}
        ppl_busy: list = []
        per_person: dict = {p: [] for p in tset}
        for it in cal.list_range(ds, de):
            try:
                s = datetime.fromisoformat(it["start"])
                e = datetime.fromisoformat(it["end"])
            except Exception:  # noqa: BLE001
                continue
            room_busy.setdefault(it["room"], []).append((s, e))
            who = {(it.get("organizer") or "").lower()} | {(a or "").lower() for a in it.get("attendees", [])}
            for p in (tset & who):
                ppl_busy.append((s, e))
                per_person[p].append((s, e))
        _day_cache[d_str] = (ds, de, room_busy, ppl_busy, per_person)
        return _day_cache[d_str]

    def _slots(d_str):
        ds, de, room_busy, ppl_busy, _pp = _day_data(d_str)
        res, slot = [], ds
        while slot + timedelta(minutes=dur) <= de:
            s, e = slot, slot + timedelta(minutes=dur)
            if not any(s < b[1] and b[0] < e for b in ppl_busy):
                room = next((r["name"] for r in fitting
                             if not any(s < bb[1] and bb[0] < e for bb in room_busy.get(r["name"], []))), None)
                if room:
                    res.append((s.strftime("%H:%M"), e.strftime("%H:%M"), room))
            slot += timedelta(minutes=30)
        return res

    def _explain(d_str):
        _ds, _de, _rb, _pb, per_person = _day_data(d_str)
        parts = []
        for email, iv in per_person.items():
            if iv:
                nm = email.split("@")[0]
                wins = ", ".join(f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}" for s, e in sorted(iv)[:3])
                parts.append(f"{nm} bận {wins}")
        return ("🧠 Mình đã cân nhắc lịch bận: " + "; ".join(parts[:3]) + ".") if parts else ""

    # 1) thử đúng ngày yêu cầu
    same = _order(_slots(date))
    if same:
        best = same[0]
        out = [f"Phương án phù hợp nhất: **{date} {best[0]}–{best[1]}** · phòng **{best[2]}** "
               f"(đủ {np} chỗ, không trùng lịch của ai)."]
        ex = _explain(date)
        if ex:
            out.append(ex)
        if len(same) > 1:
            out.append("Lựa chọn khác: " + "; ".join(f"{a[0]}–{a[1]} (P.{a[2]})" for a in same[1:4]))
        out.append("Bạn chọn khung nào để mình đặt nhé?")
        return "\n".join(out)

    # 2) ngày yêu cầu kín → quét các ngày tiếp theo trong hạn 14 ngày
    today = bk.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for off in range(1, rooms_cfg.MAX_ADVANCE_DAYS + 1):
        d = base + timedelta(days=off)
        if (d - today).days > rooms_cfg.MAX_ADVANCE_DAYS:
            break
        d_str = d.strftime("%Y-%m-%d")
        nxt = _order(_slots(d_str))
        if nxt:
            wd = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "Chủ nhật"][d.weekday()]
            b = nxt[0]
            out = [f"Ngày {date} đã kín (không còn khe nào đủ phòng & mọi người rảnh).",
                   f"➡️ Gợi ý ngày gần nhất còn chỗ: **{wd} {d_str}, {b[0]}–{b[1]}** · phòng **{b[2]}**."]
            ex = _explain(d_str)
            if ex:
                out.append(ex)
            if len(nxt) > 1:
                out.append("Khung khác cùng ngày: " + "; ".join(f"{a[0]}–{a[1]} (P.{a[2]})" for a in nxt[1:3]))
            out.append("Bạn chọn ngày/khung này nhé?")
            return "\n".join(out)

    rooms_list = ", ".join(f"{r['name']} (sức chứa {r['capacity']})" for r in fitting)
    return (f"Mình quét từ {date} đến hết hạn 14 ngày nhưng chưa thấy khung {dur} phút nào "
            f"mà tất cả đều rảnh và còn phòng. Bạn có thể chọn phòng đủ chứa: {rooms_list}, hoặc giảm số người/thời lượng.")


@tool
def book_like_last() -> str:
    """Gợi ý đặt phòng dựa trên thói quen / lần đặt gần nhất của tôi (smart default)."""
    prefs = mem.preferences(_current_user.get())
    if not prefs:
        return "Mình chưa có lịch sử đặt phòng của bạn. Bạn muốn đặt phòng nào, ngày giờ nào?"
    last = prefs.get("last_booking", {})
    return (
        "Theo thói quen của bạn:\n"
        f"- Phòng hay dùng: {prefs.get('favorite_room')}\n"
        f"- Mục đích thường: {prefs.get('common_purpose')}\n"
        f"- Hay mời: {', '.join(prefs.get('frequent_attendees', [])) or '—'}\n"
        f"- Lần gần nhất: phòng {last.get('room')} {last.get('start_time')}-{last.get('end_time')}, mục đích {last.get('purpose')}.\n"
        "Bạn muốn đặt lại tương tự cho ngày nào?"
    )


SYSTEM_PROMPT = (
    "Bạn là trợ lý đặt phòng họp, thân thiện và chính xác. Hệ thống có 6 phòng: "
    "A,B (4 người), C,D (6 người), E (10 người), F (training, 30 người). "
    "Giờ hoạt động 8:00–18:00, đặt trước tối đa 14 ngày.\n"
    "⚠️ QUY TẮC BẮT BUỘC (không vi phạm): Mọi việc đặt phòng PHẢI thực hiện qua tool — "
    "prepare_booking (đặt 1 buổi) hoặc book_recurring (định kỳ). TUYỆT ĐỐI KHÔNG được tự "
    "viết các câu như 'Đã tạo lịch đặt phòng', 'Thông tin đặt phòng', 'Bạn xác nhận đặt luôn nhé' "
    "khi CHƯA gọi tool và nhận kết quả. KHÔNG bao giờ tự khẳng định phòng còn trống hay đã bận "
    "nếu chưa gọi check_rooms/prepare_booking — vì việc kiểm tra TRÙNG phòng (cùng ngày, trùng/"
    "đè giờ) chỉ xảy ra BÊN TRONG tool. Nếu tool báo phòng bận/trùng giờ, hãy từ chối và đề xuất "
    "khung khác, KHÔNG tự ý tạo xác nhận.\n"
    "⚡ HIỆU SUẤT (giảm độ trễ — RẤT QUAN TRỌNG): gọi CÀNG ÍT tool CÀNG TỐT. Nếu người dùng đã cho đủ "
    "ngày + giờ + số người → GỌI THẲNG prepare_booking(room='AUTO') trong 1 bước, KHÔNG gọi current_date "
    "hay check_rooms trước (prepare_booking đã tự kiểm phòng + tự chọn phòng). Chỉ gọi current_date khi ngày "
    "ở dạng tương đối (hôm nay/mai/thứ X) cần quy đổi; chỉ gọi check_rooms hay suggest_time khi người dùng "
    "muốn XEM danh sách phòng, hoặc khi prepare_booking báo bận và cần tìm khung khác.\n"
    "Quy trình (TỐI GIẢN — ít hỏi nhất có thể):\n"
    "1. Khi người dùng muốn đặt phòng, chỉ cần: ngày, giờ bắt đầu/kết thúc (hoặc thời lượng), số người. "
    "Nếu người dùng chưa nêu ngày/giờ cụ thể, dùng suggest_time để quét phòng trống TỪ NGÀY YÊU CẦU "
    "(mặc định hôm nay) ĐẾN HẾT HẠN 14 NGÀY và lấy khung sớm nhất còn phòng.\n"
    "2. NGAY KHI có phòng trống thoả sức chứa → GỌI prepare_booking với room='AUTO' (hệ thống tự chọn "
    "phòng nhỏ vừa đủ) để tạo bản review LIỀN. TUYỆT ĐỐI KHÔNG hỏi 'bạn chọn phòng nào' và KHÔNG liệt kê "
    "phòng để người dùng chọn — cứ AUTO; người dùng có thể đổi phòng trên thẻ review. KHÔNG bắt buộc hỏi "
    "email người tham dự, mục đích hay nội dung trước khi tạo review — đó là TUỲ CHỌN (có thì điền, không thì để trống).\n"
    "3. Email người tham dự, mục đích, nội dung, link… người dùng có thể bổ sung SAU trên thẻ review trước "
    "khi bấm Xác nhận. Hệ thống MẶC ĐỊNH luôn thêm email của chính người dùng vào danh sách người tham dự.\n"
    "4. Sau khi prepare_booking / book_recurring trả về: KHÔNG cần in lại đoạn [[BOOKING_REVIEW]] "
    "(hệ thống tự hiển thị thẻ xác nhận). Chỉ trả lời 1–2 câu NGẮN GỌN mời người dùng xem thẻ và bấm "
    "Xác nhận. Không tự ý gửi mail — việc gửi do người dùng bấm nút xác nhận.\n"
    "Tính năng nâng cao:\n"
    "- Họp định kỳ (lặp lại hàng ngày/tuần): dùng book_recurring (giữ cùng phòng, tự né khi trùng, đảm bảo có phòng).\n"
    "- Xem lịch sắp tới của user: dùng my_schedule (day/week/month).\n"
    "- Khi user nói 'đặt như mọi khi/lần trước' hoặc mới vào chưa rõ nhu cầu: dùng book_like_last để gợi ý theo thói quen.\n"
    "- Huỷ cuộc họp: dùng cancel_booking (ngày + giờ bắt đầu). Dời cuộc họp: dùng reschedule_booking. "
    "Sau khi dời thành công, LUÔN xác nhận lại RÕ RÀNG khung giờ MỚI (ngày, giờ bắt đầu–kết thúc, phòng) để người dùng thấy ngay.\n"
    "- Soạn giúp nội dung mail mời theo mục đích: dùng compose_email. Dịch nội dung mail sang Anh/Trung/Nhật/Việt: dùng translate_email (target_language en/zh/ja/vi).\n"
    "Khi cần biết ngày hôm nay hoặc kiểm tra hạn 14 ngày, hãy gọi current_date — KHÔNG tự đoán ngày.\n"
    "Gợi ý giờ rảnh tránh trùng lịch (cho cả người mời lẫn người tham dự): dùng suggest_time. "
    "Nếu user nói 'ưu tiên buổi sáng/chiều' thì truyền prefer='morning'/'afternoon'. "
    "QUAN TRỌNG: trình bày lại output của suggest_time TRUNG THỰC — giữ NGUYÊN các khung giờ/phòng và "
    "GIỮ NGUYÊN dòng bắt đầu bằng '🧠' (giải thích lịch bận). TUYỆT ĐỐI không tự bịa thêm khung giờ khác.\n"
    "Xưng hô: luôn tự xưng là 'mình', gọi người dùng là 'bạn', giọng thân thiện, gần gũi.\n"
    "QUAN TRỌNG khi không đặt được phòng: TUYỆT ĐỐI KHÔNG nói 'hệ thống đang lỗi', KHÔNG bảo người dùng "
    "'gọi IT', 'liên hệ hỗ trợ' hay 'thử lại sau vài phút'. Luôn nêu lý do thực tế (phòng đã bận, ngoài giờ "
    "8–18h, quá hạn 14 ngày, vượt sức chứa, hoặc còn thiếu thông tin) và CHỦ ĐỘNG đề xuất phương án thay thế: "
    "đổi giờ, đổi ngày, đổi phòng, hoặc gọi suggest_time để tìm khung trống. Nếu chỉ thiếu thông tin thì hỏi lại đúng phần còn thiếu.\n"
    "Nếu thiếu thông tin thì hỏi lại. Trả lời bằng tiếng Việt, ngắn gọn."
)

# Checkpointer giữ ngữ cảnh hội thoại theo thread.
# Có MEMORY_ID → AgentBaseMemoryEvents (bền vững + chia sẻ giữa nhiều replica).
# Không có → InMemorySaver (chỉ 1 tiến trình, dùng cho local/demo).
checkpointer = None
if os.environ.get("MEMORY_ID", "").strip():
    try:
        from greennode_agent_bridge import AgentBaseMemoryEvents
        checkpointer = AgentBaseMemoryEvents(memory_id=os.environ["MEMORY_ID"].strip())
        print("[checkpointer] dùng AgentBaseMemoryEvents (chia sẻ giữa replica).")
    except Exception as e:  # noqa: BLE001
        print(f"[checkpointer] không dùng được AgentBaseMemoryEvents: {e}")
if checkpointer is None:
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
    except Exception:  # noqa: BLE001
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    print("[checkpointer] dùng InMemorySaver (1 tiến trình).")

agent = create_agent(
    llm,
    tools=[current_date, check_rooms, prepare_booking, book_recurring, my_schedule,
           book_like_last, cancel_booking, reschedule_booking, compose_email, translate_email,
           suggest_time],
    system_prompt=SYSTEM_PROMPT,
    checkpointer=checkpointer,
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _extract_review(messages) -> str | None:
    """Tìm marker review trong các message (kể cả ToolMessage)."""
    for m in reversed(messages):
        text = getattr(m, "content", "")
        if isinstance(text, str) and REVIEW_START in text and REVIEW_END in text:
            seg = text[text.index(REVIEW_START) + len(REVIEW_START): text.index(REVIEW_END)]
            return seg
    return None


def _strip_review(text: str) -> str:
    """Bỏ marker review khỏi văn bản hiển thị (card render riêng)."""
    return re.sub(re.escape(REVIEW_START) + r".*?" + re.escape(REVIEW_END), "", text, flags=re.S).strip()


def _strip_thinking(text: str) -> str:
    """Bỏ phần suy nghĩ nội bộ của model (minimax m2.5 trả về <think>...</think>).
    Trình duyệt hiển thị nội dung trong thẻ lạ này → lộ chain-of-thought (kèm xưng 'tôi')."""
    if not text:
        return text
    # bỏ cặp <think>...</think> đầy đủ
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.S | re.I)
    # phòng khi thiếu thẻ đóng (stream cắt giữa chừng) hoặc thẻ lẻ
    text = re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.S | re.I)
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.I)
    return text.strip()


# Câu chữ cho thấy model "tự khẳng định đã đặt/tạo review" — dùng để phát hiện khi model
# BỊA xác nhận mà KHÔNG gọi tool prepare_booking (→ bỏ qua bước kiểm tra trùng phòng).
_FAKE_BOOKING_RE = re.compile(
    r"(đã tạo (bản|lịch|đơn|yêu cầu)|bản xem trước|bản review|xác nhận đặt|bấm xác nhận|"
    r"đặt phòng thành công|đã giữ phòng|đã đặt phòng|thông tin đặt phòng|bạn xác nhận|"
    r"đặt luôn nhé|xác nhận đặt luôn|muốn mình đặt|xác nhận để (mình|gửi)|"
    r"bấm.{0,8}xác nhận|xác nhận trên giao diện|thông tin đã sẵn sàng|sẵn sàng để (đặt|gửi))",
    re.I,
)


def _looks_like_fake_booking(text: str) -> bool:
    """True nếu model có vẻ đang 'tự xác nhận đặt phòng' bằng lời (cần ép gọi tool).
    Bắt: câu xác nhận, HOẶC liệt kê Phòng+Giờ dạng bullet — mà KHÔNG có review thật."""
    if not text:
        return False
    # bỏ markdown (** _ `) để 'bấm **Xác nhận**' vẫn khớp 'bấm xác nhận'
    norm = re.sub(r"[*_`]", "", text).lower()
    if _FAKE_BOOKING_RE.search(norm):
        return True
    return ("phòng:" in norm and "giờ:" in norm)


def _salvage_booking_from_text(text: str, organizer: str):
    """Khi model 'bịa' xác nhận đặt phòng mà KHÔNG gọi tool, parse lại các chi tiết model
    đã in (Phòng/Ngày/Giờ/email) rồi gọi build_draft → tạo draft THẬT + kiểm tra trùng.
    Trả về (payload|None, error|None). None,None nếu không parse đủ thông tin."""
    if not text:
        return None, None
    t = re.sub(r"[*_`]", "", text)
    mroom = re.search(r"ph[oòù]ng\s*[:\-]?\s*([A-Fa-f])\b", t, re.I)
    date = None
    md = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if md:
        date = f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
    else:
        md = re.search(r"(\d{1,2})[/](\d{1,2})[/](\d{4})", t)
        if md:
            date = f"{md.group(3)}-{int(md.group(2)):02d}-{int(md.group(1)):02d}"
    mt = re.search(r"(\d{1,2})\s*[h:]\s*(\d{2})?\s*[-–đếnto]+\s*(\d{1,2})\s*[h:]\s*(\d{2})?", t, re.I)
    if not (mroom and date and mt):
        return None, None
    start = f"{int(mt.group(1)):02d}:{int(mt.group(2) or 0):02d}"
    end = f"{int(mt.group(3)):02d}:{int(mt.group(4) or 0):02d}"
    emails = re.findall(r"[\w.\-]+@[\w.\-]+\.\w+", t)
    purpose = ""
    mp = re.search(r"m[uụ]c đích\s*[:\-]?\s*([^\n]+)", t, re.I)
    if mp:
        purpose = mp.group(1).strip()[:80]
    content = ""
    mc = re.search(r"n[oộ]i dung\s*[:\-]?\s*([^\n]+)", t, re.I)
    if mc:
        content = mc.group(1).strip()[:200]
    try:
        return build_draft(mroom.group(1).upper(), date, start, end, emails,
                           purpose or "Cuộc họp", content, "", "", "",
                           max(1, len(emails)), organizer=organizer)
    except Exception:  # noqa: BLE001
        return None, None


HTML_UI = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookBird — Đặt phòng họp</title>
<link rel="icon" type="image/jpeg" href="./avatar.jpg">
<link rel="apple-touch-icon" href="./avatar.jpg">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://accounts.google.com/gsi/client" async defer></script>
<script>window.GOOGLE_CLIENT_ID="__GOOGLE_CLIENT_ID__";</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #f5f6fa; display: flex; flex-direction: column; height: 100vh; }
  #header { background: #fff; border-bottom: 1px solid #e8eaed; padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  #header .icon { width: 40px; height: 40px; border-radius: 10px; background: linear-gradient(135deg,#1a73e8,#56ccf2); display:flex; align-items:center; justify-content:center; color:#fff; font-size:20px; }
  #header .title { font-weight: 600; font-size: 15px; color: #1a1a2e; flex: 1; }
  #header button { border:1px solid #1a73e8; color:#1a73e8; background:#fff; padding:7px 14px; border-radius:18px; font-size:13px; cursor:pointer; }
  #header button:hover { background:#eaf1fe; }
  #chat { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 16px; max-width: 820px; width: 100%; margin: 0 auto; }
  .row { display: flex; gap: 10px; align-items: flex-end; }
  .row.user { flex-direction: row-reverse; }
  .av { width: 30px; height: 30px; border-radius: 50%; flex-shrink: 0; background: linear-gradient(135deg,#1a73e8,#56ccf2); display:flex; align-items:center; justify-content:center; color:#fff; font-size:15px; }
  .bubble { max-width: 74%; padding: 11px 15px; border-radius: 16px; font-size: 14.5px; line-height: 1.6; word-break: break-word; }
  .row.ai .bubble { background:#fff; color:#1a1a2e; border-bottom-left-radius:4px; box-shadow:0 1px 4px rgba(0,0,0,.07); }
  .row.user .bubble { background:#1a73e8; color:#fff; border-bottom-right-radius:4px; }
  .bubble table { border-collapse: collapse; margin:6px 0; font-size:13px; }
  .bubble th,.bubble td { border:1px solid #e0e0e0; padding:5px 9px; }
  .card { background:#fff; border:1px solid #d4e2fb; border-radius:14px; padding:16px; max-width:74%; box-shadow:0 1px 6px rgba(26,115,232,.12); }
  .card h3 { font-size:15px; color:#1a73e8; margin-bottom:10px; }
  .card .field { display:flex; gap:8px; font-size:13.5px; padding:4px 0; border-bottom:1px dashed #eee; }
  .card .field .k { color:#888; width:120px; flex-shrink:0; }
  .card .field .v { color:#222; }
  .card .actions { margin-top:14px; display:flex; gap:10px; }
  .card .actions button { padding:9px 18px; border-radius:20px; font-size:14px; cursor:pointer; border:none; }
  .btn-send { background:#1a73e8; color:#fff; }
  .btn-send:hover { background:#1558c0; }
  .btn-cancel { background:#f0f0f0; color:#555; }
  .card.done { border-color:#34a853; }
  .card.done h3 { color:#34a853; }
  #inputArea { background:#fff; border-top:1px solid #e8eaed; padding:14px 24px 10px; }
  #footer { text-align:center; font-size:11px; color:#aaa; margin-top:8px; }
  #footer strong { color:#1a73e8; font-weight:500; }
  #inputWrap { max-width:820px; margin:0 auto; display:flex; gap:10px; align-items:flex-end; }
  #box { flex:1; padding:10px 16px; border:1.5px solid #e0e0e0; border-radius:22px; font-size:14.5px; outline:none; resize:none; max-height:120px; font-family:inherit; }
  #box:focus { border-color:#1a73e8; }
  #send { width:42px; height:42px; flex-shrink:0; background:#1a73e8; color:#fff; border:none; border-radius:50%; cursor:pointer; display:flex; align-items:center; justify-content:center; }
  #send svg { width:18px; height:18px; fill:#fff; }
  .typing .bubble { display:flex; gap:5px; }
  .typing span { width:7px; height:7px; background:#aaa; border-radius:50%; animation:b 1.2s infinite; }
  .typing span:nth-child(2){animation-delay:.2s} .typing span:nth-child(3){animation-delay:.4s}
  @keyframes b {0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
  /* layout */
  #main { flex:1; display:flex; overflow:hidden; }
  #chatcol { flex:1; display:flex; flex-direction:column; min-width:0; }
  #sidebar { width:320px; flex-shrink:0; background:#fff; border-left:1px solid #e8eaed; padding:18px; overflow-y:auto; }
  #sidebar h2 { font-size:15px; color:#1a1a2e; margin-bottom:4px; }
  #sidebar .mode { font-size:11.5px; color:#999; margin-bottom:14px; }
  .ctrl { margin-bottom:10px; }
  .ctrl label { display:block; font-size:12px; color:#888; margin-bottom:4px; }
  .ctrl input, .ctrl select { width:100%; padding:8px 10px; border:1.5px solid #e0e0e0; border-radius:8px; font-size:13.5px; outline:none; font-family:inherit; }
  .ctrl input:focus, .ctrl select:focus { border-color:#1a73e8; }
  .ctrl.two { display:flex; gap:8px; }
  .ctrl.two > div { flex:1; }
  .rcard { border:1px solid #e8eaed; border-radius:10px; padding:11px 13px; margin-bottom:9px; display:flex; align-items:center; justify-content:space-between; }
  .rcard { align-items:flex-start; }
  .rcard .info .name { font-weight:600; font-size:14px; color:#1a1a2e; }
  .rcard .info .cap { font-size:11.5px; color:#999; }
  .rcard .amens { margin-top:5px; display:flex; flex-wrap:wrap; gap:4px; }
  .rcard .amen { font-size:10px; background:#eef4ff; color:#3a6; color:#1a73e8; padding:1px 7px; border-radius:8px; }
  /* heatmap phòng × giờ */
  .hm { margin-top:18px; }
  .hm h4 { font-size:12px; color:#888; margin-bottom:8px; font-weight:500; }
  .hm-hours { display:flex; gap:2px; margin-left:28px; margin-bottom:3px; }
  .hm-hours span { flex:1; text-align:center; font-size:9px; color:#aaa; }
  .hm-row { display:flex; align-items:center; gap:2px; margin-bottom:2px; }
  .hm-room { width:26px; flex-shrink:0; font-size:11.5px; font-weight:600; color:#1a1a2e; }
  .hm-cell { flex:1; height:17px; border-radius:3px; }
  .hm-cell.free { background:#cdebd6; } .hm-cell.busy { background:#f6c6c2; }
  .hm-legend { margin-top:8px; font-size:11px; color:#888; display:flex; align-items:center; gap:5px; }
  .hm-legend i { width:13px; height:13px; border-radius:3px; display:inline-block; }
  /* nhóm người nhận (teams) */
  #teamChips { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
  #teamChips .tchip { font-size:11.5px; background:#eaf1fe; color:#1a73e8; padding:3px 9px; border-radius:12px; cursor:pointer; }
  #teamChips .tsave { font-size:11.5px; background:#fff; border:1px dashed #1a73e8; color:#1a73e8; padding:3px 9px; border-radius:12px; cursor:pointer; }
  .badge { font-size:11.5px; font-weight:600; padding:3px 10px; border-radius:12px; }
  .badge.available { background:#e6f4ea; color:#137333; }
  .badge.busy { background:#fce8e6; color:#c5221f; }
  .badge.unknown { background:#f1f3f4; color:#888; }
  .schtab { flex:1; border:1px solid #e0e0e0; background:#fff; padding:6px; border-radius:8px; font-size:12.5px; cursor:pointer; }
  .schtab.active { background:#1a73e8; color:#fff; border-color:#1a73e8; }
  #demoRow { margin-top:18px; display:flex; align-items:center; gap:6px; font-size:12px; color:#6a4fb0; flex-wrap:wrap; }
  #demoRow input { width:46px; padding:5px; border:1px solid #d8cff0; border-radius:7px; font-size:12.5px; text-align:center; }
  #btnDemo { flex:1; min-width:120px; padding:8px; border:1px dashed #c0b3e8; background:#f6f3ff; color:#6a4fb0; border-radius:9px; font-size:12.5px; cursor:pointer; }
  #btnDemo:hover { background:#efe9ff; }
  #btnDemo:disabled { opacity:.6; cursor:default; }
  .schitem { border-left:3px solid #1a73e8; padding:6px 10px; margin-bottom:7px; background:#f8faff; border-radius:0 8px 8px 0; }
  .schitem .t { font-size:12.5px; font-weight:600; color:#1a1a2e; }
  .schitem .s { font-size:11.5px; color:#888; }
  .schitem .sch-act { margin-top:5px; display:flex; gap:12px; }
  .schitem .sch-act a { font-size:11.5px; color:#1a73e8; cursor:pointer; }
  .schitem .sch-act a:first-child { color:#c5221f; }
  #sidebarClose { display:none; }
  @media (max-width: 720px) { #sidebar { display:none; } #sidebar.open { display:block; position:fixed; right:0; top:0; height:100vh; z-index:20; box-shadow:-2px 0 12px rgba(0,0,0,.15); overflow-y:auto; } #sidebar.open #sidebarClose { display:block; width:100%; margin-bottom:12px; padding:9px; border:none; background:#f0f4ff; color:#1a73e8; border-radius:8px; font-size:13px; font-weight:500; cursor:pointer; } }
  #header .icon { overflow:hidden; background:#5bbfe8; }
  .av { overflow:hidden; background:#5bbfe8; }
  #header .icon img, .av img { width:100%; height:100%; object-fit:cover; object-position:center 30%; }
  #header .subtitle { font-size:12px; color:#4caf50; }
  .mail-preview { margin-top:12px; }
  .mail-preview summary { cursor:pointer; color:#1a73e8; font-size:13px; font-weight:500; outline:none; }
  .mail-preview iframe { width:100%; height:300px; border:1px solid #e0e0e0; border-radius:8px; margin-top:8px; background:#fff; }
  #header .primary { background:#1a73e8; color:#fff; border:none; }
  #header .primary:hover { background:#1558c0; }
  #overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:50; align-items:center; justify-content:center; }
  #overlay.open { display:flex; }
  #modal { background:#fff; width:480px; max-width:94vw; max-height:92vh; overflow-y:auto; border-radius:16px; padding:22px 24px; }
  #modal h2 { font-size:17px; color:#1a73e8; margin-bottom:14px; }
  #modal .fg { margin-bottom:12px; }
  #modal label { display:block; font-size:12.5px; color:#555; margin-bottom:5px; font-weight:500; }
  #modal input, #modal select, #modal textarea { width:100%; padding:9px 11px; border:1.5px solid #e0e0e0; border-radius:9px; font-size:14px; outline:none; font-family:inherit; }
  #modal input:focus, #modal select:focus, #modal textarea:focus { border-color:#1a73e8; }
  #modal .grid2 { display:flex; gap:10px; } #modal .grid2 > div { flex:1; }
  #modal .opt { font-size:12px; color:#1a73e8; cursor:pointer; user-select:none; margin-bottom:10px; display:inline-block; }
  #modal .msg { font-size:13px; color:#c5221f; margin-bottom:10px; min-height:0; }
  #modal .foot { display:flex; gap:10px; justify-content:flex-end; margin-top:6px; }
  #modal .foot button { padding:10px 20px; border-radius:22px; font-size:14px; cursor:pointer; border:none; }
  #modal .ok { background:#1a73e8; color:#fff; } #modal .ok:hover { background:#1558c0; }
  #modal .cancel { background:#f0f0f0; color:#555; }
  #modal .mail-tools { display:flex; gap:8px; margin-top:6px; }
  #modal .mt-btn { flex:1; padding:7px 10px; border:1px solid #d4e2fb; background:#f6faff; color:#1a73e8; border-radius:8px; font-size:12.5px; cursor:pointer; }
  #modal .mt-btn:hover { background:#eaf1fe; }
  #modal .mt-btn:disabled { opacity:.6; cursor:default; }
  #modal .mt-sel { padding:7px 8px; border:1px solid #d4e2fb; border-radius:8px; font-size:12.5px; color:#1a73e8; background:#fff; }
  /* collapsible sections */
  #modal .msec { border:1px solid #e3edfb; border-radius:12px; margin-bottom:12px; }
  #modal .msec-head { display:flex; align-items:center; justify-content:space-between; padding:12px 14px; background:#f6faff; cursor:pointer; user-select:none; font-size:14px; font-weight:600; color:#0d47a1; transition:background .15s; border-radius:11px; }
  #modal .msec-head.open { border-radius:11px 11px 0 0; }
  #modal .msec-head:hover { background:#eef4fc; }
  #modal .msec-head .chev { font-size:13px; color:#7da6dd; transition:transform .2s; }
  #modal .msec-head.open .chev { transform:rotate(180deg); }
  #modal .msec-opt { font-weight:400; color:#9bb8e0; font-size:11px; }
  #modal .msec-body { padding:14px 14px 4px; border-top:1px solid #eef4fc; }
  /* stats dashboard */
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:6px; }
  .stat { background:#f8faff; border:1px solid #eaf0fb; border-radius:10px; padding:10px 12px; text-align:center; }
  .stat .num { font-size:22px; font-weight:600; color:#1a73e8; line-height:1.1; }
  .stat .num.ok { color:#137333; } .stat .num.busy { color:#c5221f; }
  .stat .lbl { font-size:11px; color:#888; margin-top:2px; }
  /* tabs */
  .tabs { display:flex; gap:4px; background:#f1f3f4; border-radius:10px; padding:4px; margin:12px 0; }
  .tabs .tab { flex:1; border:none; background:transparent; padding:7px; border-radius:7px; font-size:13px; font-weight:500; color:#666; cursor:pointer; }
  .tabs .tab.active { background:#fff; color:#1a73e8; box-shadow:0 1px 2px rgba(0,0,0,.08); }
  .schtabs { display:flex; gap:6px; margin-bottom:10px; }
  /* hero / onboarding — nền xanh nhạt, chữ xanh đậm */
  #hero { background:linear-gradient(135deg,#eaf3ff,#f6faff); border:1px solid #d8e8fb; color:#0d3b66; border-radius:14px; padding:13px 16px; box-shadow:0 2px 10px rgba(26,115,232,.08); }
  #hero .hero-title { font-size:16px; font-weight:600; color:#0d47a1; margin-bottom:9px; }
  /* workflow gọn theo hàng ngang */
  #hero .flow-h { display:flex; flex-wrap:wrap; align-items:center; gap:6px 8px; }
  #hero .fstep { display:inline-flex; align-items:center; gap:6px; font-size:12.5px; color:#1a3a5c; background:#fff; border:1px solid #e3edfb; border-radius:16px; padding:4px 10px 4px 5px; }
  #hero .fic { width:22px; height:22px; flex-shrink:0; border-radius:50%; background:#eaf3ff; display:inline-flex; align-items:center; justify-content:center; }
  #hero .fic svg { width:14px; height:14px; }
  #hero .farr { color:#9bb8e0; font-size:15px; }
  /* animation hero: build-in lần lượt + icon phát sáng + mũi tên trôi */
  @keyframes heroIn { from { opacity:0; transform:translateY(10px) scale(.96); } to { opacity:1; transform:none; } }
  @keyframes icGlow { 0%,100% { box-shadow:0 0 0 0 rgba(26,115,232,0); } 50% { box-shadow:0 0 0 5px rgba(26,115,232,.13); } }
  @keyframes arrowFlow { 0%,100% { opacity:.4; transform:translateX(0); } 50% { opacity:1; transform:translateX(3px); } }
  #hero { animation: heroIn .5s ease both; }
  #hero .hero-title { animation: heroIn .5s ease both; }
  #hero .flow-h > span { opacity:0; animation: heroIn .5s ease forwards; }
  #hero .flow-h > span:nth-child(1){ animation-delay:.10s }
  #hero .flow-h > span:nth-child(2){ animation-delay:.20s }
  #hero .flow-h > span:nth-child(3){ animation-delay:.30s }
  #hero .flow-h > span:nth-child(4){ animation-delay:.40s }
  #hero .flow-h > span:nth-child(5){ animation-delay:.50s }
  #hero .flow-h > span:nth-child(6){ animation-delay:.60s }
  #hero .flow-h > span:nth-child(7){ animation-delay:.70s }
  #hero .fic { animation: icGlow 3s ease-in-out infinite; }
  #hero .flow-h > span.farr { opacity:1; animation: arrowFlow 1.8s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) {
    #hero, #hero .hero-title, #hero .flow-h > span, #hero .fic, #hero .farr { animation:none !important; opacity:1 !important; transform:none !important; }
  }
  /* sample chips — ngay trên ô nhập, cuộn ngang */
  .samples { display:flex; gap:8px; overflow-x:auto; padding:10px 24px 0; max-width:820px; width:100%; margin:0 auto; scrollbar-width:thin; }
  .samples::-webkit-scrollbar { height:5px; } .samples::-webkit-scrollbar-thumb { background:#d4e2fb; border-radius:3px; }
  .chip { flex:0 0 auto; background:#fff; border:1px solid #d4e2fb; color:#1a73e8; padding:7px 13px; border-radius:18px; font-size:13px; cursor:pointer; white-space:nowrap; transition:.15s; }
  .chip:hover { background:#eaf1fe; }
  /* nút trả lời dính trong bong bóng agent */
  .row.ai .bubble { position:relative; }
  .bubble-reply { display:flex; align-items:center; justify-content:flex-end; gap:4px; width:100%; margin-top:9px; padding:7px 0 0; border:none; border-top:1px solid #eef0f5; background:none; color:#5b86c4; font-size:12px; font-weight:600; cursor:pointer; font-family:inherit; }
  .bubble-reply:hover { color:#1a73e8; }
  /* trích dẫn trả lời trong bong bóng user */
  .bubble .reply-quote { border-left:3px solid rgba(255,255,255,.55); padding:1px 0 1px 8px; margin-bottom:6px; font-size:12px; line-height:1.45; color:rgba(255,255,255,.92); font-style:italic; }
  /* thanh "đang trả lời" trên ô nhập */
  #replyBar { display:flex; align-items:center; gap:9px; max-width:820px; width:100%; margin:0 auto 8px; padding:8px 12px; background:#eef4fc; border-left:3px solid #1a73e8; border-radius:9px; font-size:12.5px; color:#3a5a80; box-sizing:border-box; }
  #replyBar .rb-ic { color:#1a73e8; font-weight:700; }
  #replyBar .rb-text { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #replyBar .rb-x { background:none; border:none; color:#7da6dd; cursor:pointer; font-size:14px; line-height:1; padding:2px 4px; }
  #replyBar .rb-x:hover { color:#c5221f; }
  /* mascot BookBird (bồ câu đập cánh, đơn giản) ở góc trái */
  #mascot { position:fixed; left:18px; bottom:18px; z-index:80; width:66px; height:66px; cursor:pointer; animation:mascotFloat 3s ease-in-out infinite; }
  #mascot .dove { width:66px; height:66px; display:block; border-radius:50%; filter:drop-shadow(0 6px 16px rgba(26,115,232,.38)); transition:transform .2s; }
  #mascot:hover .dove { transform:scale(1.06); }
  #mascot .wing { transform-box:fill-box; transform-origin:78% 96%; animation:flap .5s ease-in-out infinite; }
  @keyframes flap { 0%,100% { transform:rotate(-8deg); } 50% { transform:rotate(26deg); } }
  #mascot .mascot-shadow { position:absolute; left:50%; bottom:-7px; width:42px; height:9px; margin-left:-21px; border-radius:50%; background:rgba(26,115,232,.22); filter:blur(1px); animation:mascotShadow 3s ease-in-out infinite; }
  #mascot .mascot-bubble { position:absolute; left:76px; bottom:16px; white-space:nowrap; background:#fff; color:#1a3a5c; border:1px solid #d8e8fb; border-radius:14px; padding:8px 12px; font-size:12.5px; box-shadow:0 4px 14px rgba(0,0,0,.12); opacity:0; transform:translateX(-6px); transition:.25s; pointer-events:none; }
  #mascot:hover .mascot-bubble, #mascot.speak .mascot-bubble { opacity:1; transform:translateX(0); }
  #mascot .mascot-bubble::after { content:""; position:absolute; left:-6px; bottom:13px; border:6px solid transparent; border-right-color:#fff; }
  #mascot .mascot-x { position:absolute; top:-5px; right:-5px; width:18px; height:18px; border-radius:50%; border:none; background:#c5d8f0; color:#fff; font-size:10px; line-height:18px; padding:0; cursor:pointer; opacity:0; transition:.2s; z-index:1; }
  #mascot:hover .mascot-x { opacity:1; }
  /* bay lượn nhẹ (thân) + cánh đập + bóng dưới chân phập phồng */
  @keyframes mascotFloat { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-7px); } }
  @keyframes mascotShadow { 0%,100% { transform:scale(1); opacity:.55; } 50% { transform:scale(.7); opacity:.3; } }
  @media (prefers-reduced-motion: reduce) { #mascot, #mascot .wing, #mascot .mascot-shadow { animation:none; } }
  @media (max-width:860px) {
    #mascot { left:12px; bottom:84px; width:52px; height:52px; }
    #mascot .dove { width:52px; height:52px; }
    #mascot .mascot-shadow { width:34px; margin-left:-17px; }
    #mascot .mascot-bubble { left:60px; bottom:8px; white-space:normal; max-width:190px; }
    #mascot .mascot-x { opacity:1; }
  }
  /* autocomplete danh bạ */
  .ac-list { position:absolute; left:0; right:0; top:100%; background:#fff; border:1px solid #d4e2fb; border-radius:8px; box-shadow:0 4px 12px rgba(0,0,0,.12); z-index:60; max-height:190px; overflow-y:auto; margin-top:3px; }
  .ac-item { display:flex; align-items:center; gap:8px; padding:8px 11px; cursor:pointer; font-size:13px; }
  .ac-item:hover { background:#eaf1fe; }
  .ac-name { font-weight:500; color:#1a1a2e; }
  .ac-mail { color:#888; font-size:12px; flex:1; }
  .ac-cnt { background:#eaf1fe; color:#1a73e8; font-size:10.5px; padding:1px 6px; border-radius:8px; }
  .ac-hint { font-size:12px; color:#137333; background:#e6f4ea; border-radius:8px; padding:6px 10px; margin-top:6px; display:none; }
  /* language toggle */
  #langToggle { display:flex; gap:3px; background:#f0f4ff; border-radius:16px; padding:3px; }
  #langToggle button { border:none; background:transparent; padding:5px 11px; border-radius:13px; font-size:12.5px; font-weight:500; color:#666; cursor:pointer; }
  #langToggle button.active { background:#1a73e8; color:#fff; }
  /* login */
  #login { position:fixed; inset:0; background:#f5f6fa; z-index:100; display:flex; align-items:center; justify-content:center; }
  #login .box { background:#fff; border-radius:16px; padding:30px 28px; width:360px; max-width:92vw; box-shadow:0 4px 24px rgba(0,0,0,.1); text-align:center; }
  #login .av { width:56px; height:56px; border-radius:50%; overflow:hidden; margin:0 auto 12px; background:#5bbfe8; }
  #login .av img { width:100%; height:100%; object-fit:cover; object-position:center 30%; }
  #login h2 { font-size:18px; color:#1a1a2e; margin-bottom:4px; }
  #login p { font-size:13px; color:#888; margin-bottom:18px; }
  #login input { width:100%; padding:11px 13px; border:1.5px solid #e0e0e0; border-radius:10px; font-size:14px; margin-bottom:10px; outline:none; }
  #login input:focus { border-color:#1a73e8; }
  #login .enter { width:100%; padding:11px; background:#1a73e8; color:#fff; border:none; border-radius:10px; font-size:15px; font-weight:500; cursor:pointer; }
  #login .enter:hover { background:#1558c0; }
  #login .guest { width:100%; padding:10px; margin-top:8px; background:#fff; color:#1a73e8; border:1.5px solid #1a73e8; border-radius:10px; font-size:14px; font-weight:500; cursor:pointer; }
  #login .guest:hover { background:#eaf1fe; }
  #login .or { font-size:12px; color:#aaa; margin:14px 0 10px; }
  #gbtn { display:flex; justify-content:center; }
  #whoami { font-size:12px; color:#888; }
  #btnLogout { border:none!important; color:#c5221f!important; background:transparent!important; font-size:12px!important; padding:4px 8px!important; cursor:pointer; }
  /* success toast */
  #toast { position:fixed; top:18px; left:50%; transform:translateX(-50%) translateY(-80px); background:#137333; color:#fff; padding:12px 22px; border-radius:24px; font-size:14px; font-weight:500; box-shadow:0 6px 20px rgba(0,0,0,.2); z-index:200; opacity:0; transition:all .35s cubic-bezier(.2,.8,.2,1); pointer-events:none; max-width:90vw; }
  #toast.show { transform:translateX(-50%) translateY(0); opacity:1; }
  #toast.err { background:#c5221f; }
  /* confetti khi đặt phòng thành công */
  .confetti { position:fixed; top:-12px; width:9px; height:13px; z-index:300; border-radius:2px; pointer-events:none; animation: confettiFall 1.9s ease-in forwards; }
  @keyframes confettiFall { to { transform: translateY(106vh) rotate(600deg); opacity:0; } }
  /* mobile */
  @media (max-width:720px){
    #header { flex-wrap:wrap; padding:10px 14px; gap:8px; }
    #header .title { font-size:14px; }
    #btnQuick, #btnStatus { font-size:12px; padding:6px 10px; }
    #chat { padding:16px 14px; } #samples { padding:8px 14px 0; } #inputArea { padding:12px 14px; }
    #hero .feat-grid { grid-template-columns:1fr; }
    #whoami { display:none; }
  }
  /* mic */
  #mic { width:42px; height:42px; flex-shrink:0; background:#f0f4ff; color:#1a73e8; border:none; border-radius:50%; cursor:pointer; display:flex; align-items:center; justify-content:center; }
  #mic svg { width:20px; height:20px; fill:#1a73e8; }
  #mic.rec { background:#fce8e6; animation:pulse 1.2s infinite; }
  #mic.rec svg { fill:#c5221f; }
  @keyframes pulse { 0%,100%{box-shadow:0 0 0 0 rgba(197,34,31,.4)} 50%{box-shadow:0 0 0 6px rgba(197,34,31,0)} }
  /* history */
  .histitem { border:1px solid #eaf0fb; border-radius:10px; padding:9px 12px; margin-bottom:8px; }
  .histitem .top { display:flex; justify-content:space-between; align-items:center; }
  .histitem .rm { font-weight:600; font-size:13.5px; color:#1a1a2e; }
  .histitem .dt { font-size:11.5px; color:#888; }
  .histitem .pp { font-size:12.5px; color:#555; margin-top:3px; }
  .histitem .rep { font-size:10.5px; background:#eaf1fe; color:#1a73e8; padding:1px 7px; border-radius:10px; }
</style>
</head>
<body>
  <div id="login">
    <div class="box">
      <div class="av"><img src="./avatar.jpg" alt="BookBird"></div>
      <h2>BookBird</h2>
      <p>Đăng nhập để đặt phòng & lưu lịch sử cá nhân</p>
      <input type="text" id="loginName" placeholder="Tên của bạn">
      <input type="email" id="loginEmail" placeholder="Email công ty (vd: ban@zalopay.vn)">
      <button class="enter" onclick="emailLogin()">Vào</button>
      <button class="guest" onclick="guestLogin()">Trải nghiệm ngay (khách)</button>
      <div id="googleWrap" style="display:none"><div class="or">— hoặc —</div><div id="gbtn"></div></div>
    </div>
  </div>

  <div id="toast"></div>
  <div id="mascot" onclick="mascotClick()" title="BookBird — bấm để đặt phòng nhanh">
    <div class="mascot-bubble" id="mascotBubble">Cần phòng họp? Bấm mình nhé! 🕊️</div>
    <button class="mascot-x" onclick="dismissMascot(event)" title="Ẩn trợ lý">✕</button>
    <span class="mascot-shadow"></span>
    <svg class="dove" viewBox="0 0 100 100" aria-label="BookBird">
      <circle cx="50" cy="50" r="50" fill="#4d9ed6"/>
      <!-- đuôi chẻ -->
      <path d="M20 50 L42 47 L40 57 Z" fill="#fff"/>
      <path d="M20 58 L42 53 L41 62 Z" fill="#eef5fc"/>
      <!-- thân -->
      <path d="M38 52 Q50 44 64 49 Q74 52 73 57 Q68 64 54 63 Q42 62 38 56 Z" fill="#fff"/>
      <!-- đầu -->
      <circle cx="70" cy="49" r="7.5" fill="#fff"/>
      <!-- mỏ cam -->
      <path d="M77 48 L85 50 L77 52 Z" fill="#ffb733"/>
      <!-- mắt -->
      <circle cx="71" cy="47.5" r="1.5" fill="#173f6b"/>
      <!-- cánh (đập) -->
      <path class="wing" d="M55 55 Q46 34 64 27 Q68 41 65 55 Z" fill="#eaf3ff"/>
    </svg>
  </div>
  <div id="header">
    <div class="icon"><img src="./avatar.jpg" alt="BookBird"></div>
    <div style="flex:1">
      <div class="title">BookBird</div>
      <div class="subtitle" id="subtitle">● Trợ lý đặt phòng họp</div>
    </div>
    <div style="text-align:right;margin-right:4px"><div id="whoami"></div><button id="btnLogout" onclick="logout()">Đăng xuất</button></div>
    <div id="langToggle">
      <button id="lang-vi" class="active" onclick="setLang('vi')">VI</button>
      <button id="lang-en" onclick="setLang('en')">EN</button>
    </div>
    <button class="primary" id="btnQuick" onclick="openModal()">+ Đặt phòng nhanh</button>
    <button id="btnStatus" onclick="document.getElementById('sidebar').classList.toggle('open')">Tình trạng phòng</button>
  </div>

  <div id="overlay">
    <div id="modal">
      <h2>Đặt phòng nhanh</h2>

      <div class="msec">
        <div class="msec-head open" id="head-secBook" onclick="toggleSec('secBook')">
          <span>📅 Đặt phòng họp</span><span class="chev">▾</span>
        </div>
        <div class="msec-body" id="secBook">
          <div class="fg">
            <label>Ngày sử dụng</label>
            <input type="date" id="mDate">
          </div>
          <div class="fg grid2">
            <div><label>Giờ bắt đầu</label><select id="mStart" onchange="updateEndHint()"></select></div>
            <div><label>Thời lượng</label><select id="mDuration" onchange="updateEndHint()"></select></div>
          </div>
          <div id="endHint" style="font-size:12px;color:#1a73e8;margin:-4px 0 10px">→ Kết thúc: --:--</div>
          <div class="fg grid2">
            <div><label>Số người</label><select id="mPeople"></select></div>
            <div><label>Phòng</label><select id="mRoom">
              <option value="AUTO">Tự động gợi ý</option>
              <option value="A">A (4)</option><option value="B">B (4)</option>
              <option value="C">C (6)</option><option value="D">D (6)</option>
              <option value="E">E (10)</option><option value="F">F (30, training)</option>
            </select></div>
          </div>
          <div class="fg grid2">
            <div><label>Lặp lại</label><select id="mPattern" onchange="document.getElementById('countWrap').style.display=this.value==='none'?'none':'block'">
              <option value="none">Không (đặt 1 buổi)</option>
              <option value="daily">Hàng ngày</option>
              <option value="weekly">Hàng tuần</option>
              <option value="biweekly">2 tuần/lần</option>
            </select></div>
            <div id="countWrap" style="display:none"><label>Số buổi</label><input type="number" id="mCount" value="4" min="1" max="14"></div>
          </div>
          <div class="fg">
            <label>Mục đích</label>
            <select id="mPurpose">
              <option>Họp team</option><option>Review</option><option>Phỏng vấn</option>
              <option>Đào tạo</option><option>Họp với khách hàng</option><option>1:1</option>
              <option value="__custom">Khác (tự nhập)...</option>
            </select>
          </div>
          <div class="fg">
            <label>Email người tham dự (gõ để gợi ý)</label>
            <input type="text" id="mAttendees" placeholder="Gõ tên hoặc email..." autocomplete="off">
            <div id="acHint" class="ac-hint"></div>
            <div id="teamChips"></div>
          </div>
        </div>
      </div>

      <div class="msec">
        <div class="msec-head" id="head-secMail" onclick="toggleSec('secMail')">
          <span>✉️ Soạn mail mời <span class="msec-opt">(tuỳ chọn)</span></span><span class="chev">▾</span>
        </div>
        <div class="msec-body" id="secMail" style="display:none">
          <div class="fg">
            <label>Lời nhắn / nội dung mail (để trống = tự soạn)</label>
            <textarea id="mMessage" rows="3" placeholder="Hi team, mời mọi người tham dự cuộc họp..."></textarea>
            <div class="mail-tools">
              <button type="button" class="mt-btn" id="btnCompose" onclick="composeMail()">✨ Soạn tự động</button>
              <select id="transLang" class="mt-sel">
                <option value="en">English</option>
                <option value="zh">中文</option>
                <option value="ja">日本語</option>
                <option value="vi">Tiếng Việt</option>
              </select>
              <button type="button" class="mt-btn" id="btnTranslate" onclick="translateMail()">🌐 Dịch</button>
            </div>
          </div>
          <div class="fg"><label>CC (phân tách bằng dấu phẩy)</label><input type="text" id="mCc" placeholder="quanly@zalopay.vn"></div>
          <div class="fg"><label>BCC</label><input type="text" id="mBcc" placeholder="luutru@zalopay.vn"></div>
          <div class="fg"><label>Nội dung cuộc họp</label><textarea id="mContent" rows="2"></textarea></div>
          <div class="fg"><label>Link tham dự online</label><input type="text" id="mOnline" placeholder="https://meet..."></div>
          <div class="fg"><label>Link tài liệu</label><input type="text" id="mDocs" placeholder="https://docs..."></div>
          <div class="fg"><label>Ghi chú</label><input type="text" id="mNote"></div>
          <div class="fg"><label>Chữ ký cuối mail</label><textarea id="mSignature" rows="3" placeholder="Best regards,&#10;..."></textarea></div>
        </div>
      </div>

      <div class="msg" id="mMsg"></div>
      <div class="foot">
        <button class="cancel" onclick="closeModal()">Huỷ</button>
        <button class="ok" id="mSubmit" onclick="submitBooking()">Tạo & review</button>
      </div>
    </div>
  </div>
  <div id="main">
    <div id="chatcol">
      <div id="chat">
        <div class="row ai"><div class="av"><img src="./avatar.jpg" alt="BookBird"></div>
          <div class="bubble" id="greetBubble"></div>
        </div>
      </div>
      <div id="samples" class="samples"></div>
      <div id="inputArea">
        <div id="replyBar" style="display:none">
          <span class="rb-ic">↩</span>
          <span id="replyBarText" class="rb-text"></span>
          <button class="rb-x" onclick="cancelReply()" title="Huỷ trả lời">✕</button>
        </div>
        <div id="inputWrap">
        <button id="mic" onclick="toggleVoice()" title="Voice"><svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg></button>
        <textarea id="box" rows="1"></textarea>
        <button id="send" onclick="sendMessage()"><svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg></button>
      </div>
      <div id="footer">Track <strong>Chat Agent</strong> · Triển khai trên GreenNode AgentBase · __TEAM_NAME__</div>
      </div>
    </div>
    <div id="sidebar">
      <button id="sidebarClose" onclick="document.getElementById('sidebar').classList.remove('open')">✕ Đóng</button>
      <div class="stats">
        <div class="stat"><div class="num" id="stTotal">–</div><div class="lbl">Tổng phòng</div></div>
        <div class="stat"><div class="num ok" id="stFree">–</div><div class="lbl">Trống</div></div>
        <div class="stat"><div class="num busy" id="stBusy">–</div><div class="lbl">Đang dùng</div></div>
        <div class="stat"><div class="num" id="stWeek">–</div><div class="lbl">Lịch tuần</div></div>
      </div>
      <div class="mode" id="modeLabel">—</div>
      <div class="tabs">
        <button class="tab active" data-tab="rooms" onclick="switchTab('rooms',this)">Phòng</button>
        <button class="tab" data-tab="sched" onclick="switchTab('sched',this)">Lịch</button>
        <button class="tab" data-tab="hist" onclick="switchTab('hist',this)">Lịch sử</button>
      </div>

      <div class="panel" id="panel-rooms">
        <div class="ctrl two">
          <div><label>Ngày</label><input type="date" id="qDate" onchange="refreshRooms()"></div>
          <div><label>Phòng</label><select id="selRoom" onchange="renderRooms()">
            <option value="ALL">Tất cả</option>
            <option value="A">A</option><option value="B">B</option><option value="C">C</option>
            <option value="D">D</option><option value="E">E</option><option value="F">F</option>
          </select></div>
        </div>
        <div class="ctrl two">
          <div><label>Từ</label><input type="time" id="qStart" step="900" onchange="refreshRooms()"></div>
          <div><label>Đến</label><input type="time" id="qEnd" step="900" onchange="refreshRooms()"></div>
        </div>
        <div id="heatmapWrap"></div>
        <div id="roomCards"></div>
        <a id="roomsMore" style="display:none;font-size:12px;color:#1a73e8;cursor:pointer" onclick="toggleRoomsMore()"></a>
      </div>

      <div class="panel" id="panel-sched" style="display:none">
        <div class="schtabs">
          <button class="schtab" data-scope="day" onclick="loadSchedule('day',this)">Ngày</button>
          <button class="schtab" data-scope="week" onclick="loadSchedule('week',this)">Tuần</button>
          <button class="schtab" data-scope="month" onclick="loadSchedule('month',this)">Tháng</button>
        </div>
        <div id="schedList"></div>
      </div>

      <div class="panel" id="panel-hist" style="display:none">
        <div id="histList"></div>
      </div>

      <div id="demoRow">
        <span>Ngày bận:</span><input type="number" id="demoDays" value="1" min="1" max="7">
        <span>Phòng bận:</span><input type="number" id="demoRoomsBusy" value="4" min="1" max="6">
        <button id="btnDemo" onclick="createDemoData()">🎬 Tạo data demo</button>
      </div>
    </div>
  </div>

<script>
  let userId = null, userName = null;
  const sessionId = "sess-" + Math.floor(Math.random()*100000);
  const chat = document.getElementById("chat");
  const box = document.getElementById("box");
  const AV = '<img src="./avatar.jpg" alt="BookBird">';

  box.addEventListener("keydown", e => { if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendMessage(); }});
  box.addEventListener("input", () => { box.style.height="auto"; box.style.height=Math.min(box.scrollHeight,120)+"px"; });

  const REPLY_LABEL = { vi: "↩ Trả lời", en: "↩ Reply" };
  // ---------- trả lời trích dẫn (quote-reply) ----------
  let replyTo = null;
  function quoteSnippet(t){ const s=(t||"").replace(/\s+/g," ").trim(); return s.length>140 ? s.slice(0,140)+"…" : s; }
  function startReply(q){
    replyTo = q;
    document.getElementById("replyBarText").textContent = "“" + q + "”";
    document.getElementById("replyBar").style.display = "flex";
    box.focus();
  }
  function cancelReply(){ replyTo = null; document.getElementById("replyBar").style.display = "none"; }
  function addMsg(text, who, quote) {
    const row = document.createElement("div"); row.className = "row " + who;
    if (who === "ai") { const a=document.createElement("div"); a.className="av"; a.innerHTML=AV; row.appendChild(a); }
    const b = document.createElement("div"); b.className="bubble";
    if (who === "user" && quote) {
      const q = document.createElement("div"); q.className = "reply-quote"; q.textContent = "“" + quote + "”";
      const t = document.createElement("div"); t.textContent = text;
      b.appendChild(q); b.appendChild(t);
    } else if (who === "ai") {
      b.innerHTML = marked.parse(text);
      // nút trả lời dính ngay trong bong bóng agent
      const rep = document.createElement("button");
      rep.type = "button"; rep.className = "bubble-reply";
      rep.textContent = REPLY_LABEL[lang] || REPLY_LABEL.vi;
      const snip = quoteSnippet(text);
      rep.onclick = () => startReply(snip);
      b.appendChild(rep);
    } else {
      b.textContent = text;
    }
    row.appendChild(b); chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
  }

  function fieldRow(k, v) { return v ? `<div class="field"><div class="k">${k}</div><div class="v">${v}</div></div>` : ""; }

  function fireConfetti(){
    if(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const colors=["#1a73e8","#56ccf2","#34a853","#fbbc04","#e03030"];
    for(let i=0;i<70;i++){
      const c=document.createElement("div"); c.className="confetti";
      c.style.left=(Math.random()*100)+"vw";
      c.style.background=colors[i%colors.length];
      c.style.animationDelay=(Math.random()*0.3)+"s";
      c.style.animationDuration=(1.5+Math.random()*0.8)+"s";
      document.body.appendChild(c);
      setTimeout(()=>c.remove(), 2600);
    }
  }

  let _toastT=null;
  function showToast(msg, isErr){
    const t=document.getElementById("toast");
    t.textContent=(isErr?"":"✓ ")+msg; t.className=isErr?"err show":"show";
    clearTimeout(_toastT); _toastT=setTimeout(()=>{ t.className=t.className.replace("show","").trim(); }, 3200);
  }

  const _seenDrafts = new Set();   // chống hiển thị lại thẻ review trùng (bug "luôn xuất hiện")
  function addReviewCard(data) {
    if (data && data.draft_id) {
      if (_seenDrafts.has(data.draft_id)) return;   // đã hiện rồi → bỏ qua
      _seenDrafts.add(data.draft_id);
    }
    const row = document.createElement("div"); row.className="row ai";
    const a=document.createElement("div"); a.className="av"; a.innerHTML=AV; row.appendChild(a);
    const card = document.createElement("div"); card.className="card";
    const links = [data.online_link ? `<a href="${data.online_link}" target="_blank">Online</a>`:"", data.docs_link ? `<a href="${data.docs_link}" target="_blank">Tài liệu</a>`:""].filter(Boolean).join(" | ");
    let timeOrSeries;
    if (data.is_series) {
      const occ = (data.occurrences||[]).map(o => {
        const tag = o.status==="ok" ? `P.${o.room}` : o.status==="moved" ? `P.${o.room} (đổi phòng)` : `⚠ hết phòng`;
        return `<div style="font-size:13px;padding:2px 0">${o.date} · ${tag}</div>`;
      }).join("");
      timeOrSeries = fieldRow("Giờ", data.start_time + "–" + data.end_time) +
        `<div class="field"><div class="k">Các buổi (${(data.occurrences||[]).length})</div><div class="v">${occ}</div></div>`;
    } else {
      timeOrSeries = fieldRow("Thời gian", data.date + ", " + data.start_time + "–" + data.end_time);
    }
    const title = data.is_series ? `Xác nhận lịch định kỳ (${data.pattern}) — phòng ${data.room}` : `Xác nhận đặt phòng ${data.room}`;
    card.innerHTML = `<h3>${title}</h3>
      ${fieldRow("Phòng", data.room + " (sức chứa " + data.capacity + ")")}
      ${timeOrSeries}
      ${fieldRow("Mục đích", data.purpose)}
      ${fieldRow("Nội dung", data.content)}
      ${fieldRow("Người tham dự", (data.attendees||[]).join(", "))}
      ${fieldRow("CC", (data.cc||[]).join(", "))}
      ${fieldRow("BCC", (data.bcc||[]).join(", "))}
      ${fieldRow("Liên kết", links)}
      ${fieldRow("Ghi chú", data.note)}
      <details class="mail-preview">
        <summary>📧 Xem trước nội dung mail</summary>
        <iframe src="./preview?draft_id=${data.draft_id}" title="Bản nháp mail"></iframe>
      </details>
      <div class="actions">
        <button class="btn-send">${data.is_series ? "Xác nhận đặt chuỗi" : "Xác nhận & gửi mail"}</button>
        <button class="btn-cancel">Huỷ</button>
      </div>`;
    row.appendChild(card); chat.appendChild(row); chat.scrollTop = chat.scrollHeight;
    card.querySelector(".btn-send").onclick = () => confirmBooking(data.draft_id, card, data);
    card.querySelector(".btn-cancel").onclick = () => { card.querySelector(".actions").innerHTML = '<em style="color:#888">Đã huỷ.</em>'; };
  }

  async function confirmBooking(draftId, card, data) {
    const actions = card.querySelector(".actions");
    actions.innerHTML = '<em style="color:#888">Đang gửi...</em>';
    try {
      const r = await fetch("./confirm", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({ draft_id: draftId }) });
      const d = await r.json();
      if (d.status === "success") {
        card.classList.add("done");
        card.querySelector("h3").textContent = "✅ Đã đặt phòng & gửi mail";
        actions.innerHTML = `<em style="color:#34a853">${d.message}</em>`;
        showToast("Đặt phòng thành công!");
        fireConfetti();
        mascotSay(mascotMsg("booked"), 4000);
        // nhảy heatmap/tình trạng phòng sang đúng NGÀY vừa đặt để thấy slot vừa chuyển sang "bận"
        if (data && data.date) { const qd=document.getElementById("qDate"); if(qd) qd.value=data.date; }
        refreshAll();
      } else {
        actions.innerHTML = `<em style="color:#c5221f">${d.message}</em>`;
      }
    } catch(e) { actions.innerHTML = `<em style="color:#c5221f">Lỗi: ${e.message}</em>`; }
  }

  function showTyping(){ const r=document.createElement("div"); r.className="row ai typing"; r.id="typing"; r.innerHTML='<div class="av">'+AV+'</div><div class="bubble"><span></span><span></span><span></span></div>'; chat.appendChild(r); chat.scrollTop=chat.scrollHeight; }
  function hideTyping(){ const t=document.getElementById("typing"); if(t) t.remove(); }

  async function sendMessage() {
    const text = box.value.trim(); if(!text) return;
    const quote = replyTo;
    const outbound = quote
      ? (lang === "en"
          ? `(Replying to your earlier message: "${quote}")\n\n${text}`
          : `(Đang trả lời cho câu trước đó của bạn: "${quote}")\n\n${text}`)
      : text;
    addMsg(text, "user", quote); box.value=""; box.style.height="auto"; cancelReply(); showTyping();
    try {
      const r = await fetch("./invocations", { method:"POST", headers:{ "Content-Type":"application/json", "X-GreenNode-AgentBase-User-Id":userId, "X-GreenNode-AgentBase-Session-Id":sessionId }, body: JSON.stringify({ message: LANG_PREFIX[lang] + outbound }) });
      hideTyping();
      const d = await r.json();
      if (d.response) addMsg(d.response, "ai");
      if (d.review) addReviewCard(d.review);
      refreshAll();
    } catch(e) { hideTyping(); addMsg("Lỗi kết nối: " + e.message, "ai"); }
  }

  // ---------- sidebar: tình trạng phòng ----------
  const ROOMS_META = [["A",4,"meeting"],["B",4,"meeting"],["C",6,"meeting"],["D",6,"meeting"],["E",10,"meeting"],["F",30,"training"]];
  let lastRooms = null;
  const pad = n => String(n).padStart(2,"0");

  function initDefaults() {
    const now = new Date();
    document.getElementById("qDate").value = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
    let h = now.getHours();
    if (h < 8) h = 8;
    if (h > 16) h = 16;
    document.getElementById("qStart").value = `${pad(h)}:${pad(now.getMinutes() < 30 ? 0 : 30)}`;
    document.getElementById("qEnd").value = `${pad(h+1)}:${pad(now.getMinutes() < 30 ? 0 : 30)}`;
  }

  let roomsExpanded = false;
  function toggleRoomsMore(){ roomsExpanded = !roomsExpanded; renderRooms(); }
  function renderRooms() {
    const sel = document.getElementById("selRoom").value;
    const wrap = document.getElementById("roomCards");
    const more = document.getElementById("roomsMore");
    const statusOf = {}, amenOf = {};
    if (lastRooms) lastRooms.forEach(r => { statusOf[r.room] = r.status; amenOf[r.room] = r.amenities || []; });
    let list = ROOMS_META.filter(([name]) => sel === "ALL" || sel === name);
    const LIMIT = 5;
    const collapsed = (sel === "ALL" && !roomsExpanded && list.length > LIMIT);
    const shown = collapsed ? list.slice(0, LIMIT) : list;
    wrap.innerHTML = shown.map(([name, cap, type]) => {
        const st = statusOf[name] || "unknown";
        const label = st === "available" ? "Trống" : st === "busy" ? "Bận" : "—";
        const am = (amenOf[name] || []).map(a=>`<span class="amen">${a}</span>`).join("");
        return `<div class="rcard"><div class="info"><div class="name">Phòng ${name}</div><div class="cap">Sức chứa ${cap} · ${type}</div>${am?`<div class="amens">${am}</div>`:""}</div><span class="badge ${st}">${label}</span></div>`;
      }).join("");
    if (more) {
      if (sel === "ALL" && list.length > LIMIT) {
        more.style.display = "block";
        more.textContent = roomsExpanded ? "▲ Thu gọn" : `▼ Xem thêm ${list.length - LIMIT} phòng`;
      } else { more.style.display = "none"; }
    }
  }

  async function refreshRooms() {
    const date = document.getElementById("qDate").value;
    const start = document.getElementById("qStart").value;
    const end = document.getElementById("qEnd").value;
    try {
      const qs = (date && start && end) ? `?date=${date}&start_time=${start}&end_time=${end}` : "";
      const r = await fetch("./rooms" + qs);
      const d = await r.json();
      lastRooms = d.rooms || [];
      document.getElementById("modeLabel").textContent = d.mode === "google" ? "Nguồn: Google Calendar" : "Nguồn: MOCK (bộ nhớ tạm)";
    } catch(e) { lastRooms = null; }
    // Cập nhật stats Trống/Đang dùng theo đúng ngày-giờ đang xem
    if (lastRooms) {
      const busy = lastRooms.filter(r => r.status === "busy").length;
      const free = lastRooms.filter(r => r.status === "available").length;
      document.getElementById("stTotal").textContent = lastRooms.length;
      document.getElementById("stBusy").textContent = busy;
      document.getElementById("stFree").textContent = (start && end) ? free : "–";
    }
    renderRooms();
    loadHeatmap(date);
  }

  async function loadHeatmap(date){
    const wrap=document.getElementById("heatmapWrap"); if(!wrap) return;
    if(!date){ wrap.innerHTML=""; return; }
    try{
      const r=await fetch("./heatmap?date="+date);
      const d=await r.json();
      const hdr=`<div class="hm-hours">`+d.hours.map(h=>`<span>${h}</span>`).join("")+`</div>`;
      const rows=d.rooms.map(rm=>`<div class="hm-row"><span class="hm-room">${rm.room}</span>`+
        rm.slots.map((s,i)=>`<span class="hm-cell ${s?'free':'busy'}" title="${d.hours[i]}:00 — ${s?'trống':'bận'}"></span>`).join("")+
        `</div>`).join("");
      wrap.innerHTML=`<div class="hm"><h4>🗺️ Bản đồ phòng theo giờ · ${date}</h4>${hdr}${rows}`+
        `<div class="hm-legend"><i style="background:#cdebd6"></i>Trống <i style="background:#f6c6c2"></i>Bận</div></div>`;
    }catch(e){ wrap.innerHTML=""; }
  }

  // ---------- modal đặt phòng nhanh ----------
  function fillSelect(id, opts, def){ const s=document.getElementById(id); s.innerHTML=opts.map(o=>`<option ${o===def?'selected':''}>${o}</option>`).join(""); }

  const DUR_OPTS = [["30","30 phút"],["60","1 tiếng"],["90","1 tiếng 30"],["120","2 tiếng"],["150","2 tiếng 30"],["180","3 tiếng"]];
  function addMins(hhmm, mins){
    const [h,m]=hhmm.split(":").map(Number); let t=h*60+m+mins;
    return pad(Math.floor(t/60))+":"+pad(t%60);
  }
  function updateEndHint(){
    const s=(document.getElementById("mStart")||{}).value, d=parseInt((document.getElementById("mDuration")||{}).value)||60;
    const el=document.getElementById("endHint"); if(s&&el) el.textContent="→ Kết thúc: "+addMins(s,d);
  }
  function initModal(){
    const times=[]; for(let h=8;h<=18;h++){ times.push(`${pad(h)}:00`); if(h<18) times.push(`${pad(h)}:30`); }
    fillSelect("mStart", times, "");
    document.getElementById("mDuration").innerHTML = DUR_OPTS.map(o=>`<option value="${o[0]}"${o[0]==="60"?" selected":""}>${o[1]}</option>`).join("");
    const ppl=[]; for(let i=1;i<=30;i++) ppl.push(String(i));
    fillSelect("mPeople", ppl, "2");
    const p=document.getElementById("mPurpose");
    p.addEventListener("change", ()=>{
      if(p.value==="__custom" && !document.getElementById("mPurposeCustom")){
        const inp=document.createElement("input"); inp.id="mPurposeCustom"; inp.placeholder="Nhập mục đích..."; inp.style.marginTop="6px";
        p.parentNode.appendChild(inp);
      }
    });
  }

  async function openModal(){
    const now=new Date(); let h=now.getHours(); if(h<8)h=8; if(h>16)h=16;
    document.getElementById("mDate").value=`${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`;
    document.getElementById("mStart").value=`${pad(h)}:00`;
    document.getElementById("mDuration").value="60";
    updateEndHint();
    document.getElementById("mMsg").textContent="";
    const acH=document.getElementById("acHint"); if(acH) acH.style.display="none";
    document.getElementById("mSignature").value = localStorage.getItem(sigKey()) || defaultSig();
    renderTeams();
    setSec("secBook", true); setSec("secMail", false);
    document.getElementById("overlay").classList.add("open");
    // smart defaults: prefill theo thói quen
    try {
      const r=await fetch("./defaults",{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json(); const p=d.preferences||{};
      if(p.favorite_room) document.getElementById("mRoom").value=p.favorite_room;
      if(p.frequent_attendees && p.frequent_attendees.length && !document.getElementById("mAttendees").value)
        document.getElementById("mAttendees").value=p.frequent_attendees.join(", ");
      if(p.common_purpose){
        const sel=document.getElementById("mPurpose");
        if([...sel.options].some(o=>o.value===p.common_purpose)) sel.value=p.common_purpose;
      }
    } catch(e){}
    // luôn có email của chính user trong danh sách người tham dự (mặc định)
    const ma=document.getElementById("mAttendees");
    if(ma && userId && userId.indexOf("@")>0){
      const list=ma.value.split(",").map(s=>s.trim()).filter(Boolean);
      if(!list.some(x=>x.toLowerCase()===userId.toLowerCase())){ list.unshift(userId); ma.value=list.join(", "); }
    }
  }
  function setSec(id, open){
    const body=document.getElementById(id), head=document.getElementById("head-"+id);
    if(!body||!head) return;
    body.style.display = open ? "block" : "none";
    head.classList.toggle("open", open);
  }
  function toggleSec(id){
    const body=document.getElementById(id);
    setSec(id, body.style.display==="none");
  }
  function closeModal(){ document.getElementById("overlay").classList.remove("open"); }
  // mascot góc trái: bấm để mở đặt phòng nhanh; ✕ để ẩn; lời thoại theo ngữ cảnh
  const MASCOT_MSG = {
    vi: { idle:"Cần phòng họp? Bấm mình nhé! 🕊️", welcome:"Chào bạn! Cần phòng họp cứ bấm mình nha 🕊️", booked:"Đặt phòng xong xuôi! 🎉" },
    en: { idle:"Need a room? Tap me! 🕊️", welcome:"Hi! Tap me whenever you need a room 🕊️", booked:"Room booked! 🎉" },
  };
  let _mascotT=null;
  function mascotMsg(k){ return (MASCOT_MSG[lang]||MASCOT_MSG.vi)[k]; }
  function mascotSay(text, ms){
    const m=document.getElementById("mascot"), b=document.getElementById("mascotBubble");
    if(!m||!b) return;
    b.textContent=text; m.classList.add("speak");
    clearTimeout(_mascotT);
    if(ms!==0) _mascotT=setTimeout(()=>{ m.classList.remove("speak"); b.textContent=mascotMsg("idle"); }, ms||3800);
  }
  function mascotClick(){ if(typeof openModal==="function") openModal(); }
  function dismissMascot(e){ if(e) e.stopPropagation(); const m=document.getElementById("mascot"); if(m) m.style.display="none"; }
  document.getElementById("overlay").addEventListener("click", e=>{ if(e.target.id==="overlay") closeModal(); });

  async function composeMail(){
    const btn=document.getElementById("btnCompose"); const val=id=>(document.getElementById(id)||{}).value||"";
    let purpose=val("mPurpose"); if(purpose==="__custom") purpose=(document.getElementById("mPurposeCustom")||{}).value||"";
    if(!purpose){ showToast("Hãy chọn mục đích trước khi soạn.", true); return; }
    btn.disabled=true; const old=btn.textContent; btn.textContent="Đang soạn...";
    try{
      const r=await fetch("./compose-mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
        purpose, content:val("mContent"), room:val("mRoom")==="AUTO"?"":val("mRoom"),
        date:val("mDate"), start_time:val("mStart"), end_time:addMins(val("mStart"), parseInt(val("mDuration"))||60),
        attendees:val("mAttendees"), language:lang })});
      const d=await r.json();
      if(d.status==="success"){ document.getElementById("mMessage").value=d.body; }
      else showToast(d.message||"Không soạn được.", true);
    }catch(e){ showToast("Lỗi: "+e.message, true); }
    btn.disabled=false; btn.textContent=old;
  }
  async function translateMail(){
    const btn=document.getElementById("btnTranslate"); const ta=document.getElementById("mMessage");
    if(!ta.value.trim()){ showToast("Chưa có nội dung để dịch.", true); return; }
    const target=(document.getElementById("transLang")||{}).value||"en";
    btn.disabled=true; const old=btn.textContent; btn.textContent="Đang dịch...";
    try{
      const r=await fetch("./translate-mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ text:ta.value, target })});
      const d=await r.json();
      if(d.status==="success"){ ta.value=d.text; }
      else showToast(d.message||"Không dịch được.", true);
    }catch(e){ showToast("Lỗi: "+e.message, true); }
    btn.disabled=false; btn.textContent=old;
  }

  async function submitBooking(){
    const btn=document.getElementById("mSubmit"), msg=document.getElementById("mMsg");
    let purpose=document.getElementById("mPurpose").value;
    if(purpose==="__custom") purpose=(document.getElementById("mPurposeCustom")||{}).value||"";
    const val=id=>(document.getElementById(id)||{}).value||"";
    const endTime = addMins(val("mStart"), parseInt(val("mDuration"))||60);
    const body={ date:val("mDate"), start_time:val("mStart"), end_time:endTime,
      num_people:parseInt(val("mPeople"))||1, room:val("mRoom"), purpose,
      attendees:val("mAttendees"), cc:val("mCc"), bcc:val("mBcc"), message:val("mMessage"),
      content:val("mContent"), online_link:val("mOnline"),
      docs_link:val("mDocs"), note:val("mNote"), signature:val("mSignature"),
      pattern:val("mPattern"), count:parseInt(val("mCount"))||4 };
    if(body.signature.trim()) localStorage.setItem(sigKey(), body.signature);
    if(!body.date||!body.start_time||!body.end_time){ msg.textContent="Vui lòng chọn ngày & giờ."; return; }
    // email người tham dự KHÔNG bắt buộc — hệ thống tự thêm email của bạn vào danh sách.
    if(body.start_time>=body.end_time){ msg.textContent="Giờ kết thúc phải sau giờ bắt đầu."; return; }
    btn.disabled=true; btn.textContent="Đang xử lý..."; msg.textContent="";
    try{
      const r=await fetch("./book",{method:"POST",headers:{"Content-Type":"application/json","X-GreenNode-AgentBase-User-Id":userId},body:JSON.stringify(body)});
      const d=await r.json();
      if(d.status==="success"){ closeModal(); addMsg("Đã tạo yêu cầu đặt phòng, vui lòng review:","ai"); addReviewCard(d.review); refreshAll(); }
      else msg.textContent=d.message||"Không tạo được.";
    }catch(e){ msg.textContent="Lỗi: "+e.message; }
    btn.disabled=false; btn.textContent="Tạo & review";
  }

  // ---------- lịch sắp tới ----------
  async function loadSchedule(scope, btn){
    currentScope=scope;
    document.querySelectorAll(".schtab").forEach(b=>b.classList.remove("active"));
    if(btn) btn.classList.add("active");
    else document.querySelector('.schtab[data-scope="'+scope+'"]').classList.add("active");
    const wrap=document.getElementById("schedList");
    try{
      const r=await fetch("./schedule?scope="+scope,{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json();
      if(!d.items || !d.items.length){ wrap.innerHTML='<div style="font-size:12.5px;color:#aaa;padding:4px">Chưa có lịch họp.</div>'; return; }
      wrap.innerHTML=d.items.map(it=>{
        const date=it.start.slice(0,10), time=it.start.includes("T")?it.start.slice(11,16):"";
        const end=it.end&&it.end.includes("T")?it.end.slice(11,16):"";
        return `<div class="schitem">
          <div class="t">${it.summary||"(họp)"}</div>
          <div class="s">${date} ${time}${end?"–"+end:""} · Phòng ${it.room}</div>
          <div class="sch-act">
            <a onclick='cancelItem(${JSON.stringify(it.room)},${JSON.stringify(it.id)})'>Huỷ</a>
            <a onclick='rescheduleItem(${JSON.stringify(it.room)},${JSON.stringify(it.id)},${JSON.stringify(date)})'>Dời</a>
          </div>
        </div>`;
      }).join("");
    }catch(e){ wrap.innerHTML='<div style="font-size:12.5px;color:#c5221f;padding:4px">Lỗi tải lịch.</div>'; }
  }

  function refreshAll(){ refreshRooms(); loadSchedule(currentScope); loadStats(); if(currentTab==="hist") loadHistory(); }
  let currentScope="week";

  async function createDemoData(){
    const btn=document.getElementById("btnDemo"); btn.disabled=true; const old=btn.textContent; btn.textContent="Đang tạo...";
    try{
      const days=parseInt((document.getElementById("demoDays")||{}).value)||1;
      const rooms_busy=parseInt((document.getElementById("demoRoomsBusy")||{}).value)||4;
      const r=await fetch("./demo-stress",{method:"POST",headers:{"Content-Type":"application/json","X-GreenNode-AgentBase-User-Id":userId},body:JSON.stringify({attendees:"an.nguyen@zalopay.vn,bao.tran@zalopay.vn", days, rooms_busy})});
      const d=await r.json();
      if(d.status==="success"){
        showToast("✓ Data demo: "+(d.rooms_busy||0)+"/6 phòng bận + người dự bận 9–10h,14–15h ("+(d.days_full||[]).join(", ")+")");
        // chuyển sidebar sang đúng ngày kín để thấy tất cả phòng Bận
        const qd=document.getElementById("qDate"); if(qd){ qd.value=d.date; }
        const qs=document.getElementById("qStart"), qe=document.getElementById("qEnd");
        if(qs) qs.value="09:00"; if(qe) qe.value="10:00";
        box.value="Gợi ý giờ họp 60 phút cho 8 người ngày "+d.date+", người dự an.nguyen@zalopay.vn, bao.tran@zalopay.vn";
        box.style.height="auto"; box.style.height=Math.min(box.scrollHeight,120)+"px"; box.focus();
        refreshAll();
      } else showToast(d.message||"Lỗi tạo data", true);
    }catch(e){ showToast("Lỗi: "+e.message, true); }
    btn.disabled=false; btn.textContent=old;
  }

  async function cancelItem(room, id){
    if(!confirm("Huỷ cuộc họp này?")) return;
    try{
      const r=await fetch("./cancel",{method:"POST",headers:{"Content-Type":"application/json","X-GreenNode-AgentBase-User-Id":userId},body:JSON.stringify({room,id})});
      const d=await r.json();
      if(d.status!=="success") showToast(d.message||"Không huỷ được.", true);
      else showToast("Đã huỷ cuộc họp.");
      refreshAll();
    }catch(e){ showToast("Lỗi: "+e.message, true); }
  }
  async function rescheduleItem(room, id, oldDate){
    const nd=prompt("Dời sang ngày (YYYY-MM-DD):", oldDate); if(!nd) return;
    const tr=prompt("Khung giờ mới (HH:MM-HH:MM):", "14:00-15:00"); if(!tr) return;
    const m=tr.split("-"); if(m.length!==2){ alert("Định dạng giờ không hợp lệ."); return; }
    try{
      const r=await fetch("./reschedule",{method:"POST",headers:{"Content-Type":"application/json","X-GreenNode-AgentBase-User-Id":userId},body:JSON.stringify({room,id,date:nd.trim(),start_time:m[0].trim(),end_time:m[1].trim()})});
      const d=await r.json();
      showToast(d.message||(d.status==="success"?"Đã dời cuộc họp.":"Không dời được."), d.status!=="success");
      refreshAll();
    }catch(e){ showToast("Lỗi: "+e.message, true); }
  }

  // ---------- stats dashboard ----------
  async function loadStats(){
    // Trống/Đang dùng được tính theo ngày-giờ đang chọn (trong refreshRooms);
    // ở đây chỉ lấy Tổng phòng + số lịch họp tuần này của user.
    try{
      const r=await fetch("./stats",{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json();
      document.getElementById("stTotal").textContent=d.total_rooms;
      document.getElementById("stWeek").textContent=d.my_week;
    }catch(e){}
  }

  // ---------- tabs ----------
  let currentTab="rooms";
  function switchTab(tab, btn){
    currentTab=tab;
    document.querySelectorAll(".tabs .tab").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("panel-rooms").style.display = tab==="rooms"?"block":"none";
    document.getElementById("panel-sched").style.display = tab==="sched"?"block":"none";
    document.getElementById("panel-hist").style.display  = tab==="hist"?"block":"none";
    if(tab==="sched") loadSchedule(currentScope);
    if(tab==="hist") loadHistory();
  }

  // ---------- history ----------
  async function loadHistory(){
    const wrap=document.getElementById("histList");
    try{
      const r=await fetch("./history",{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json();
      if(!d.items || !d.items.length){ wrap.innerHTML='<div style="font-size:12.5px;color:#aaa;padding:4px">Chưa có lịch sử đặt phòng.</div>'; return; }
      wrap.innerHTML=d.items.map(it=>`<div class="histitem">
        <div class="top"><span class="rm">Phòng ${it.room}</span>${it.is_series?'<span class="rep">định kỳ</span>':''}</div>
        <div class="dt">${it.date} · ${it.start_time}–${it.end_time}</div>
        <div class="pp">${it.purpose||""}</div>
      </div>`).join("");
    }catch(e){ wrap.innerHTML='<div style="font-size:12.5px;color:#c5221f;padding:4px">Lỗi tải lịch sử.</div>'; }
  }

  // ---------- sample questions ----------
  function sendSample(btn){ box.value=btn.textContent; sendMessage(); }
  function hideSamples(){ const s=document.getElementById("samples"); if(s) s.style.display="none"; }

  // ---------- voice (Web Speech API) ----------
  let recog=null, recording=false;
  function toggleVoice(){
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if(!SR){ alert("Trình duyệt không hỗ trợ nhập giọng nói. Hãy dùng Chrome/Edge."); return; }
    if(recording){ recog && recog.stop(); return; }
    recog = new SR();
    recog.lang = (lang==="en"?"en-US":"vi-VN"); recog.interimResults = true; recog.continuous = false;
    const mic=document.getElementById("mic");
    let finalTxt="";
    recog.onstart = ()=>{ recording=true; mic.classList.add("rec"); box.placeholder="🎙️ Đang nghe... (nói xong mình tự gửi)"; };
    recog.onerror = ()=>{ recording=false; mic.classList.remove("rec"); box.placeholder=I18N[lang].placeholder; };
    recog.onresult = (e)=>{
      let txt=""; for(let i=0;i<e.results.length;i++){ txt+=e.results[i][0].transcript; if(e.results[i].isFinal) finalTxt=txt; }
      box.value=txt; box.style.height="auto"; box.style.height=Math.min(box.scrollHeight,120)+"px";
    };
    // hands-free: nói xong tự gửi
    recog.onend = ()=>{
      recording=false; mic.classList.remove("rec"); box.placeholder=I18N[lang].placeholder;
      if(box.value.trim()) setTimeout(()=>sendMessage(), 250);
    };
    recog.start();
  }

  // ---------- i18n VI / EN ----------
  const I18N = {
    vi: {
      subtitle: "● Trợ lý đặt phòng họp",
      greetHi: "Chào {name} 👋 ",
      greetBody: "Mình là BookBird - trợ lý book phòng họp! Bạn cần phòng khi nào, mấy người - cứ cho mình biết, còn lại để mình lo. 😉",
      placeholder: "Nhập yêu cầu đặt phòng...",
      quick: "+ Đặt phòng nhanh", status: "Tình trạng phòng",
      tabs: { rooms:"Phòng", sched:"Lịch", hist:"Lịch sử" },
      stats: ["Tổng phòng","Trống","Đang dùng","Lịch tuần"],
      samples: ["Phòng nào trống chiều nay?","Đặt phòng cho 5 người ngày mai 14:00–15:00","Gợi ý giờ rảnh cho 4 người, họp 1 tiếng","Lịch họp tuần này của tôi","Đặt phòng như mọi khi"],
      heroTitle: "Không lo dò lịch, chốt phòng nhẹ tênh!",
      heroSub: "Một luồng liền mạch — từ đặt phòng đến gửi mail mời:",
      flow: [
        ["voice", "Nói hoặc chat nhu cầu", "\"Đặt phòng 5 người chiều mai\""],
        ["spark", "Gợi ý phòng & giữ chỗ", "Đúng sức chứa · chống đặt trùng · họp định kỳ"],
        ["mail", "AI soạn mail mời", "Theo mục đích họp · dịch Anh / Trung / Nhật"],
        ["send", "Gửi mail xác nhận", "Tới người tham dự · CC/BCC · chữ ký riêng"],
      ],
    },
    en: {
      subtitle: "● Meeting room assistant",
      greetHi: "Hi {name} 👋 ",
      greetBody: "I'm BookBird - your meeting-room assistant! Just tell me when you need a room and for how many - I'll handle the rest. 😉",
      placeholder: "Type your booking request...",
      quick: "+ Quick book", status: "Room status",
      tabs: { rooms:"Rooms", sched:"Schedule", hist:"History" },
      stats: ["Total","Free","In use","This week"],
      samples: ["Which rooms are free this afternoon?","Book a room for 5 people tomorrow 14:00–15:00","Suggest a free time for 4 people, 1 hour","My meetings this week","Book my usual room"],
      heroTitle: "Booking made easy — no calendar hunting!",
      heroSub: "One seamless flow — from booking to sending the invite:",
      flow: [
        ["voice", "Say or type your need", "\"Book a room for 5 tomorrow\""],
        ["spark", "Suggest & hold a room", "Right size · no double-booking · recurring"],
        ["mail", "AI drafts the invite", "By meeting purpose · translate EN / 中 / 日"],
        ["send", "Send confirmation email", "To attendees · CC/BCC · custom signature"],
      ],
    },
  };
  const LANG_PREFIX = { vi: "Hãy trả lời bằng tiếng Việt. ", en: "Please respond in English. " };
  let lang = "vi";

  function renderSamples(){
    document.getElementById("samples").innerHTML = I18N[lang].samples.map(s=>`<button class="chip" onclick="sendSample(this)">${s}</button>`).join("");
  }
  function greetName(){
    if(!userId || userId.indexOf("guest")===0 || !userName) return lang==="en" ? "there" : "bạn";
    const parts=userName.trim().split(/\s+/);
    return parts[parts.length-1];  // tên gọi (tiếng Việt: từ cuối)
  }
  function renderGreet(){
    const t=I18N[lang], el=document.getElementById("greetBubble");
    const isReal = userId && userId.indexOf("guest")!==0 && userName;
    const namePart = isReal ? "<strong>"+greetName()+"</strong>" : greetName();
    if(el) el.innerHTML = t.greetHi.replace("{name}", namePart) + t.greetBody;
  }
  function setLang(l){
    lang=l;
    document.getElementById("lang-vi").classList.toggle("active", l==="vi");
    document.getElementById("lang-en").classList.toggle("active", l==="en");
    const t=I18N[l];
    document.getElementById("subtitle").innerHTML=t.subtitle;
    renderGreet();
    document.getElementById("box").placeholder=t.placeholder;
    document.getElementById("btnQuick").textContent=t.quick;
    document.getElementById("btnStatus").textContent=t.status;
    document.querySelectorAll(".tabs .tab").forEach(b=>{ b.textContent=t.tabs[b.dataset.tab]; });
    const lbls=document.querySelectorAll(".stat .lbl");
    t.stats.forEach((s,i)=>{ if(lbls[i]) lbls[i].textContent=s; });
    renderSamples();
    if(document.getElementById("hero")) renderHero();
  }

  // ---------- autocomplete danh bạ người nhận ----------
  function attachAC(inputId, withHint){
    const inp=document.getElementById(inputId);
    if(!inp) return;
    const list=document.createElement("div"); list.className="ac-list"; list.style.display="none";
    inp.parentNode.style.position="relative"; inp.parentNode.appendChild(list);
    let timer=null;
    inp.addEventListener("input", ()=>{
      clearTimeout(timer);
      const frag=inp.value.split(",").pop().trim();
      timer=setTimeout(async ()=>{
        try{
          const r=await fetch("./contacts?q="+encodeURIComponent(frag),{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
          const d=await r.json();
          if(!d.items||!d.items.length){ list.style.display="none"; return; }
          list.innerHTML=d.items.map(c=>`<div class="ac-item" data-email="${c.email}"><span class="ac-name">${c.name}</span><span class="ac-mail">${c.email}</span>${c.count?`<span class="ac-cnt">${c.count}×</span>`:""}</div>`).join("");
          list.style.display="block";
        }catch(e){ list.style.display="none"; }
      }, 160);
    });
    list.addEventListener("mousedown", e=>{
      const it=e.target.closest(".ac-item"); if(!it) return;
      e.preventDefault();
      const email=it.dataset.email;
      const arr=inp.value.split(",").slice(0,-1).map(s=>s.trim()).filter(Boolean);
      arr.push(email);
      inp.value=arr.join(", ")+", ";
      list.style.display="none"; inp.focus();
      if(withHint) showRelated(email);
    });
    inp.addEventListener("blur", ()=>{ setTimeout(()=>list.style.display="none",150); });
  }

  async function showRelated(email){
    const hint=document.getElementById("acHint");
    try{
      const r=await fetch("./contact?email="+encodeURIComponent(email),{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json();
      if(d.meetings && d.meetings.length){
        const last=d.meetings[d.meetings.length-1];
        hint.innerHTML=`💡 ${d.name}: ${d.meetings.length} cuộc họp trước. Gần nhất ${last.date} · phòng ${last.room}${last.purpose?" · "+last.purpose:""}.`;
        hint.style.display="block";
        const pp=document.getElementById("mPurpose");
        if(d.last_purpose && [...pp.options].some(o=>o.value===d.last_purpose)) pp.value=d.last_purpose;
      } else { hint.style.display="none"; }
    }catch(e){ hint.style.display="none"; }
  }

  // ---------- chữ ký mail (theo user) ----------
  function defaultSig(){ return ["Best regards,", userName||"", userId||""].filter(Boolean).join("\n"); }
  function sigKey(){ return "rb_sig_" + (userId||"anon"); }

  // ---------- nhóm người nhận (teams, theo user) ----------
  function teamsKey(){ return "rb_teams_" + (userId||"anon"); }
  function getTeams(){ try{ return JSON.parse(localStorage.getItem(teamsKey())||"{}"); }catch(e){ return {}; } }
  function renderTeams(){
    const wrap=document.getElementById("teamChips"); if(!wrap) return;
    const teams=getTeams();
    let html=Object.keys(teams).map(n=>`<span class="tchip" onclick='applyTeam(${JSON.stringify(n)})'>👥 ${n}</span>`).join("");
    html+=`<span class="tsave" onclick="saveTeam()">+ Lưu nhóm</span>`;
    wrap.innerHTML=html;
  }
  function applyTeam(name){
    const t=getTeams()[name]; if(!t) return;
    const cur=document.getElementById("mAttendees").value.split(",").map(s=>s.trim()).filter(Boolean);
    const merged=[...new Set(cur.concat(t))];
    document.getElementById("mAttendees").value=merged.join(", ")+", ";
  }
  function saveTeam(){
    const emails=document.getElementById("mAttendees").value.split(",").map(s=>s.trim()).filter(Boolean);
    if(!emails.length){ alert("Nhập email người tham dự trước khi lưu nhóm."); return; }
    const name=prompt("Tên nhóm (vd: Team Finance):"); if(!name) return;
    const teams=getTeams(); teams[name.trim()]=emails;
    localStorage.setItem(teamsKey(), JSON.stringify(teams));
    renderTeams();
  }

  // ---------- đăng nhập / định danh ----------
  const HICONS = {
    voice: '<svg viewBox="0 0 24 24" fill="none" stroke="#2f80ed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="11" rx="3" fill="#dbeafe"/><path d="M6 11a6 6 0 0 0 12 0"/><line x1="12" y1="17" x2="12" y2="20.5"/><line x1="8.5" y1="20.5" x2="15.5" y2="20.5"/></svg>',
    repeat: '<svg viewBox="0 0 24 24" fill="none" stroke="#2f80ed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10a5 5 0 0 1 5-5h8"/><path d="M14 2l3 3-3 3"/><path d="M20 14a5 5 0 0 1-5 5H7"/><path d="M10 22l-3-3 3-3"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="#2f80ed"><path d="M12 2.5l1.7 4.6 4.6 1.7-4.6 1.7L12 15l-1.7-4.5L5.7 8.8l4.6-1.7z"/><circle cx="18.5" cy="16.5" r="1.6" fill="#56ccf2"/><circle cx="5.5" cy="17.5" r="1.2" fill="#56ccf2"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="#2f80ed" stroke-width="2"><circle cx="12" cy="12" r="9" fill="#dbeafe"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" stroke-linecap="round"/></svg>',
    mail: '<svg viewBox="0 0 24 24" fill="none" stroke="#2f80ed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5" width="19" height="14" rx="3.5" fill="#dbeafe"/><path d="M3.5 7.5l8.5 6 8.5-6"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="#2f80ed" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3L10.5 13.5" /><path d="M21 3l-6.5 18-4-8-8-4z" fill="#dbeafe"/></svg>',
  };
  function renderHero(){
    const t=I18N[lang];
    let hero=document.getElementById("hero");
    if(!hero){ hero=document.createElement("div"); hero.id="hero"; chat.insertBefore(hero, chat.firstChild); }
    const steps=t.flow.map((s,i)=>
      `<span class="fstep"><span class="fic">${HICONS[s[0]]||""}</span>${s[1]}</span>`+
      (i < t.flow.length-1 ? '<span class="farr">›</span>' : '')
    ).join("");
    hero.innerHTML=`<div class="hero-title">${t.heroTitle}</div><div class="flow-h">${steps}</div>`;
  }
  function startApp(){
    document.getElementById("login").style.display="none";
    document.getElementById("whoami").textContent=userName ? (userName+" · "+userId) : userId;
    renderGreet();
    renderHero();
    refreshRooms(); loadSchedule("week"); loadStats();
    checkUpcoming();
    if(!window._remTimer) window._remTimer=setInterval(checkUpcoming, 120000);  // nhắc mỗi 2 phút
    setTimeout(()=>mascotSay(mascotMsg("welcome"), 4500), 900);  // mascot chào khi mới vào
  }
  // ---------- nhắc lịch họp sắp tới (in-app) ----------
  const _reminded = {};
  const REMIND_BEFORE_MIN = 30;
  async function checkUpcoming(){
    if(!userId) return;
    try{
      const r=await fetch("./schedule?scope=day",{headers:{"X-GreenNode-AgentBase-User-Id":userId}});
      const d=await r.json();
      const now=new Date();
      (d.items||[]).forEach(it=>{
        if(!it.start || !it.start.includes("T")) return;
        const st=new Date(it.start.replace(" ","T"));
        const mins=(st-now)/60000;
        const key=it.id||it.start;
        if(mins>0 && mins<=REMIND_BEFORE_MIN && !_reminded[key]){
          _reminded[key]=1;
          showToast("⏰ Sắp họp: "+(it.summary||"cuộc họp")+" lúc "+it.start.slice(11,16)+" · phòng "+it.room);
        }
      });
    }catch(e){}
  }
  function doLogin(email, name){
    email=(email||"").trim().toLowerCase(); if(!email) return;
    userId=email; userName=(name||"").trim()||email.split("@")[0];
    localStorage.setItem("rb_user", JSON.stringify({email:userId, name:userName}));
    startApp();
  }
  function emailLogin(){
    const email=document.getElementById("loginEmail").value.trim();
    const name=document.getElementById("loginName").value.trim();
    if(!email || email.indexOf("@")<0){ alert("Vui lòng nhập email hợp lệ."); return; }
    doLogin(email, name);
  }
  function logout(){ localStorage.removeItem("rb_user"); location.reload(); }
  async function guestLogin(){
    const gid = localStorage.getItem("rb_guest_id") || ("guest-"+Math.floor(Math.random()*1000000));
    localStorage.setItem("rb_guest_id", gid);
    userId=gid; userName="Khách trải nghiệm";
    localStorage.setItem("rb_user", JSON.stringify({email:userId, name:userName}));
    try{ await fetch("./demo-seed",{method:"POST",headers:{"X-GreenNode-AgentBase-User-Id":userId}}); }catch(e){}
    startApp();
  }

  async function onGoogleCredential(resp){
    try{
      const r=await fetch("./auth/google",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({credential:resp.credential})});
      const d=await r.json();
      if(d.status==="success" && d.email) doLogin(d.email, d.name);
      else alert(d.message||"Đăng nhập Google thất bại.");
    }catch(e){ alert("Lỗi đăng nhập Google: "+e.message); }
  }
  function initGoogleLogin(){
    const cid=window.GOOGLE_CLIENT_ID;
    if(!cid || cid.indexOf("__GOOGLE")===0 || !window.google || !google.accounts) return;
    document.getElementById("googleWrap").style.display="block";
    google.accounts.id.initialize({client_id:cid, callback:onGoogleCredential});
    google.accounts.id.renderButton(document.getElementById("gbtn"), {theme:"outline", size:"large", width:300, text:"signin_with"});
  }

  // UI setup (không cần đăng nhập)
  initModal();
  initDefaults();
  attachAC("mAttendees", true);
  attachAC("mCc", false);
  setLang("vi");

  // khôi phục phiên đăng nhập hoặc hiện màn hình login
  (function(){
    const saved=localStorage.getItem("rb_user");
    if(saved){ try{ const u=JSON.parse(saved); userId=u.email; userName=u.name; startApp(); return; }catch(e){} }
    // chưa đăng nhập → thử bật nút Google (đợi script GIS tải)
    let tries=0; const t=setInterval(()=>{ initGoogleLogin(); if((window.google&&google.accounts)||++tries>20) clearInterval(t); }, 300);
  })();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return (HTML_UI
            .replace("__GOOGLE_CLIENT_ID__", GOOGLE_OAUTH_CLIENT_ID)
            .replace("__TEAM_NAME__", TEAM_NAME)
            .replace("__TEAM_BU__", TEAM_BU))


@app.get("/avatar.jpg")
def serve_avatar():
    return FileResponse(AVATAR_PATH, media_type="image/jpeg")


@app.post("/auth/google")
def auth_google(payload: dict = Body(default={})):
    """Xác thực Google ID token → trả về email + tên đã verify."""
    token = payload.get("credential", "")
    if not token or not GOOGLE_OAUTH_CLIENT_ID:
        return {"status": "error", "message": "Google login chưa được cấu hình."}
    try:
        from google.oauth2 import id_token as gid
        from google.auth.transport import requests as greq
        info = gid.verify_oauth2_token(token, greq.Request(), GOOGLE_OAUTH_CLIENT_ID)
        return {"status": "success", "email": info.get("email", ""), "name": info.get("name", info.get("email", ""))}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Xác thực thất bại: {e}"}


@app.get("/preview", response_class=HTMLResponse)
def preview_email(draft_id: str = ""):
    """Trả về HTML bản nháp mail cho draft (để xem trước trong UI)."""
    data = bk.get_draft(draft_id)
    if not data:
        return HTMLResponse("<p style='font-family:sans-serif;color:#888;padding:12px'>Draft không tồn tại.</p>")
    return HTMLResponse(mailer.build_booking_html(data))


@app.get("/health")
def health():
    return {"status": "HEALTHY", "calendar_mode": cal.mode()}


@app.get("/rooms")
def rooms_status(date: str = "", start_time: str = "", end_time: str = ""):
    """Card tình trạng phòng theo khung giờ query."""
    out = []
    avail = {}
    if date and start_time and end_time:
        try:
            s = bk.parse_dt(date, start_time)
            e = bk.parse_dt(date, end_time)
            # 1 freebusy batch cho cả ngày rồi xét cục bộ (thay vì 6 lượt is_available)
            bm = cal.busy_map_for_day(bk.parse_dt(date, "00:00"), bk.parse_dt(date, "23:59"))
            avail = {rid: not any(s < b[1] and b[0] < e for b in bm.get(rid, [])) for rid in rooms_cfg.ROOMS}
        except ValueError:
            avail = {}
    for rid, r in rooms_cfg.ROOMS.items():
        out.append({
            "room": r["name"], "capacity": r["capacity"], "type": r["type"],
            "amenities": r.get("amenities", []),
            "status": ("busy" if avail.get(rid) is False else "available") if avail else "unknown",
        })
    return {"mode": cal.mode(), "rooms": out}


@app.post("/book")
def book(request: Request, payload: dict = Body(default={})):
    """Tạo draft từ form đặt phòng nhanh (không qua LLM). Hỗ trợ lặp định kỳ."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")

    def _split(v):
        if isinstance(v, str):
            return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
        return v or []

    attendees = _split(payload.get("attendees", []))
    cc = _split(payload.get("cc", []))
    bcc = _split(payload.get("bcc", []))
    message = payload.get("message", "")
    signature = payload.get("signature", "")
    pattern = (payload.get("pattern") or "").lower().strip()

    if pattern and pattern != "none":
        review, error = build_series(
            room=payload.get("room", "AUTO"), date=payload.get("date", ""),
            start_time=payload.get("start_time", ""), end_time=payload.get("end_time", ""),
            attendees=attendees, purpose=payload.get("purpose", ""),
            content=payload.get("content", ""), note=payload.get("note", ""),
            online_link=payload.get("online_link", ""), docs_link=payload.get("docs_link", ""),
            num_people=payload.get("num_people", 1), organizer=user_id,
            pattern=pattern, count=int(payload.get("count", 8) or 8),
            cc=cc, bcc=bcc, message=message, signature=signature,
        )
    else:
        review, error = build_draft(
            room=payload.get("room", "AUTO"), date=payload.get("date", ""),
            start_time=payload.get("start_time", ""), end_time=payload.get("end_time", ""),
            attendees=attendees, purpose=payload.get("purpose", ""),
            content=payload.get("content", ""), note=payload.get("note", ""),
            online_link=payload.get("online_link", ""), docs_link=payload.get("docs_link", ""),
            num_people=payload.get("num_people", 1), organizer=user_id,
            cc=cc, bcc=bcc, message=message, signature=signature,
        )
    if error:
        return {"status": "error", "message": error}
    return {"status": "success", "review": review}


def _commit_one(room, date, start_time, end_time, data) -> bool:
    """Đặt 1 buổi (nguyên tử chống trùng). Trả về True nếu thành công."""
    start = bk.parse_dt(date, start_time)
    end = bk.parse_dt(date, end_time)
    organizer = data.get("organizer", "")
    # chống trùng giờ của chính người đặt (kiểm lại lúc commit — phòng khi lịch đổi giữa review và xác nhận)
    if organizer and cal.busy_for([organizer], start, end):
        return False
    summary = f"[{room}] {data.get('purpose', 'Cuộc họp')}"
    ev, err = cal.book_if_free(room, start, end, summary, data.get("content", ""),
                               data.get("attendees", []), organizer=organizer)
    if err:
        return False
    rec = {**data, "room": room, "date": date, "start_time": start_time, "end_time": end_time}
    if data.get("organizer"):
        mem.save_booking(data["organizer"], rec)
    # học người nhận vào danh bạ
    for e in data.get("attendees", []) + data.get("cc", []) + data.get("bcc", []):
        contacts_dir.learn(e)
    return True


def _seed_demo(user_id: str) -> int:
    """Tạo sẵn vài booking mẫu cho khách trải nghiệm (chỉ khi chưa có dữ liệu)."""
    if mem.history(user_id):
        return 0

    def dd(offset):
        return (bk.now() + timedelta(days=offset)).strftime("%Y-%m-%d")

    mine = [
        {"room": "C", "date": dd(1), "s": "10:00", "e": "11:00", "purpose": "Họp team Finance",
         "att": ["an.nguyen@zalopay.vn", "bao.tran@zalopay.vn"]},
        {"room": "E", "date": dd(2), "s": "14:00", "e": "15:30", "purpose": "Đào tạo nghiệp vụ",
         "att": ["chi.le@zalopay.vn", "dung.pham@zalopay.vn", "giang.vo@zalopay.vn"]},
        {"room": "A", "date": dd(3), "s": "09:30", "e": "10:00", "purpose": "1:1 review",
         "att": ["khoa.bui@zalopay.vn"]},
    ]
    others = [
        {"room": "D", "date": dd(1), "s": "10:00", "e": "11:00", "purpose": "Daily Ops",
         "org": "team.ops@zalopay.vn", "att": ["team.ops@zalopay.vn"]},
        {"room": "B", "date": dd(1), "s": "14:00", "e": "15:00", "purpose": "Sprint planning",
         "org": "team.dev@zalopay.vn", "att": ["team.dev@zalopay.vn"]},
    ]
    n = 0
    for s in mine:
        data = {"purpose": s["purpose"], "content": "", "note": "", "online_link": "", "docs_link": "",
                "attendees": s["att"], "cc": [], "bcc": [], "organizer": user_id,
                "capacity": rooms_cfg.get_room(s["room"])["capacity"]}
        if _commit_one(s["room"], s["date"], s["s"], s["e"], data):
            n += 1
    for s in others:
        st = bk.parse_dt(s["date"], s["s"]); en = bk.parse_dt(s["date"], s["e"])
        cal.book_if_free(s["room"], st, en, f"[{s['room']}] {s['purpose']}", "", s["att"], organizer=s["org"])
    return n


@app.get("/heatmap")
def heatmap(date: str = ""):
    """Bản đồ nhiệt: mỗi phòng × từng giờ (8–18) trong ngày → trống/bận.
    Lấy lịch bận cả ngày 1 LẦN (freebusy batch) rồi tính cục bộ → nhanh, không timeout ở Google mode."""
    hours = list(range(rooms_cfg.OPEN_HOUR, rooms_cfg.CLOSE_HOUR))
    busy = {}
    if date:
        try:
            day_start = bk.parse_dt(date, "00:00")
            busy = cal.busy_map_for_day(day_start, day_start + timedelta(days=1))
        except Exception:  # noqa: BLE001
            busy = {}
    out = []
    for rid, r in rooms_cfg.ROOMS.items():
        intervals = busy.get(rid, [])
        slots = []
        for h in hours:
            if not date:
                slots.append(1)
                continue
            s = bk.parse_dt(date, f"{h:02d}:00")
            e = bk.parse_dt(date, f"{h + 1:02d}:00")
            free = not any(a < e and s < b for (a, b) in intervals)
            slots.append(1 if free else 0)
        out.append({"room": r["name"], "capacity": r["capacity"], "slots": slots})
    return {"date": date, "hours": hours, "rooms": out, "mode": cal.mode()}


@app.post("/demo-seed")
def demo_seed(request: Request):
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    return {"status": "success", "seeded": _seed_demo(user_id)}


@app.post("/demo-stress")
def demo_stress(request: Request, payload: dict = Body(default={})):
    """Kịch bản 'ngày kín': đặt KÍN toàn bộ 6 phòng trong nhiều ngày liên tiếp,
    để minh hoạ agent quét sang các ngày sau (trong tuần / tuần kế) tìm ngày còn trống."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    date = payload.get("date") or (bk.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    days_full = int(payload.get("days", 3) or 3)
    att = payload.get("attendees", [])
    if isinstance(att, str):
        att = [a.strip() for a in att.replace(";", ",").split(",") if a.strip()]
    if not att:
        att = ["an.nguyen@zalopay.vn", "bao.tran@zalopay.vn"]

    all_rooms = ["A", "B", "C", "D", "E", "F"]
    rooms_busy = int(payload.get("rooms_busy", len(all_rooms)) or len(all_rooms))
    rooms_busy = max(1, min(rooms_busy, len(all_rooms)))
    base = bk.parse_dt(date, "00:00")
    filled = []
    for off in range(days_full):
        d_str = (base + timedelta(days=off)).strftime("%Y-%m-%d")
        # (a) Lịch bận của người gửi + người nhận (cùng dự) — 09–10h & 14–15h
        for i, (s, e) in enumerate([("09:00", "10:00"), ("14:00", "15:00")]):
            rm = all_rooms[i]
            try:
                cal.book_if_free(rm, bk.parse_dt(d_str, s), bk.parse_dt(d_str, e),
                                 f"[{rm}] Lịch nhóm", "", att, organizer=user_id)
            except Exception:  # noqa: BLE001
                pass
        # (b) Lấp đầy `rooms_busy` phòng (giờ còn lại do người khác đặt)
        for room in all_rooms[:rooms_busy]:
            for h in range(rooms_cfg.OPEN_HOUR, rooms_cfg.CLOSE_HOUR):
                owner = f"team.{room.lower()}@zalopay.vn"
                try:
                    cal.book_if_free(room, bk.parse_dt(d_str, f"{h:02d}:00"), bk.parse_dt(d_str, f"{h + 1:02d}:00"),
                                     f"[{room}] Đã đặt", "", [owner], organizer=owner)
                except Exception:  # noqa: BLE001
                    pass
        filled.append(d_str)
    next_free = (base + timedelta(days=days_full)).strftime("%Y-%m-%d")
    free_rooms = all_rooms[rooms_busy:]
    return {
        "status": "success", "date": date, "days_full": filled, "next_free": next_free,
        "attendees": att, "rooms_busy": rooms_busy, "free_rooms": free_rooms,
        "note": (f"Ngày {', '.join(filled)}: {rooms_busy}/6 phòng bận"
                 + (f" (còn trống: {', '.join(free_rooms)})" if free_rooms else " (kín hết)")
                 + f"; người gửi & người nhận bận 09–10h và 14–15h. "
                 + ("Hỏi agent gợi ý giờ → né các khung bận + phòng còn trống."
                    if free_rooms else f"Agent sẽ quét sang ngày trống (~{next_free}).")),
    }


@app.post("/compose-mail")
def compose_mail_ep(payload: dict = Body(default={})):
    """Tự soạn thân email mời họp theo mục đích (LLM)."""
    att = payload.get("attendees", [])
    if isinstance(att, str):
        att = [a.strip() for a in att.replace(";", ",").split(",") if a.strip()]
    try:
        body = _llm_compose_mail(
            purpose=payload.get("purpose", ""), content=payload.get("content", ""),
            room=payload.get("room", ""), date=payload.get("date", ""),
            start=payload.get("start_time", ""), end=payload.get("end_time", ""),
            attendees=att, language=payload.get("language", "vi"),
        )
        return {"status": "success", "body": body}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Lỗi soạn mail: {e}"}


@app.post("/translate-mail")
def translate_mail_ep(payload: dict = Body(default={})):
    """Dịch nội dung email sang ngôn ngữ đích (mặc định tiếng Anh)."""
    try:
        return {"status": "success", "text": _llm_translate(payload.get("text", ""), payload.get("target", "en"))}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Lỗi dịch: {e}"}


@app.post("/confirm")
def confirm(payload: dict = Body(default={})):
    draft_id = payload.get("draft_id", "")
    data = bk.pop_draft(draft_id)
    if not data:
        return {"status": "error", "message": "Draft không tồn tại hoặc đã xử lý."}
    try:
        # ----- lịch định kỳ -----
        if data.get("is_series"):
            booked, skipped = [], []
            for occ in data.get("occurrences", []):
                if occ["status"] == "conflict" or not occ.get("room"):
                    skipped.append(occ["date"])
                    continue
                if _commit_one(occ["room"], occ["date"], data["start_time"], data["end_time"], data):
                    booked.append(f"{occ['date']} (P.{occ['room']})")
                else:
                    skipped.append(occ["date"])
            if data.get("organizer"):
                mem.save_series(data["organizer"], data)
            if mailer.smtp_configured() and booked:
                mailer.send_booking_email({**data, "purpose": f"{data.get('purpose','')} (định kỳ {data.get('pattern')})"})
            msg = f"Đã đặt {len(booked)} buổi định kỳ: {', '.join(booked)}."
            if skipped:
                msg += f" Không xếp được (hết phòng): {', '.join(skipped)}."
            return {"status": "success", "message": msg}

        # ----- đặt đơn -----
        if not _commit_one(data["room"], data["date"], data["start_time"], data["end_time"], data):
            return {"status": "error", "message": f"Phòng {data['room']} vừa bị đặt mất. Hãy chọn lại."}
        if mailer.smtp_configured() and data.get("attendees"):
            mailer.send_booking_email(data)
            sent = f"Đã gửi mail tới {', '.join(data.get('attendees', []))}."
        elif not data.get("attendees"):
            sent = "(Chưa có người tham dự — đã giữ phòng, chưa gửi mail.)"
        else:
            sent = "(SMTP chưa cấu hình — đã giữ phòng nhưng chưa gửi mail.)"
        return {"status": "success", "message": f"Đã giữ phòng {data['room']} ({data['date']} {data['start_time']}–{data['end_time']}). {sent}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"Lỗi: {e}"}


@app.get("/schedule")
def schedule(request: Request, scope: str = "week", mine: bool = True):
    """Lịch họp theo ngày/tuần/tháng. mine=True → chỉ lịch của user hiện tại."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    start, end = _scope_range(scope)
    items = cal.list_range(start, end, organizer=user_id if mine else "")
    return {"scope": scope, "mode": cal.mode(), "items": items}


@app.post("/cancel")
def cancel_ep(request: Request, payload: dict = Body(default={})):
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    room = payload.get("room", "")
    event_id = payload.get("id", "")
    if not room or not event_id:
        return {"status": "error", "message": "Thiếu room hoặc id."}
    ok, msg = cal.cancel_event(room, event_id, organizer=user_id)
    return {"status": "success" if ok else "error", "message": "Đã huỷ cuộc họp." if ok else msg}


@app.post("/reschedule")
def reschedule_ep(request: Request, payload: dict = Body(default={})):
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    ev = {"room": payload.get("room", ""), "id": payload.get("id", ""),
          "attendees": payload.get("attendees", [])}
    if not ev["room"] or not ev["id"]:
        return {"status": "error", "message": "Thiếu room hoặc id."}
    full = cal.get_event(ev["room"], ev["id"])
    if full:
        ev["attendees"] = full.get("attendees", [])
    ok, msg = _do_reschedule(ev, payload.get("date", ""), payload.get("start_time", ""),
                             payload.get("end_time", ""), user_id)
    return {"status": "success" if ok else "error", "message": msg}


@app.get("/defaults")
def defaults(request: Request):
    """Smart defaults: thói quen của user để prefill form."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    return {"preferences": mem.preferences(user_id)}


@app.get("/contacts")
def contacts_ep(request: Request, q: str = ""):
    """Gợi ý người nhận (autocomplete) — mock + đã học + từ lịch sử."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    return {"items": contacts_dir.suggest(user_id, q)}


@app.get("/contact")
def contact_ep(request: Request, email: str = ""):
    """Lịch họp + mục đích/lời nhắn gần nhất liên quan một người nhận."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    return contacts_dir.related(user_id, email)


@app.get("/history")
def history_ep(request: Request):
    """Lịch sử đặt phòng của user (mới nhất trước)."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    items = []
    for b in reversed(mem.history(user_id)):
        items.append({
            "room": b.get("room"), "date": b.get("date"),
            "start_time": b.get("start_time"), "end_time": b.get("end_time"),
            "purpose": b.get("purpose", ""),
            "attendees": b.get("attendees", []),
            "is_series": bool(b.get("is_series")),
        })
    return {"items": items}


@app.get("/stats")
def stats(request: Request):
    """Card thống kê: tổng phòng, trống/bận hiện tại, số cuộc họp tuần này của user."""
    user_id = request.headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    now = bk.now()
    slot_end = now + timedelta(hours=1)
    avail = cal.list_availability(now, slot_end)
    available_now = sum(1 for v in avail.values() if v)
    busy_now = sum(1 for v in avail.values() if not v)
    wk_start, wk_end = _scope_range("week")
    my_week = len(cal.list_range(wk_start, wk_end, organizer=user_id))
    return {
        "total_rooms": len(rooms_cfg.ROOMS),
        "available_now": available_now,
        "busy_now": busy_now,
        "my_week": my_week,
        "mode": cal.mode(),
    }


@app.post("/invocations")
def handler(request: Request, payload: dict = Body(default={})):
    headers = request.headers
    user_id = headers.get("X-GreenNode-AgentBase-User-Id", "anon")
    session_id = headers.get("X-GreenNode-AgentBase-Session-Id", "default")
    message = payload.get("message", "Chào bạn")

    token = _current_user.set(user_id)
    config = {"configurable": {"thread_id": session_id, "actor_id": user_id}}
    try:
        result = agent.invoke({"messages": [{"role": "user", "content": message}]}, config=config)
        ai_text = _strip_thinking(result["messages"][-1].content)
        # Quét review marker ở TẤT CẢ message (lấy cái mới nhất). Việc tránh hiện lại thẻ cũ
        # do client xử lý bằng dedupe theo draft_id (_seenDrafts) → vừa luôn render được thẻ
        # mới, vừa không lặp thẻ cũ.
        review_json = _extract_review(result["messages"])
        # LƯỚI AN TOÀN: model đôi khi BỊA câu xác nhận đặt phòng mà KHÔNG gọi prepare_booking
        # → không có review thật → bỏ qua bước kiểm tra trùng. Ép gọi lại tool (tối đa 2 lần).
        nudge = (
            "DỪNG LẠI. Bạn vừa viết câu như đã đặt phòng nhưng CHƯA hề gọi tool — điều này bị CẤM. "
            "Hãy GỌI NGAY tool prepare_booking (hoặc book_recurring nếu định kỳ) với đúng thông tin vừa nêu. "
            "Chỉ có tool mới kiểm tra được phòng trùng/đè giờ. Nếu thiếu thông tin thì hỏi lại; "
            "TUYỆT ĐỐI KHÔNG tự viết 'thông tin đặt phòng' hay 'xác nhận đặt' bằng lời."
        )
        if not review_json and _looks_like_fake_booking(ai_text):
            result = agent.invoke({"messages": [{"role": "user", "content": nudge}]}, config=config)
            ai_text = _strip_thinking(result["messages"][-1].content)
            review_json = _extract_review(result["messages"])
        review = None
        if review_json:
            try:
                review = json.loads(review_json)
            except json.JSONDecodeError:
                review = None
        # LƯỚI CUỐI (tất định): model vẫn bịa xác nhận mà không có draft thật → parse lại chi
        # tiết model đã in và tạo draft qua build_draft (chạy ĐÚNG bước kiểm tra trùng phòng).
        if review is None and _looks_like_fake_booking(ai_text):
            payload, err = _salvage_booking_from_text(ai_text, user_id)
            if payload:
                review = payload
                ai_text = "Mình đã chuẩn bị bản xác nhận đặt phòng. Bạn kiểm tra thông tin và bấm Xác nhận bên dưới nhé! 😊"
            elif err:
                # phòng bận/trùng/ngoài giờ... — KHÔNG để model khẳng định 'đã đặt' sai sự thật
                ai_text = err + " Bạn chọn khung giờ hoặc phòng khác giúp mình nhé."
        # luôn bỏ marker khỏi text hiển thị — KHÔNG bao giờ để lộ [[BOOKING_REVIEW]] thô
        clean = _strip_review(ai_text)
        if not clean:
            clean = "Mời bạn xác nhận thông tin bên dưới." if review else "Mình đã xử lý xong, bạn kiểm tra giúp mình nhé."
        return {
            "status": "success",
            "response": clean,
            "review": review,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {"status": "error", "response": f"Lỗi nội bộ Agent: {e}"}
    finally:
        _current_user.reset(token)


# ---------- nhắc trước cuộc họp ----------
REMINDER_MINUTES = int(os.environ.get("REMINDER_MINUTES", "15") or "15")
_reminded: set[str] = set()


def _reminder_scan():
    """Quét cuộc họp sắp diễn ra trong REMINDER_MINUTES tới và gửi mail nhắc."""
    if not mailer.smtp_configured():
        return
    try:
        now = bk.now()
        horizon = now + timedelta(minutes=REMINDER_MINUTES)
        for it in cal.list_range(now, horizon):
            key = f"{it['room']}|{it['start']}"
            if key in _reminded or not it.get("attendees"):
                continue
            try:
                st = datetime.fromisoformat(it["start"])
            except ValueError:
                continue
            if now <= st <= horizon:
                mailer.send_booking_email({
                    "room": it["room"], "date": it["start"][:10],
                    "start_time": it["start"][11:16], "end_time": it["end"][11:16],
                    "attendees": it["attendees"], "purpose": it.get("summary", "Cuộc họp"),
                    "note": f"Nhắc lịch: cuộc họp bắt đầu lúc {it['start'][11:16]} hôm nay.",
                })
                _reminded.add(key)
    except Exception as e:  # noqa: BLE001
        print(f"[reminder] lỗi: {e}")


# Khi scale >1 replica, chỉ bật scheduler trên ĐÚNG 1 replica để tránh nhắc trùng
# (đặt REMINDERS_ENABLED=false ở các replica còn lại). Mặc định bật.
REMINDERS_ENABLED = os.environ.get("REMINDERS_ENABLED", "true").lower() != "false"


@app.on_event("startup")
def _start_scheduler():
    if not REMINDERS_ENABLED:
        print("[reminder] REMINDERS_ENABLED=false → replica này không gửi nhắc.")
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        sched = BackgroundScheduler(daemon=True)
        sched.add_job(_reminder_scan, "interval", minutes=1)
        sched.start()
        print(f"[reminder] scheduler chạy (nhắc trước {REMINDER_MINUTES} phút).")
    except Exception as e:  # noqa: BLE001
        print(f"[reminder] không khởi động được scheduler: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

