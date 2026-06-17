"""Danh bạ người nhận: mock seed + học email mới + suy gợi ý từ lịch sử.

Nguồn gợi ý (gộp & xếp theo tần suất):
  1. MOCK_CONTACTS — danh bạ mẫu có sẵn.
  2. _learned — email mới user nhập trong phiên (học runtime).
  3. Lịch sử đặt phòng của user (GreenNode Memory) — bền vững giữa các phiên.

Mỗi người nhận gắn với: số lần xuất hiện, các cuộc họp liên quan, mục đích/lời nhắn gần nhất.
"""

from __future__ import annotations

import threading

import memory_store as mem

# Danh bạ mẫu (mock) — thay bằng danh bạ thật khi cần
MOCK_CONTACTS = [
    {"email": "an.nguyen@zalopay.vn", "name": "An Nguyễn"},
    {"email": "bao.tran@zalopay.vn", "name": "Bảo Trần"},
    {"email": "chi.le@zalopay.vn", "name": "Chi Lê"},
    {"email": "dung.pham@zalopay.vn", "name": "Dũng Phạm"},
    {"email": "giang.vo@zalopay.vn", "name": "Giang Võ"},
    {"email": "ha.do@zalopay.vn", "name": "Hà Đỗ"},
    {"email": "khoa.bui@zalopay.vn", "name": "Khoa Bùi"},
    {"email": "linh.dang@zalopay.vn", "name": "Linh Đặng"},
    {"email": "minh.hoang@zalopay.vn", "name": "Minh Hoàng"},
    {"email": "phuong.ngo@zalopay.vn", "name": "Phương Ngô"},
]

_lock = threading.Lock()
_learned: dict[str, dict] = {}  # email -> {email, name, count}


def name_from_email(email: str) -> str:
    local = email.split("@")[0]
    return local.replace(".", " ").replace("_", " ").strip().title()


def learn(email: str, name: str = "") -> None:
    """Học email mới (hoặc tăng tần suất). Gọi khi user xác nhận booking."""
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return
    with _lock:
        c = _learned.get(e) or {"email": e, "name": name or name_from_email(e), "count": 0}
        c["count"] += 1
        if name:
            c["name"] = name
        _learned[e] = c


def _all_recipients(booking: dict) -> list[str]:
    return (booking.get("attendees", []) or []) + (booking.get("cc", []) or []) + (booking.get("bcc", []) or [])


def _merged(user_id: str) -> dict[str, dict]:
    """Gộp mock + learned + lịch sử user → {email: {email, name, count}}."""
    out: dict[str, dict] = {}
    for c in MOCK_CONTACTS:
        out[c["email"].lower()] = {"email": c["email"], "name": c["name"], "count": 0}
    for e, c in _learned.items():
        cur = out.get(e) or {"email": e, "name": c["name"], "count": 0}
        cur["count"] += c["count"]
        out[e] = cur
    for b in mem.history(user_id):
        for e in _all_recipients(b):
            el = e.strip().lower()
            if not el:
                continue
            cur = out.get(el) or {"email": el, "name": name_from_email(el), "count": 0}
            cur["count"] += 1
            out[el] = cur
    return out


def suggest(user_id: str, q: str = "", limit: int = 8) -> list[dict]:
    """Gợi ý người nhận khớp q (theo email hoặc tên), xếp theo tần suất."""
    ql = (q or "").strip().lower()
    items = list(_merged(user_id).values())
    if ql:
        items = [c for c in items if ql in c["email"].lower() or ql in c["name"].lower()]
    items.sort(key=lambda c: (-c["count"], c["email"]))
    return items[:limit]


def related(user_id: str, email: str) -> dict:
    """Lịch họp + mục đích/lời nhắn gần nhất liên quan tới một người nhận."""
    e = (email or "").strip().lower()
    meetings, last_purpose, last_message = [], "", ""
    for b in mem.history(user_id):
        if e in [x.strip().lower() for x in _all_recipients(b)]:
            meetings.append({
                "date": b.get("date"), "start_time": b.get("start_time"),
                "end_time": b.get("end_time"), "room": b.get("room"),
                "purpose": b.get("purpose", ""),
            })
            if b.get("purpose"):
                last_purpose = b["purpose"]
            if b.get("message"):
                last_message = b["message"]
    return {"email": e, "name": name_from_email(e), "meetings": meetings,
            "last_purpose": last_purpose, "last_message": last_message}
