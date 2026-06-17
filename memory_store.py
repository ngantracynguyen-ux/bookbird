"""Lưu lịch sử đặt phòng & thói quen của từng user.

- Nếu có MEMORY_ID (+ credential GreenNode tự inject trong runtime) → dùng GreenNode Memory
  để lưu bền vững + recall theo ngữ nghĩa ("đặt như lần trước").
- Luôn giữ thêm một bản structured trong bộ nhớ tiến trình để liệt kê/tính smart-defaults nhanh.
- Khi chưa có MEMORY_ID → chỉ dùng bản in-memory (đủ để demo).
"""

from __future__ import annotations

import os
import threading
from collections import Counter

MEMORY_ID = os.environ.get("MEMORY_ID", "").strip()
STRATEGY = os.environ.get("MEMORY_STRATEGY_ID", "default")

_lock = threading.Lock()
# _local[user_id] = {"bookings": [booking, ...], "series": [series, ...]}
_local: dict[str, dict] = {}

_client = None
_ready: bool | None = None


def _ns(user_id: str) -> str:
    return f"/strategies/{STRATEGY}/actors/booking-{user_id}"


def _get_client():
    global _client, _ready
    if _ready is not None:
        return _client
    if not MEMORY_ID:
        _ready = False
        return None
    try:
        from greennode_agentbase.memory import MemoryClient
        _client = MemoryClient()
        _ready = True
    except Exception as e:  # noqa: BLE001
        print(f"[memory] Không khởi tạo được GreenNode Memory, dùng bộ nhớ tạm. Lỗi: {e}")
        _client = None
        _ready = False
    return _client


def enabled() -> bool:
    return _get_client() is not None


def _bucket(user_id: str) -> dict:
    return _local.setdefault(user_id, {"bookings": [], "series": []})


# ---------------- bookings ----------------

def save_booking(user_id: str, booking: dict) -> None:
    with _lock:
        _bucket(user_id)["bookings"].append(dict(booking))
    c = _get_client()
    if c:
        att = ", ".join(booking.get("attendees", []))
        text = (
            f"Đặt phòng {booking.get('room')} ngày {booking.get('date')} "
            f"{booking.get('start_time')}-{booking.get('end_time')}, "
            f"mục đích: {booking.get('purpose','')}, người dự: {att}"
        )
        try:
            from greennode_agentbase.memory.models import MemoryRecordInsertDirectlyRequest
            c.insert_memory_records_directly(
                id=MEMORY_ID, namespace=_ns(user_id),
                request=MemoryRecordInsertDirectlyRequest(memory_records=[text]),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[memory] save_booking lỗi: {e}")


def history(user_id: str) -> list[dict]:
    with _lock:
        return list(_bucket(user_id)["bookings"])


def recall_text(user_id: str, query: str, limit: int = 5) -> list[str]:
    """Tìm lịch sử theo ngữ nghĩa qua GreenNode Memory (rỗng nếu chưa bật)."""
    c = _get_client()
    if not c:
        return []
    try:
        from greennode_agentbase.memory.models import MemoryRecordSearchRequest
        res = c.search_memory_records(
            id=MEMORY_ID, namespace=_ns(user_id),
            request=MemoryRecordSearchRequest(query=query, limit=max(5, limit)),
        )
        out = []
        for r in res:
            out.append(r.get("memory", "") if isinstance(r, dict) else getattr(r, "memory", ""))
        return [x for x in out if x]
    except Exception as e:  # noqa: BLE001
        print(f"[memory] recall lỗi: {e}")
        return []


# ---------------- smart defaults ----------------

def preferences(user_id: str) -> dict:
    """Suy thói quen từ lịch sử: phòng hay dùng, attendee/mục đích thường gặp, độ dài TB."""
    hist = history(user_id)
    if not hist:
        return {}
    rooms = Counter(b.get("room") for b in hist if b.get("room"))
    purposes = Counter(b.get("purpose") for b in hist if b.get("purpose"))
    attendees = Counter()
    durations = []
    for b in hist:
        for a in b.get("attendees", []):
            attendees[a] += 1
        durations.append((b.get("start_time", ""), b.get("end_time", "")))
    return {
        "count": len(hist),
        "favorite_room": rooms.most_common(1)[0][0] if rooms else None,
        "common_purpose": purposes.most_common(1)[0][0] if purposes else None,
        "frequent_attendees": [a for a, _ in attendees.most_common(5)],
        "last_booking": hist[-1],
    }


# ---------------- recurring series ----------------

def save_series(user_id: str, series: dict) -> None:
    with _lock:
        _bucket(user_id)["series"].append(dict(series))
    c = _get_client()
    if c:
        text = (
            f"Lịch định kỳ: {series.get('pattern')} phòng {series.get('room')} "
            f"{series.get('start_time')}-{series.get('end_time')}, mục đích: {series.get('purpose','')}"
        )
        try:
            from greennode_agentbase.memory.models import MemoryRecordInsertDirectlyRequest
            c.insert_memory_records_directly(
                id=MEMORY_ID, namespace=_ns(user_id),
                request=MemoryRecordInsertDirectlyRequest(memory_records=[text]),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[memory] save_series lỗi: {e}")


def list_series(user_id: str) -> list[dict]:
    with _lock:
        return list(_bucket(user_id)["series"])
