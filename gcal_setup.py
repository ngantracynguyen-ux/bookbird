"""Tạo & seed Google Calendar cho 6 phòng họp BookBird — để quay demo PAINPOINT.

Một lần chạy là có ngay 6 lịch phòng (A–F) trên Google, chia sẻ về email của bạn để
xem/quay, kèm kịch bản "ngày kín phòng". Các event này agent đọc được luôn (sync thật).

CÀI ĐẶT TRƯỚC:
  1. Tạo Google Cloud project → bật "Google Calendar API".
  2. Tạo Service Account → tạo key JSON → tải về (vd sa.json).
  3. pip install google-api-python-client google-auth   (đã có trong requirements.txt)

DÙNG:
  # tạo 6 lịch phòng + chia sẻ cho email của bạn (để thấy trong Google Calendar)
  python gcal_setup.py create --creds sa.json --share you@gmail.com

  # seed kịch bản painpoint (đa số phòng kín) cho 1 ngày
  python gcal_setup.py seed   --creds sa.json --date 2026-06-20

  # xoá hết event đã seed trong 1 ngày (dọn dẹp sau khi quay)
  python gcal_setup.py clear  --creds sa.json --date 2026-06-20

Sau bước `create`, script in ra khối ENV (ROOM_*_CALENDAR_ID + GOOGLE_SA_CREDENTIALS_B64)
để dán vào .env.deploy rồi deploy lại — agent sẽ đọc đúng các lịch này.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = "Asia/Ho_Chi_Minh"
ROOMS = [("A", 4), ("B", 4), ("C", 6), ("D", 6), ("E", 10), ("F", 30)]
CAL_PREFIX = "BookBird · Phòng "

# Kịch bản painpoint: mỗi phòng bận nhiều khung rải rác trong ngày 8–18h
# → tìm 1 khung chung trống rất khó (đúng nỗi đau). Phòng F (training) để trống vài khung.
PAINPOINT = {
    "A": [("08:00", "09:30", "Standup + review PR"), ("10:00", "11:30", "Họp khách hàng"), ("14:00", "15:00", "1:1"), ("16:00", "17:30", "Phỏng vấn")],
    "B": [("08:30", "10:00", "Sprint planning"), ("10:30", "12:00", "Design review"), ("13:30", "15:00", "Daily ops"), ("15:30", "17:00", "Sync team")],
    "C": [("09:00", "10:30", "Họp tài chính"), ("11:00", "12:00", "Review ngân sách"), ("14:00", "16:00", "Đào tạo nghiệp vụ")],
    "D": [("08:00", "09:00", "Catch-up"), ("09:30", "11:00", "Họp dự án"), ("13:00", "14:30", "Retro"), ("15:00", "16:30", "Demo nội bộ")],
    "E": [("08:30", "10:30", "Town hall"), ("11:00", "12:00", "Họp BU"), ("14:00", "15:30", "Workshop"), ("16:00", "18:00", "Đào tạo")],
    "F": [("09:00", "12:00", "Training onboarding")],  # còn trống chiều → "khe hiếm hoi"
}


def _creds(path_or_json: str):
    raw = path_or_json.strip()
    if raw.startswith("{"):
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)


def _svc(creds_arg):
    return build("calendar", "v3", credentials=_creds(creds_arg), cache_discovery=False)


def _find_calendar(svc, summary):
    page = None
    while True:
        resp = svc.calendarList().list(pageToken=page).execute()
        for c in resp.get("items", []):
            if c.get("summary") == summary:
                return c["id"]
        page = resp.get("nextPageToken")
        if not page:
            return None


def cmd_create(svc, share_email):
    ids = {}
    for room, _cap in ROOMS:
        summary = CAL_PREFIX + room
        cid = _find_calendar(svc, summary)
        if not cid:
            created = svc.calendars().insert(body={"summary": summary, "timeZone": TZ}).execute()
            cid = created["id"]
            print(f"  + Tạo lịch {summary} → {cid}", file=sys.stderr)
        else:
            print(f"  = Đã có lịch {summary} → {cid}", file=sys.stderr)
        if share_email:
            try:
                svc.acl().insert(calendarId=cid, body={
                    "role": "writer",
                    "scope": {"type": "user", "value": share_email},
                }).execute()
                print(f"    ↳ chia sẻ cho {share_email} (writer)", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"    ! không chia sẻ được: {e}", file=sys.stderr)
        else:
            # không có email → đặt công khai-chỉ-xem để 'Đăng ký bằng ID' mà quay
            try:
                svc.acl().insert(calendarId=cid, body={
                    "role": "reader", "scope": {"type": "default"},
                }).execute()
                print("    ↳ đặt công khai (ai có ID đều xem được)", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                print(f"    ! không đặt công khai được: {e}", file=sys.stderr)
        ids[room] = cid

    print("\n# ==== Dán khối này vào room-booking/.env.deploy rồi deploy lại ====")
    for room, _ in ROOMS:
        print(f"ROOM_{room}_CALENDAR_ID={ids[room]}")
    print("\n# Mở Google Calendar → 'Thêm lịch' → 'Đăng ký bằng ID' và dán từng ID trên để XEM/QUAY.")
    print("# (Hoặc chấp nhận lời mời chia sẻ trong email nếu có.)")


def _ext_props(organizer):
    """extendedProperties để gán 'người đặt' (đồng nghiệp) cho event seed — khớp định dạng app."""
    if not organizer:
        return None
    return {"private": {"bb_organizer": organizer, "bb_attendees": organizer}}


def cmd_seed(svc, date_str, organizer=""):
    ids = {room: _find_calendar(svc, CAL_PREFIX + room) for room, _ in ROOMS}
    ext = _ext_props(organizer)
    n = 0
    for room, slots in PAINPOINT.items():
        cid = ids.get(room)
        if not cid:
            print(f"  ! Chưa có lịch phòng {room}, chạy 'create' trước.", file=sys.stderr)
            continue
        for s, e, title in slots:
            sd = datetime.fromisoformat(f"{date_str}T{s}:00")
            ed = datetime.fromisoformat(f"{date_str}T{e}:00")
            body = {
                "summary": f"[{room}] {title}",
                "start": {"dateTime": sd.isoformat(), "timeZone": TZ},
                "end": {"dateTime": ed.isoformat(), "timeZone": TZ},
            }
            if ext:
                body["extendedProperties"] = ext
            svc.events().insert(calendarId=cid, body=body).execute()
            n += 1
    who = f" (người đặt: {organizer})" if organizer else ""
    print(f"Đã seed {n} cuộc họp 'painpoint' cho ngày {date_str}{who}.", file=sys.stderr)


def cmd_fill(svc, date_str, organizer=""):
    """Lấp KÍN cả ngày (08:00–18:00) toàn bộ 6 phòng → 'hết sạch phòng', buộc agent quét ngày khác."""
    ids = {room: _find_calendar(svc, CAL_PREFIX + room) for room, _ in ROOMS}
    ext = _ext_props(organizer)
    n = 0
    for room, _cap in ROOMS:
        cid = ids.get(room)
        if not cid:
            print(f"  ! Chưa có lịch phòng {room}, chạy 'create' trước.", file=sys.stderr)
            continue
        sd = datetime.fromisoformat(f"{date_str}T08:00:00")
        ed = datetime.fromisoformat(f"{date_str}T18:00:00")
        body = {
            "summary": f"[{room}] Kín cả ngày",
            "start": {"dateTime": sd.isoformat(), "timeZone": TZ},
            "end": {"dateTime": ed.isoformat(), "timeZone": TZ},
        }
        if ext:
            body["extendedProperties"] = ext
        svc.events().insert(calendarId=cid, body=body).execute()
        n += 1
    print(f"Đã lấp KÍN {n} phòng cho ngày {date_str}.", file=sys.stderr)


def cmd_slot(svc, date_str, start, end, organizer=""):
    """Đặt 1 khung giờ cụ thể (start–end) cho TOÀN BỘ 6 phòng → phòng kín đúng khung đó."""
    ids = {room: _find_calendar(svc, CAL_PREFIX + room) for room, _ in ROOMS}
    ext = _ext_props(organizer)
    n = 0
    for room, _cap in ROOMS:
        cid = ids.get(room)
        if not cid:
            print(f"  ! Chưa có lịch phòng {room}, chạy 'create' trước.", file=sys.stderr)
            continue
        sd = datetime.fromisoformat(f"{date_str}T{start}:00")
        ed = datetime.fromisoformat(f"{date_str}T{end}:00")
        body = {
            "summary": f"[{room}] Đã đặt",
            "start": {"dateTime": sd.isoformat(), "timeZone": TZ},
            "end": {"dateTime": ed.isoformat(), "timeZone": TZ},
        }
        if ext:
            body["extendedProperties"] = ext
        svc.events().insert(calendarId=cid, body=body).execute()
        n += 1
    print(f"Đã đặt {n} phòng khung {start}–{end} ngày {date_str}.", file=sys.stderr)


def cmd_clear(svc, date_str):
    ids = {room: _find_calendar(svc, CAL_PREFIX + room) for room, _ in ROOMS}
    day_start = datetime.fromisoformat(f"{date_str}T00:00:00")
    day_end = day_start + timedelta(days=1)
    n = 0
    for room, cid in ids.items():
        if not cid:
            continue
        resp = svc.events().list(calendarId=cid, timeMin=day_start.isoformat() + "+07:00",
                                 timeMax=day_end.isoformat() + "+07:00", singleEvents=True).execute()
        for ev in resp.get("items", []):
            svc.events().delete(calendarId=cid, eventId=ev["id"]).execute()
            n += 1
    print(f"Đã xoá {n} event trong ngày {date_str}.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "seed", "fill", "slot", "clear", "b64"])
    ap.add_argument("--creds", required=True, help="đường dẫn sa.json hoặc nội dung JSON")
    ap.add_argument("--share", default="", help="email nhận chia sẻ lịch (cmd create)")
    ap.add_argument("--date", default="", help="YYYY-MM-DD (cmd seed/fill/clear)")
    ap.add_argument("--days", type=int, default=1, help="số ngày liên tiếp từ --date (seed/fill/clear)")
    ap.add_argument("--organizer", default="", help="gán 'người đặt' (đồng nghiệp) cho event seed/fill/slot")
    ap.add_argument("--start", default="14:00", help="giờ bắt đầu HH:MM (cmd slot)")
    ap.add_argument("--end", default="15:00", help="giờ kết thúc HH:MM (cmd slot)")
    args = ap.parse_args()

    if args.cmd == "b64":
        # in base64 của SA JSON để set GOOGLE_SA_CREDENTIALS_B64 trên AgentBase
        with open(args.creds, "rb") as f:
            print("GOOGLE_SA_CREDENTIALS_B64=" + base64.b64encode(f.read()).decode())
        return

    svc = _svc(args.creds)
    if args.cmd == "create":
        cmd_create(svc, args.share)
        return
    if not args.date:
        sys.exit("Cần --date YYYY-MM-DD")
    base = datetime.fromisoformat(f"{args.date}T00:00:00")
    for i in range(max(1, args.days)):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        if args.cmd == "seed":
            cmd_seed(svc, d, args.organizer)
        elif args.cmd == "fill":
            cmd_fill(svc, d, args.organizer)
        elif args.cmd == "slot":
            cmd_slot(svc, d, args.start, args.end, args.organizer)
        else:
            cmd_clear(svc, d)


if __name__ == "__main__":
    main()
