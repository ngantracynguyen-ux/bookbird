# 🗓️ Nối Google Calendar — để quay painpoint & sync trạng thái phòng

Mục tiêu: 6 lịch phòng (A–F) trên Google Calendar có sẵn các cuộc họp "ngày kín" →
quay được **painpoint** (phải dò từng lịch phòng), đồng thời **agent đọc đúng các lịch
này** nên trạng thái phòng/heatmap khớp 100% với Google Calendar.

## Bước 1 — Tạo Service Account (làm 1 lần, ~5 phút)
1. Vào https://console.cloud.google.com → tạo project (hoặc dùng project sẵn có).
2. **APIs & Services → Library** → tìm **Google Calendar API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account** → đặt tên → Done.
4. Mở Service account vừa tạo → tab **Keys → Add key → Create new key → JSON** → tải file về (đặt tên `sa.json`).
   - Ghi nhớ **email của service account** (dạng `xxx@yyy.iam.gserviceaccount.com`).

## Bước 2 — Tạo 6 lịch phòng + chia sẻ (tự động bằng script)
```bash
cd room-booking
pip install google-api-python-client google-auth
python gcal_setup.py create --creds sa.json --share EMAIL_CUA_BAN@gmail.com
```
- Script tạo 6 lịch "BookBird · Phòng A..F", chia sẻ cho email của bạn, và **in ra khối ENV**
  (`ROOM_A_CALENDAR_ID=...` → `ROOM_F_CALENDAR_ID=...`).
- Mở Google Calendar → **Thêm lịch → Đăng ký bằng ID** → dán từng ID để **thấy & quay**.

## Bước 3 — Seed kịch bản painpoint (ngày kín)
```bash
python gcal_setup.py seed --creds sa.json --date 2026-06-20
```
→ mỗi phòng đầy cuộc họp rải rác trong ngày, chỉ còn vài khe hiếm. Đây là cảnh để **quay painpoint**.
(Quay xong dọn: `python gcal_setup.py clear --creds sa.json --date 2026-06-20`.)

## Bước 4 — Bật Google mode cho agent
1. Lấy base64 của SA JSON (env AgentBase không nhận JSON nhiều dòng):
   ```bash
   python gcal_setup.py b64 --creds sa.json
   ```
2. Thêm vào `room-booking/.env.deploy`:
   ```
   GOOGLE_SA_CREDENTIALS_B64=<chuỗi base64 ở trên>
   ROOM_A_CALENDAR_ID=...
   ROOM_B_CALENDAR_ID=...
   ROOM_C_CALENDAR_ID=...
   ROOM_D_CALENDAR_ID=...
   ROOM_E_CALENDAR_ID=...
   ROOM_F_CALENDAR_ID=...
   ```
3. Deploy lại:
   ```bash
   bash .claude/skills/agentbase/scripts/cr.sh credentials docker-login
   bash .claude/skills/agentbase/scripts/runtime.sh update runtime-4285f206-2416-449b-bc80-0d7390f6869c \
     --image vcr.vngcloud.vn/111480-abp111912/room-booking:v52 --flavor runtime-s2-general-2x4 \
     --env-file room-booking/.env.deploy --from-cr
   ```
4. Kiểm tra: `GET /health` phải trả `"calendar_mode":"google"` (đang là `mock`).

## Bước 5 — Quay demo
1. **Cảnh painpoint**: mở Google Calendar hiển thị 6 lịch phòng ngày đã seed — kín mít, khó tìm khe chung. Voiceover: *"Muốn tìm 1 phòng trống phải mở từng lịch thế này…"*.
2. **Cắt sang BookBird**: hỏi *"đặt phòng 5 người chiều 20/06"* → agent quét, tránh trùng, gợi ý khe trống + giải thích 🧠. Trạng thái phòng/heatmap trên agent **khớp đúng** Google Calendar.
3. Đặt thử trùng giờ → agent **từ chối** (vì đọc đúng lịch bận thật).

> Lưu ý: khi đã bật Google mode, mọi đặt phòng qua agent sẽ **ghi event thật** lên các lịch
> này (thấy ngay trên Google Calendar) — rất hợp để quay "đặt xong hiện liền trên lịch".
> Muốn quay lại từ đầu: dùng `clear` rồi `seed` lại.
