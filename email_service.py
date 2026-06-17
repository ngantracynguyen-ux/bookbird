"""Gửi mail xác nhận đặt phòng họp qua SMTP (Gmail/Office365).

Env vars (giống report-agent):
  SMTP_HOST      vd smtp.gmail.com
  SMTP_PORT      vd 587
  SMTP_USER      địa chỉ gửi
  SMTP_PASS      app password
  SMTP_USE_TLS   "true" (mặc định) — STARTTLS; "false" để dùng SSL trực tiếp
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


class EmailConfigError(Exception):
    pass


def _parse_addresses(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split(",") if a.strip()]


def _receiver_names(attendees: list[str]) -> str:
    """Suy tên người nhận từ email: 'an.nguyen@x.vn' -> 'An Nguyen'. Ghép tối đa 3 tên."""
    names = []
    for a in (attendees or []):
        local = a.split("@")[0]
        name = local.replace(".", " ").replace("_", " ").strip().title()
        if name:
            names.append(name)
    if not names:
        return "all"
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + " và các thành viên"


def _signature_domain(organizer: str = "") -> str:
    """Domain ký tên: ưu tiên ORG_DOMAIN, rồi domain của SMTP_USER / organizer."""
    dom = os.environ.get("ORG_DOMAIN", "").strip()
    if dom:
        return dom
    for src in (os.environ.get("SMTP_USER", ""), organizer):
        if "@" in src:
            return src.split("@", 1)[1]
    return "zalopay.vn"


def _get_smtp_config() -> dict[str, Any]:
    host = os.environ.get("SMTP_HOST", "").strip()
    port_str = os.environ.get("SMTP_PORT", "587").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"
    if not host:
        raise EmailConfigError("Chưa cấu hình SMTP_HOST")
    if not user:
        raise EmailConfigError("Chưa cấu hình SMTP_USER")
    if not password:
        raise EmailConfigError("Chưa cấu hình SMTP_PASS")
    try:
        port = int(port_str)
    except ValueError:
        raise EmailConfigError(f"SMTP_PORT không hợp lệ: {port_str}")
    return {"host": host, "port": port, "user": user, "password": password, "use_tls": use_tls}


def smtp_configured() -> bool:
    try:
        _get_smtp_config()
        return True
    except EmailConfigError:
        return False


def build_booking_html(booking: dict[str, Any]) -> str:
    """Tạo HTML mail xác nhận từ thông tin booking."""
    room = booking.get("room", "")
    capacity = booking.get("capacity", "")
    date = booking.get("date", "")
    start = booking.get("start_time", "")
    end = booking.get("end_time", "")
    purpose = booking.get("purpose", "")
    content = booking.get("content", "")
    body_note = booking.get("note", "")
    online_link = booking.get("online_link", "")
    docs_link = booking.get("docs_link", "")
    diagram_url = booking.get("diagram_url", "")
    attendees = booking.get("attendees", [])

    def row(label, value):
        if not value:
            return ""
        return f'<tr><td style="padding:8px 12px;color:#888;width:140px;vertical-align:top">{label}</td><td style="padding:8px 12px;color:#222">{value}</td></tr>'

    link_html = ""
    if online_link:
        link_html += f'<a href="{online_link}" style="color:#1a73e8">Tham dự online</a>'
    if docs_link:
        if link_html:
            link_html += " &nbsp;|&nbsp; "
        link_html += f'<a href="{docs_link}" style="color:#1a73e8">Tài liệu</a>'

    diagram_html = f'<img src="{diagram_url}" alt="Sơ đồ phòng" style="max-width:100%;border-radius:8px;margin-top:8px">' if diagram_url else ""
    attendees_html = ", ".join(attendees) if attendees else ""

    # --- soạn mail chuẩn: Hi <receiver> / Body / Best regards, <domain> ---
    greeting = f"Hi {_receiver_names(attendees)},"
    org_name = os.environ.get("ORG_NAME", "ZaloPay Accounting/Finance").strip()
    domain = _signature_domain(booking.get("organizer", ""))
    custom_message = booking.get("message", "").strip()
    if custom_message:
        # body do người dùng tự soạn (mỗi dòng = 1 đoạn)
        body_lines = [ln for ln in custom_message.splitlines() if ln.strip()]
    else:
        body_lines = [
            f"Bạn được mời tham dự cuộc họp <strong>{purpose or 'cuộc họp'}</strong>.",
            f"Cuộc họp diễn ra ngày <strong>{date}</strong>, lúc <strong>{start}–{end}</strong> tại <strong>phòng {room}</strong> (sức chứa {capacity}).",
        ]
        if content:
            body_lines.append(f"Nội dung: {content}.")
        if body_note:
            body_lines.append(body_note)
    body_html = "".join(f'<p style="margin:0 0 10px;line-height:1.6">{b}</p>' for b in body_lines)

    # chữ ký: dùng chữ ký tuỳ chỉnh nếu có, ngược lại dùng mặc định "Best regards, ORG, domain"
    custom_sig = booking.get("signature", "").strip()
    organizer = booking.get("organizer", "").strip()
    if custom_sig:
        sig_lines = "<br>".join(ln.strip() for ln in custom_sig.splitlines() if ln.strip())
        signature_html = f'<p style="margin:18px 0 0;line-height:1.5">{sig_lines}</p>'
    elif organizer and "@" in organizer:
        signature_html = (
            f'<p style="margin:18px 0 0;line-height:1.5">Best regards,<br>'
            f'<span style="color:#555">{organizer}</span></p>'
        )
    else:
        signature_html = (
            f'<p style="margin:18px 0 0;line-height:1.5">Best regards,<br>'
            f'<strong>{org_name}</strong><br>'
            f'<span style="color:#888;font-size:13px">{domain}</span></p>'
        )

    return f"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#eef4fc;margin:0;padding:24px;color:#1a3a5c">
  <div style="max-width:640px;margin:0 auto;background:#f6faff;border:1px solid #d8e8fb;border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(26,115,232,.12)">
    <div style="background:linear-gradient(135deg,#eaf3ff,#dcebff);padding:22px 28px;border-bottom:1px solid #d8e8fb">
      <h1 style="font-size:20px;margin:0 0 4px;color:#0d47a1;font-weight:600">🕊️ Lời mời họp · Phòng {room}</h1>
      <p style="margin:0;font-size:13px;color:#4a7bb0">{date} · {start}–{end}</p>
    </div>
    <div style="padding:22px 28px;background:#ffffff">
      <p style="margin:0 0 14px;font-size:15px;font-weight:600;color:#0d47a1">{greeting}</p>
      {body_html}
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin:14px 0">
        {row("Phòng", f"{room} (sức chứa {capacity})")}
        {row("Thời gian", f"{date}, {start} – {end}")}
        {row("Mục đích", purpose)}
        {row("Người tham dự", attendees_html)}
        {row("Liên kết", link_html)}
      </table>
      {diagram_html}
      {signature_html}
    </div>
    <div style="background:#f6faff;padding:14px 28px;font-size:12px;color:#9bb8e0;border-top:1px solid #e3edfb">
      Mail gửi tự động từ <strong style="color:#1a73e8">BookBird</strong> · Trợ lý đặt phòng họp.
    </div>
  </div>
</body></html>"""


def send_booking_email(booking: dict[str, Any]) -> dict[str, list[str]]:
    """Gửi mail xác nhận. attendees = danh sách To. Trả về {to: [...]}."""
    cfg = _get_smtp_config()
    to_list = booking.get("attendees", [])
    cc_list = booking.get("cc", []) or []
    bcc_list = booking.get("bcc", []) or []
    if not to_list:
        raise EmailConfigError("Chưa có người tham dự (To) để gửi mail")

    subject = f"[Đặt phòng] Phòng {booking.get('room','')} — {booking.get('date','')} {booking.get('start_time','')}–{booking.get('end_time','')}"
    html_body = build_booking_html(booking)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    all_rcpts = to_list + cc_list + bcc_list
    context = ssl.create_default_context()
    if cfg["use_tls"]:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], all_rcpts, msg.as_string())
    else:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context) as server:
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["user"], all_rcpts, msg.as_string())

    return {"to": to_list, "cc": cc_list, "bcc": bcc_list}
