"""Test cho BookBird — tập trung vào BUG: tool gặp lỗi → KHÔNG được báo "hệ thống lỗi".

Chạy (không cần mạng, dùng MOCK calendar + LLM giả):
    cd room-booking
    pip install pytest
    pytest -v
Hoặc qua Docker (không cần cài Python máy):
    docker run --rm -v "<repo>/room-booking:/app" -w /app python:3.13-slim \
        bash -c "pip install -q -r requirements.txt pytest && pytest -v tests"
"""
import os
import sys
from datetime import timedelta

# --- env giả để import main mà không cần LLM/Google thật (ép MOCK calendar) ---
os.environ.setdefault("LLM_MODEL", "test/model")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.pop("GOOGLE_SA_CREDENTIALS", None)
os.environ.pop("GOOGLE_SA_CREDENTIALS_B64", None)
os.environ.pop("SMTP_HOST", None)  # tắt gửi mail thật khi test

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import main  # noqa: E402
import booking as bk  # noqa: E402
import calendar_service as cal  # noqa: E402


def _date(offset_days=1):
    """Ngày hợp lệ trong hạn 14 ngày (mặc định ngày mai)."""
    return (bk.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _clean_mock():
    """Xoá booking MOCK trước mỗi test để không dính trạng thái chéo."""
    for r in list(cal._mock_bookings.keys()):
        cal._mock_bookings[r] = []
    main._current_user.set("tester@vng.com.vn")
    yield


def _call(tool, **kwargs):
    """Gọi 1 LangChain @tool và trả về chuỗi kết quả."""
    return tool.invoke(kwargs)


# ============ 1) Luồng đặt phòng cơ bản (happy path) ============
def test_build_draft_happy():
    payload, err = main.build_draft("A", _date(), "09:00", "10:00",
                                    ["an.nguyen@vng.com.vn"], "Họp team", num_people=2)
    assert err is None
    assert payload["room"] == "A"
    assert payload["start_time"] == "09:00" and payload["end_time"] == "10:00"
    assert "draft_id" in payload


def test_build_draft_auto_adds_user_email():
    payload, err = main.build_draft("A", _date(), "09:00", "10:00", [], "Họp",
                                    num_people=1, organizer="me@vng.com.vn")
    assert err is None
    assert "me@vng.com.vn" in payload["attendees"]   # luôn thêm email user


def test_build_draft_no_attendees_ok_for_guest():
    # khách (user_id không phải email) → không thêm, vẫn tạo được draft (email không bắt buộc)
    payload, err = main.build_draft("A", _date(), "09:00", "10:00", [], "Họp",
                                    num_people=1, organizer="guest-123")
    assert err is None and payload["attendees"] == []


def test_prepare_booking_tool_returns_review_marker():
    out = _call(main.prepare_booking, room="A", date=_date(), start_time="09:00",
                end_time="10:00", attendees=["a@vng.com.vn"], purpose="Họp team")
    assert main.REVIEW_START in out and main.REVIEW_END in out


# ============ 2) Các trường hợp KHÔNG đặt được → phải có lý do rõ, KHÔNG crash ============
def test_room_busy_returns_friendly_error_not_exception():
    d = _date()
    cal.create_event("A", bk.parse_dt(d, "09:00"), bk.parse_dt(d, "10:00"), "Đã có họp")
    payload, err = main.build_draft("A", d, "09:00", "10:00", ["a@vng.com.vn"], "Họp", num_people=2)
    assert payload is None
    assert err and "BẬN" in err  # nêu rõ phòng bận, không phải lỗi hệ thống


def test_out_of_hours_returns_error():
    payload, err = main.build_draft("A", _date(), "07:00", "08:00", ["a@vng.com.vn"], "Họp")
    assert payload is None and err  # ngoài giờ 8–18h


def test_beyond_14_days_returns_error():
    payload, err = main.build_draft("A", _date(30), "09:00", "10:00", ["a@vng.com.vn"], "Họp")
    assert payload is None and err  # quá hạn 14 ngày


def test_auto_over_capacity_returns_error_not_exception():
    # 99 người vượt sức chứa mọi phòng → trả lý do, KHÔNG raise
    payload, err = main.build_draft("AUTO", _date(), "09:00", "10:00", [], "Họp", num_people=99)
    assert payload is None and err


def test_invalid_date_returns_error():
    payload, err = main.build_draft("AUTO", "ngày-mai", "09:00", "10:00", [], "Họp", num_people=1)
    assert payload is None and err


# ============ 3) BUG CHÍNH: LLM trục trặc trong tool → degrade, KHÔNG báo "hệ thống lỗi" ============
def test_compose_email_falls_back_when_llm_fails(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("LLM timeout / 503")
    monkeypatch.setattr(main, "_llm_compose_mail", _boom)
    out = _call(main.compose_email, purpose="Họp review", content="Sprint 12")
    assert isinstance(out, str) and out.strip()
    low = out.lower()
    assert "lỗi" not in low and "error" not in low  # không lộ lỗi hệ thống
    assert "review" in low or "họp" in low          # vẫn là thân mail dùng được


def test_translate_email_falls_back_to_original_when_llm_fails(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(main, "_llm_translate", _boom)
    src = "Mời anh chị tham dự cuộc họp."
    out = _call(main.translate_email, text=src, target_language="en")
    assert out == src  # trả nguyên văn thay vì văng lỗi


def test_prepare_booking_guards_unexpected_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(main, "build_draft", _boom)
    out = _call(main.prepare_booking, room="A", date=_date(), start_time="09:00",
                end_time="10:00", attendees=["a@vng.com.vn"], purpose="Họp")
    low = out.lower()
    assert "kiểm tra" in low  # hướng dẫn người dùng, không báo lỗi hệ thống
    assert "it" not in low.split()  # không bảo "gọi IT"


def test_book_recurring_guards_unexpected_exception(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(main, "build_series", _boom)
    out = _call(main.book_recurring, pattern="weekly", date=_date(), start_time="09:00",
                end_time="10:00", attendees=["a@vng.com.vn"], purpose="Họp", count=4)
    assert "định kỳ" in out.lower()


# ============ 4) Đặt được rồi thì phòng phải bận với lần đặt sau (chống đặt trùng) ============
def test_organizer_double_book_blocked_at_prepare():
    """Người đặt đã có họp lúc 9-10 (phòng C) → đặt phòng KHÁC (B) cùng giờ phải bị chặn."""
    d = _date()
    org = "tester@vng.com.vn"
    cal.create_event("C", bk.parse_dt(d, "09:00"), bk.parse_dt(d, "10:00"), "Họp cũ", organizer=org)
    payload, err = main.build_draft("B", d, "09:00", "10:00", ["x@vng.com.vn"], "Họp mới",
                                    num_people=2, organizer=org)
    assert payload is None
    assert err and "trùng" in err.lower()


def test_organizer_double_book_blocked_at_commit():
    d = _date()
    org = "tester@vng.com.vn"
    cal.create_event("C", bk.parse_dt(d, "09:00"), bk.parse_dt(d, "10:00"), "Họp cũ", organizer=org)
    ok = main._commit_one("B", d, "09:00", "10:00",
                          {"attendees": ["x@vng.com.vn"], "purpose": "Họp mới", "organizer": org})
    assert ok is False


def test_looks_like_fake_booking_detects_narration():
    # model BỊA xác nhận (cần ép gọi tool)
    assert main._looks_like_fake_booking("Đã tạo bản xem trước cho bạn. Thông tin đặt phòng:")
    assert main._looks_like_fake_booking("- **Phòng:** A\n- **Giờ:** 09:00 - 10:00")
    assert main._looks_like_fake_booking("Bạn xác nhận đặt luôn nhé? 👆")
    # markdown ** ** chèn vào giữa vẫn phải bắt được
    assert main._looks_like_fake_booking("Mọi thông tin đã sẵn sàng! Bạn bấm **Xác nhận** trên giao diện để mình gửi mail nhé!")
    # KHÔNG phải bịa: lời từ chối / hỏi thêm
    assert not main._looks_like_fake_booking("Phòng E đã bận khung đó, bạn đổi giờ khác nhé?")
    assert not main._looks_like_fake_booking("Bạn cần đặt phòng cho mấy người?")
    assert not main._looks_like_fake_booking("")


def test_salvage_builds_real_draft_from_narrated_text():
    ds = _date(8)
    text = (f"Thông tin đặt phòng:\n- Phòng: C\n- Ngày: {ds}\n- Giờ: 10:00 - 11:00\n"
            "- Người tham dự: an@vng.com.vn\n- Mục đích: họp team\n- Nội dung: review tuần")
    payload, err = main._salvage_booking_from_text(text, "tester@vng.com.vn")
    assert err is None and payload is not None
    assert payload["room"] == "C" and payload["date"] == ds
    assert payload["start_time"] == "10:00" and payload["end_time"] == "11:00"
    assert "draft_id" in payload


def test_salvage_detects_conflict_in_narrated_text():
    ds = _date(8)
    cal.create_event("C", bk.parse_dt(ds, "10:00"), bk.parse_dt(ds, "11:00"), "Đã có họp")
    text = f"Phòng: C\nNgày: {ds}\nGiờ: 10h-11h\nMục đích: x"
    payload, err = main._salvage_booking_from_text(text, "tester@vng.com.vn")
    assert payload is None and err  # phải phát hiện trùng, không tạo draft


def test_salvage_none_when_unparseable():
    payload, err = main._salvage_booking_from_text("Chào bạn, bạn cần đặt phòng cho mấy người?", "x@vng.com.vn")
    assert payload is None and err is None


def test_strip_review_removes_marker_from_display():
    raw = "Đây nhé: " + main.REVIEW_START + '{"draft_id":"x","room":"A"}' + main.REVIEW_END
    out = main._strip_review(raw)
    assert "BOOKING_REVIEW" not in out      # marker không bao giờ lộ ra text
    assert out.startswith("Đây nhé")


def test_strip_thinking_removes_block_and_toi():
    raw = "<think>Người dùng chào. Tôi nên chào lại.</think>\n\nChào bạn! 👋 Mình là BookBird."
    out = main._strip_thinking(raw)
    assert "<think" not in out and "</think" not in out
    assert "Tôi nên" not in out            # bỏ luôn phần xưng 'tôi' trong suy nghĩ
    assert out.startswith("Chào bạn!")


def test_strip_thinking_handles_unclosed_and_empty():
    assert main._strip_thinking("<think>bị cắt giữa chừng") == ""
    assert main._strip_thinking("") == ""
    assert main._strip_thinking("Chào bạn!") == "Chào bạn!"   # không có think → giữ nguyên


def test_commit_then_room_busy():
    d = _date()
    data = {"attendees": ["a@vng.com.vn"], "purpose": "Họp", "organizer": "tester@vng.com.vn"}
    assert main._commit_one("B", d, "14:00", "15:00", data) is True
    # lần 2 cùng khung → không còn trống
    assert cal.is_available("B", bk.parse_dt(d, "14:00"), bk.parse_dt(d, "15:00")) is False
