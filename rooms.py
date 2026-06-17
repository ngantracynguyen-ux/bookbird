"""Cấu hình 6 phòng họp và quy tắc đặt phòng.

Mỗi phòng map tới một Google Calendar (qua biến môi trường ROOM_<X>_CALENDAR_ID).
Khi chưa cấu hình calendar_id, agent chạy ở chế độ MOCK (bộ nhớ tạm).
"""

import os

# Giờ hoạt động (giờ địa phương Asia/Ho_Chi_Minh)
OPEN_HOUR = 8          # 8:00 AM
CLOSE_HOUR = 18        # 6:00 PM
MAX_ADVANCE_DAYS = 14  # đặt trước tối đa 14 ngày
TIMEZONE = "Asia/Ho_Chi_Minh"


def _cal_id(room: str) -> str:
    return os.environ.get(f"ROOM_{room}_CALENDAR_ID", "").strip()


# capacity: số người tối đa; type: meeting / training; amenities: tiện ích phòng
ROOMS: dict[str, dict] = {
    "A": {"name": "A", "capacity": 4,  "type": "meeting",  "amenities": ["TV"], "calendar_id": _cal_id("A"), "diagram_url": os.environ.get("ROOM_A_DIAGRAM_URL", "")},
    "B": {"name": "B", "capacity": 4,  "type": "meeting",  "amenities": ["TV"], "calendar_id": _cal_id("B"), "diagram_url": os.environ.get("ROOM_B_DIAGRAM_URL", "")},
    "C": {"name": "C", "capacity": 6,  "type": "meeting",  "amenities": ["Máy chiếu"], "calendar_id": _cal_id("C"), "diagram_url": os.environ.get("ROOM_C_DIAGRAM_URL", "")},
    "D": {"name": "D", "capacity": 6,  "type": "meeting",  "amenities": ["Máy chiếu"], "calendar_id": _cal_id("D"), "diagram_url": os.environ.get("ROOM_D_DIAGRAM_URL", "")},
    "E": {"name": "E", "capacity": 10, "type": "meeting",  "amenities": ["Máy chiếu", "Video call"], "calendar_id": _cal_id("E"), "diagram_url": os.environ.get("ROOM_E_DIAGRAM_URL", "")},
    "F": {"name": "F", "capacity": 30, "type": "training", "amenities": ["Sân khấu", "Máy chiếu", "Mic"], "calendar_id": _cal_id("F"), "diagram_url": os.environ.get("ROOM_F_DIAGRAM_URL", "")},
}


def get_room(room_id: str) -> dict | None:
    return ROOMS.get((room_id or "").strip().upper())


def rooms_fitting(num_people: int) -> list[dict]:
    """Danh sách phòng có sức chứa >= num_people, sắp theo sức chứa tăng dần
    (ưu tiên phòng vừa đủ, tránh lãng phí phòng lớn)."""
    fits = [r for r in ROOMS.values() if r["capacity"] >= max(1, num_people)]
    return sorted(fits, key=lambda r: r["capacity"])
