# BookBird — Test cases

## A. Test tự động (pytest)

Không cần mạng, dùng MOCK calendar + LLM giả. Tập trung vào bug "tool lỗi → agent báo *hệ thống lỗi*".

```bash
# Cách 1: có Python sẵn
cd room-booking && pip install pytest && pytest -v

# Cách 2: qua Docker (không cần cài Python)
docker run --rm -v "<repo>/room-booking:/app" -w /app python:3.13-slim \
  bash -c "pip install -q -r requirements.txt pytest && pytest -v tests"
```

12 test trong `tests/test_agent.py`:

| Nhóm | Test | Kỳ vọng |
|------|------|---------|
| Happy path | `build_draft_happy`, `prepare_booking_tool_returns_review_marker` | Tạo được draft + thẻ review |
| Không đặt được (lý do rõ, không crash) | `room_busy…`, `out_of_hours…`, `beyond_14_days…`, `auto_over_capacity…`, `invalid_date…` | Trả lý do cụ thể, **không** raise |
| **Bug chính** | `compose_email_falls_back…`, `translate_email_falls_back…`, `prepare_booking_guards…`, `book_recurring_guards…` | LLM/tool trục trặc → degrade nhẹ nhàng, **không** chứa chữ "lỗi"/"IT" |
| Chống đặt trùng | `commit_then_room_busy` | Khung đã đặt → phòng báo bận |

## B. Test thủ công trên UI (cho demo/QA)

> Mở app → **Trải nghiệm khách** → bấm **🎬 Tạo data demo** để có sẵn ngày kín.

| # | Thao tác | Kết quả đúng | Bug đã sửa |
|---|----------|--------------|------------|
| 1 | "Đặt phòng A ngày mai 9h-10h cho 3 người, mời a@vng.com.vn, họp team" | Hỏi nốt nội dung → tạo **thẻ review** | |
| 2 | Bấm **Xác nhận** trên thẻ review | Toast "Đã giữ phòng…" + (nếu có SMTP) gửi mail | |
| 3 | Đặt lại đúng phòng/giờ vừa đặt | "Phòng A đã bận… gợi ý B/C/D/E hoặc đổi giờ" — **KHÔNG** nói "hệ thống lỗi/gọi IT" | ✅ |
| 4 | Đặt giờ 7h sáng (ngoài giờ) | Báo ngoài giờ 8–18h, gợi ý khung hợp lệ | ✅ |
| 5 | Đặt cho 40 người | Báo vượt sức chứa, gợi ý phòng F (30) hoặc tách nhóm | ✅ |
| 6 | "Soạn mail mời họp review" rồi "đặt luôn" | Soạn được thân mail; nếu LLM chập chờn vẫn ra mẫu mail dùng được (không văng lỗi) | ✅ |
| 7 | "Dịch nội dung mail sang tiếng Trung" | Ra bản dịch; nếu lỗi tạm thời thì giữ nguyên văn (không báo lỗi) | ✅ |
| 8 | Họp định kỳ: "đặt phòng họp mỗi tuần 4 buổi, thứ 2 9h" | Thẻ review chuỗi định kỳ, né phòng khi trùng | |
| 9 | Bấm **↩ Trả lời** trên 1 bong bóng agent → gõ câu trả lời | Câu hỏi của agent được trích trong dấu " ", agent hiểu là trả lời tiếp nối | |

## C. Smoke test endpoint (live)

```bash
curl -s https://<endpoint>/health        # {"status":"HEALTHY","calendar_mode":"mock"}
curl -s https://<endpoint>/rooms          # danh sách 6 phòng
```
