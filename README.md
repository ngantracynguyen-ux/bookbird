# Room Booking Agent

Trợ lý đặt phòng họp qua web chat. Kiểm tra điều kiện phòng → gợi ý phòng trống → soạn mail → review → gửi xác nhận.

## Phòng & quy tắc
| Phòng | Sức chứa | Loại |
|-------|----------|------|
| A, B | 4 | meeting |
| C, D | 6 | meeting |
| E | 10 | meeting |
| F | 30 | training |

- Giờ hoạt động: **8:00–18:00**
- Đặt trước tối đa: **14 ngày**
- Trạng thái: `available` / `busy`

## Workflow
1. Nhận yêu cầu đặt phòng (web chat).
2. Kiểm tra: trạng thái (rảnh/bận qua Google Calendar), sức chứa, khung giờ, hạn 14 ngày.
3. Gợi ý phòng thỏa điều kiện.
4. Thu thập: phòng, email người dự, mục đích, nội dung, link online/docs.
5. Tạo **thẻ review** (popup) — người dùng kiểm tra.
6. Bấm **Xác nhận & gửi mail** → tạo event Calendar + gửi mail.

## Chế độ MOCK
Khi `GOOGLE_SA_CREDENTIALS` trống, agent dùng bộ nhớ tạm (in-memory) để demo toàn bộ luồng. Khi điền credential service account + calendar ID của từng phòng → tự chuyển sang Google Calendar thật.

### Bật Google Calendar thật
1. Tạo service account trên Google Cloud, bật Calendar API, tải JSON key.
2. Tạo 6 Google Calendar (mỗi phòng 1 cái), share cho email service account quyền **"Make changes to events"**.
3. Điền `GOOGLE_SA_CREDENTIALS` (nội dung JSON hoặc path) và `ROOM_<X>_CALENDAR_ID`.

## Chạy local
```bash
docker build -t room-booking .
docker run -p 8080:8080 --env-file .env room-booking
# mở http://localhost:8080
```

## Endpoints
- `GET /` — giao diện chat
- `POST /invocations` — chat (header `X-GreenNode-AgentBase-User-Id`, `-Session-Id`)
- `POST /confirm` — xác nhận đặt phòng `{ "draft_id": "..." }`
- `GET /rooms?date&start_time&end_time` — card tình trạng phòng
- `GET /health` — health + chế độ calendar
