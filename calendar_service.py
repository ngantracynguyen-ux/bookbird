"""Tầng truy cập lịch phòng họp.

- Nếu có credential Google (biến GOOGLE_SA_CREDENTIALS = đường dẫn file JSON
  hoặc nội dung JSON) và phòng có calendar_id → dùng Google Calendar thật.
- Ngược lại → chế độ MOCK: lưu booking trong bộ nhớ tạm để demo.

API công khai:
    is_available(room_id, start_dt, end_dt) -> bool
    list_availability(start_dt, end_dt) -> dict[str, bool]   # {room_id: available?}
    create_event(room_id, start_dt, end_dt, summary, description, attendees) -> dict
    list_bookings(room_id, day) -> list[dict]
    mode() -> "google" | "mock"
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from rooms import ROOMS, get_room, TIMEZONE

_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ---- in-memory store cho MOCK mode ----
_lock = threading.Lock()
# _mock_bookings[room_id] = list of {id, start, end, summary, description, attendees}
_mock_bookings: dict[str, list[dict]] = {r: [] for r in ROOMS}

_google_ready: bool | None = None   # None=chưa thử, True=có Google, False=MOCK
_creds = None                        # credentials (chia sẻ được giữa các thread)
_tls = threading.local()             # service Google PER-THREAD (httplib2 KHÔNG thread-safe!)


def _build_creds():
    """Tạo credentials 1 lần (dùng lại được). Trả về creds hoặc None."""
    global _creds, _google_ready
    if _creds is not None:
        return _creds
    raw = os.environ.get("GOOGLE_SA_CREDENTIALS", "").strip()
    raw_b64 = os.environ.get("GOOGLE_SA_CREDENTIALS_B64", "").strip()
    if not raw and not raw_b64:
        _google_ready = False
        return None
    import base64
    from google.oauth2 import service_account
    if raw_b64:  # ưu tiên base64 (1 dòng, hợp với env AgentBase)
        info = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
        _creds = service_account.Credentials.from_service_account_info(info, scopes=_GOOGLE_SCOPES)
    elif raw.startswith("{"):
        info = json.loads(raw)
        _creds = service_account.Credentials.from_service_account_info(info, scopes=_GOOGLE_SCOPES)
    else:
        _creds = service_account.Credentials.from_service_account_file(raw, scopes=_GOOGLE_SCOPES)
    return _creds


def _load_google():
    """Service Google Calendar RIÊNG cho mỗi thread (tránh lỗi SSL record layer do
    httplib2 không thread-safe khi nhiều request gọi Google đồng thời)."""
    global _google_ready
    if _google_ready is False:
        return None
    svc = getattr(_tls, "svc", None)
    if svc is not None:
        return svc
    try:
        from googleapiclient.discovery import build
        creds = _build_creds()
        if creds is None:
            return None
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _tls.svc = svc
        _google_ready = True
        return svc
    except Exception as e:  # noqa: BLE001
        print(f"[calendar] Không khởi tạo được Google Calendar, dùng MOCK. Lỗi: {e}")
        _google_ready = False
        return None


def _use_google(room_id: str) -> bool:
    room = get_room(room_id)
    return bool(room and room.get("calendar_id")) and _load_google() is not None


def mode() -> str:
    return "google" if _load_google() is not None else "mock"


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


# ----------------- availability -----------------

def is_available(room_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    room_id = room_id.strip().upper()
    if _use_google(room_id):
        service = _load_google()
        cal_id = get_room(room_id)["calendar_id"]
        body = {
            "timeMin": start_dt.isoformat(),
            "timeMax": end_dt.isoformat(),
            "timeZone": TIMEZONE,
            "items": [{"id": cal_id}],
        }
        resp = service.freebusy().query(body=body).execute()
        busy = resp["calendars"].get(cal_id, {}).get("busy", [])
        return len(busy) == 0
    # MOCK
    with _lock:
        for b in _mock_bookings.get(room_id, []):
            if _overlap(start_dt, end_dt, b["start"], b["end"]):
                return False
        return True


def list_availability(start_dt: datetime, end_dt: datetime) -> dict[str, bool]:
    return {rid: is_available(rid, start_dt, end_dt) for rid in ROOMS}


def busy_map_for_day(day_start: datetime, day_end: datetime) -> dict[str, list[tuple]]:
    """Khoảng bận của TỪNG phòng trong [day_start, day_end] → {room_id: [(start,end),...]}.
    Google: gọi freebusy 1 LẦN cho cả 6 phòng (thay vì 60 lượt is_available) → nhanh, không timeout."""
    out: dict[str, list[tuple]] = {rid: [] for rid in ROOMS}
    google_rooms = [rid for rid in ROOMS if _use_google(rid)]
    if google_rooms:
        service = _load_google()
        id2room = {get_room(rid)["calendar_id"]: rid for rid in google_rooms}
        resp = service.freebusy().query(body={
            "timeMin": day_start.isoformat(), "timeMax": day_end.isoformat(),
            "timeZone": TIMEZONE, "items": [{"id": cid} for cid in id2room],
        }).execute()
        for cid, info in resp.get("calendars", {}).items():
            rid = id2room.get(cid)
            if not rid:
                continue
            for b in info.get("busy", []):
                out[rid].append((
                    datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]),
                ))
    # phòng MOCK (hoặc khi Google lỗi)
    with _lock:
        for rid in ROOMS:
            if rid in google_rooms:
                continue
            for b in _mock_bookings.get(rid, []):
                if _overlap(day_start, day_end, b["start"], b["end"]):
                    out[rid].append((b["start"], b["end"]))
    return out


# ----------------- create -----------------

def create_event(
    room_id: str,
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str = "",
    attendees: list[str] | None = None,
    organizer: str = "",
) -> dict:
    room_id = room_id.strip().upper()
    attendees = attendees or []
    if _use_google(room_id):
        service = _load_google()
        cal_id = get_room(room_id)["calendar_id"]
        # KHÔNG set Google attendees: service account không có Domain-Wide Delegation sẽ bị 403
        # ("cannot invite attendees"). Thư mời gửi bằng SMTP riêng của app. Người tham dự +
        # người đặt lưu ở extendedProperties để lọc "lịch của tôi" & chỉ chủ lịch được huỷ/dời.
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
            "extendedProperties": {"private": {
                "bb_organizer": organizer or "",
                "bb_attendees": ",".join(attendees),
            }},
        }
        created = service.events().insert(calendarId=cal_id, body=event).execute()
        return {"id": created.get("id"), "htmlLink": created.get("htmlLink"), "mode": "google"}
    # MOCK
    with _lock:
        bid = "mock-" + uuid.uuid4().hex[:10]
        _mock_bookings.setdefault(room_id, []).append({
            "id": bid, "start": start_dt, "end": end_dt,
            "summary": summary, "description": description,
            "attendees": attendees, "organizer": organizer,
        })
        return {"id": bid, "htmlLink": "", "mode": "mock"}


def book_if_free(room_id: str, start_dt: datetime, end_dt: datetime, summary: str,
                 description: str = "", attendees: list[str] | None = None,
                 organizer: str = "") -> tuple[dict | None, str | None]:
    """Nguyên tử: kiểm tra trống + tạo trong cùng một critical section.
    Tránh đặt trùng khi 2 người xác nhận cùng lúc. Trả về (event, error)."""
    room_id = room_id.strip().upper()
    attendees = attendees or []
    if _use_google(room_id):
        # Google: best-effort (freebusy → insert)
        if not is_available(room_id, start_dt, end_dt):
            return None, "busy"
        return create_event(room_id, start_dt, end_dt, summary, description, attendees, organizer), None
    # MOCK: check + insert trong cùng 1 lock (không gọi is_available/create_event để tránh deadlock)
    with _lock:
        for b in _mock_bookings.get(room_id, []):
            if _overlap(start_dt, end_dt, b["start"], b["end"]):
                return None, "busy"
        bid = "mock-" + uuid.uuid4().hex[:10]
        _mock_bookings.setdefault(room_id, []).append({
            "id": bid, "start": start_dt, "end": end_dt,
            "summary": summary, "description": description,
            "attendees": attendees, "organizer": organizer,
        })
        return {"id": bid, "htmlLink": "", "mode": "mock"}, None


def list_range(start_dt: datetime, end_dt: datetime, organizer: str = "") -> list[dict]:
    """Liệt kê tất cả booking trong khoảng [start, end] trên mọi phòng.
    Nếu truyền organizer → chỉ lấy booking của người đó (chỉ áp dụng MOCK)."""
    out: list[dict] = []
    for rid in ROOMS:
        if _use_google(rid):
            service = _load_google()
            cal_id = get_room(rid)["calendar_id"]
            kwargs = dict(
                calendarId=cal_id, timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(),
                singleEvents=True, orderBy="startTime",
            )
            if organizer:  # chỉ lấy lịch do CHÍNH người này đặt (lọc phía Google)
                kwargs["privateExtendedProperty"] = f"bb_organizer={organizer}"
            resp = service.events().list(**kwargs).execute()
            for ev in resp.get("items", []):
                priv = ev.get("extendedProperties", {}).get("private", {})
                out.append({
                    "id": ev.get("id"), "room": rid, "summary": ev.get("summary", ""),
                    "start": ev["start"].get("dateTime", ev["start"].get("date")),
                    "end": ev["end"].get("dateTime", ev["end"].get("date")),
                    "attendees": [a for a in priv.get("bb_attendees", "").split(",") if a],
                    "organizer": priv.get("bb_organizer", ""),
                })
        else:
            with _lock:
                for b in _mock_bookings.get(rid, []):
                    if _overlap(start_dt, end_dt, b["start"], b["end"]):
                        if organizer and b.get("organizer") and b["organizer"] != organizer:
                            continue
                        out.append({
                            "id": b["id"], "room": rid, "summary": b["summary"],
                            "start": b["start"].isoformat(), "end": b["end"].isoformat(),
                            "attendees": b.get("attendees", []), "organizer": b.get("organizer", ""),
                        })
    return sorted(out, key=lambda x: x["start"])


def busy_for(emails, start_dt: datetime, end_dt: datetime) -> list[tuple]:
    """Các khoảng bận của BẤT KỲ người nào trong `emails` (là người đặt hoặc người dự)
    trong khoảng [start, end]. Trả về list (start, end). (MOCK; Google bỏ qua ở bản này.)"""
    targets = {e.strip().lower() for e in (emails or []) if e and e.strip()}
    if not targets:
        return []
    out: list[tuple] = []
    for it in list_range(start_dt, end_dt):
        people = {(it.get("organizer") or "").lower()} | {(a or "").lower() for a in it.get("attendees", [])}
        if targets & people:
            out.append((datetime.fromisoformat(it["start"]), datetime.fromisoformat(it["end"])))
    return out


def busy_by_person(emails, start_dt: datetime, end_dt: datetime) -> dict:
    """Lịch bận theo TỪNG người (organizer hoặc attendee) trong [start, end].
    Trả về {email: [(start,end), ...]} đã sắp xếp. Dùng để giải thích gợi ý giờ."""
    targets = {e.strip().lower() for e in (emails or []) if e and e.strip()}
    out: dict[str, list] = {e: [] for e in targets}
    if not targets:
        return out
    for it in list_range(start_dt, end_dt):
        people = {(it.get("organizer") or "").lower()} | {(a or "").lower() for a in it.get("attendees", [])}
        for e in targets & people:
            out[e].append((datetime.fromisoformat(it["start"]), datetime.fromisoformat(it["end"])))
    for e in out:
        out[e].sort(key=lambda x: x[0])
    return out


def get_event(room_id: str, event_id: str) -> dict | None:
    """Lấy thông tin 1 booking theo id (chỉ MOCK giữ đủ field; Google trả tối thiểu)."""
    room_id = room_id.strip().upper()
    if _use_google(room_id):
        service = _load_google()
        cal_id = get_room(room_id)["calendar_id"]
        try:
            ev = service.events().get(calendarId=cal_id, eventId=event_id).execute()
        except Exception:  # noqa: BLE001
            return None
        _priv = ev.get("extendedProperties", {}).get("private", {})
        return {
            "id": ev.get("id"), "room": room_id, "summary": ev.get("summary", ""),
            "start": ev["start"].get("dateTime"), "end": ev["end"].get("dateTime"),
            "attendees": [a for a in _priv.get("bb_attendees", "").split(",") if a],
            "organizer": _priv.get("bb_organizer", ""),
            "description": ev.get("description", ""),
        }
    with _lock:
        for b in _mock_bookings.get(room_id, []):
            if b["id"] == event_id:
                return {
                    "id": b["id"], "room": room_id, "summary": b["summary"],
                    "start": b["start"].isoformat(), "end": b["end"].isoformat(),
                    "attendees": b.get("attendees", []), "organizer": b.get("organizer", ""),
                    "description": b.get("description", ""),
                }
    return None


def cancel_event(room_id: str, event_id: str, organizer: str = "") -> tuple[bool, str]:
    """Huỷ booking theo id. Nếu organizer truyền vào, chỉ cho huỷ booking của chính họ."""
    room_id = room_id.strip().upper()
    if _use_google(room_id):
        service = _load_google()
        cal_id = get_room(room_id)["calendar_id"]
        try:
            if organizer:  # chỉ người đặt mới được huỷ (toàn công ty dùng chung lịch phòng)
                ev = service.events().get(calendarId=cal_id, eventId=event_id).execute()
                owner = ev.get("extendedProperties", {}).get("private", {}).get("bb_organizer", "")
                if owner != organizer:
                    return False, "Đây là lịch do người khác đặt — bạn không thể huỷ/dời."
            service.events().delete(calendarId=cal_id, eventId=event_id).execute()
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    with _lock:
        lst = _mock_bookings.get(room_id, [])
        for i, b in enumerate(lst):
            if b["id"] == event_id:
                if organizer and b.get("organizer") and b["organizer"] != organizer:
                    return False, "Bạn không có quyền huỷ booking này."
                lst.pop(i)
                return True, "ok"
    return False, "Không tìm thấy booking."


def list_bookings(room_id: str, day: datetime) -> list[dict]:
    room_id = room_id.strip().upper()
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start.replace(hour=23, minute=59, second=59)
    if _use_google(room_id):
        service = _load_google()
        cal_id = get_room(room_id)["calendar_id"]
        resp = service.events().list(
            calendarId=cal_id, timeMin=day_start.isoformat(), timeMax=day_end.isoformat(),
            singleEvents=True, orderBy="startTime",
        ).execute()
        out = []
        for ev in resp.get("items", []):
            out.append({
                "id": ev.get("id"),
                "summary": ev.get("summary", ""),
                "start": ev["start"].get("dateTime", ev["start"].get("date")),
                "end": ev["end"].get("dateTime", ev["end"].get("date")),
            })
        return out
    # MOCK
    with _lock:
        out = []
        for b in _mock_bookings.get(room_id, []):
            if _overlap(day_start, day_end, b["start"], b["end"]):
                out.append({
                    "id": b["id"], "summary": b["summary"],
                    "start": b["start"].isoformat(), "end": b["end"].isoformat(),
                })
        return sorted(out, key=lambda x: x["start"])
