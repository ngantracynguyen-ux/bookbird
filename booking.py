"""Logic đặt phòng: parse thời gian, kiểm tra điều kiện, lưu draft chờ xác nhận."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rooms import OPEN_HOUR, CLOSE_HOUR, MAX_ADVANCE_DAYS, TIMEZONE, get_room

TZ = ZoneInfo(TIMEZONE)

_lock = threading.Lock()
_drafts: dict[str, dict] = {}


def now() -> datetime:
    return datetime.now(TZ)


def parse_dt(date_str: str, time_str: str) -> datetime:
    """date_str: YYYY-MM-DD, time_str: HH:MM. Trả về datetime có timezone."""
    raw = f"{date_str.strip()} {time_str.strip()}"
    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=TZ)


def validate(room_id: str, date_str: str, start_time: str, end_time: str, num_people: int) -> list[str]:
    """Kiểm tra điều kiện đặt phòng. Trả về danh sách lỗi (rỗng = hợp lệ)."""
    errors: list[str] = []
    room = get_room(room_id)
    if not room:
        return [f"Không tồn tại phòng '{room_id}'. Các phòng: A, B, C, D, E, F."]

    if num_people > room["capacity"]:
        errors.append(f"Phòng {room['name']} chứa tối đa {room['capacity']} người (yêu cầu {num_people}).")

    try:
        start = parse_dt(date_str, start_time)
        end = parse_dt(date_str, end_time)
    except ValueError:
        return errors + ["Định dạng ngày/giờ không hợp lệ. Dùng ngày YYYY-MM-DD và giờ HH:MM."]

    if end <= start:
        errors.append("Giờ kết thúc phải sau giờ bắt đầu.")

    if start.hour < OPEN_HOUR or end.hour > CLOSE_HOUR or (end.hour == CLOSE_HOUR and end.minute > 0):
        errors.append(f"Phòng chỉ hoạt động {OPEN_HOUR}:00–{CLOSE_HOUR}:00.")

    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    booking_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if booking_day < today:
        errors.append("Không thể đặt phòng cho ngày trong quá khứ.")
    elif (booking_day - today).days > MAX_ADVANCE_DAYS:
        errors.append(f"Chỉ được đặt trước tối đa {MAX_ADVANCE_DAYS} ngày.")

    return errors


_PATTERN_DAYS = {"daily": 1, "weekly": 7, "biweekly": 14}


def generate_occurrences(date_str: str, pattern: str, count: int) -> list[str]:
    """Sinh danh sách ngày (YYYY-MM-DD) cho lịch định kỳ.

    pattern: daily | weekly | biweekly. count: số buổi (tối đa giới hạn theo 14 ngày).
    """
    step = _PATTERN_DAYS.get((pattern or "").lower().strip())
    if not step:
        raise ValueError("pattern phải là daily, weekly hoặc biweekly")
    base = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    today = now().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    out = []
    for i in range(max(1, count)):
        d = base + timedelta(days=step * i)
        # chỉ giữ các buổi nằm trong hạn đặt trước (14 ngày)
        if (d - today).days > MAX_ADVANCE_DAYS:
            break
        if d >= today:
            out.append(d.strftime("%Y-%m-%d"))
    return out


def create_draft(data: dict) -> str:
    """Lưu draft, trả về draft_id."""
    with _lock:
        did = uuid.uuid4().hex[:12]
        _drafts[did] = data
        return did


def get_draft(draft_id: str) -> dict | None:
    with _lock:
        return _drafts.get(draft_id)


def pop_draft(draft_id: str) -> dict | None:
    with _lock:
        return _drafts.pop(draft_id, None)
