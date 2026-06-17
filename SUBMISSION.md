# Submission — BookBird (Room Booking Agent) · Team Chim Báo Bão

**Track:** Chat Agent
**Platform:** GreenNode AgentBase
**Project link:** https://endpoint-e47acb1a-02e9-4dea-a7ae-a03dfe7ff260.agentbase-runtime.aiplatform.vngcloud.vn
**Team:** Chim Báo Bão — **BU:** Zalopay
**Members:** _<điền danh sách thành viên>_
**Demo video:** _<điền link video 2–3 phút>_

---

## Caption showcase (ngắn — chọn 1 dùng cho landing page)

**Tagline chính:** ⭐ **Nói một câu, có ngay phòng họp.**
*(phương án khác: "Đặt phòng họp trong 10 giây.")*

**Caption ngắn (VI, ~30 từ):**
> 🕊️ **Chim Báo Bão** — đặt phòng họp bằng chat & giọng nói trong 10 giây. Tự tìm phòng, chống trùng lịch, gửi mail mời, nhắc giờ họp, đặt cả họp định kỳ. Song ngữ Việt–Anh, chạy trên GreenNode AgentBase.

**Caption ngắn (EN, ~30 từ):**
> 🕊️ **Chim Báo Bão** — book a meeting room by chat or voice in 10 seconds. Finds the room, prevents clashes, emails the invite, reminds you, handles recurring meetings. Bilingual, on GreenNode AgentBase.

**Bản siêu ngắn (1 dòng):** Trợ lý đặt phòng họp bằng giọng nói — 10 giây, không trùng lịch, tự gửi mail mời. 🕊️

**🎉 Vui vẻ (VI) — ⭐ caption chính dùng cho showcase:**
> 🕊️ **Chim Báo Bão** đây! Cần phòng họp? Chỉ cần câu nói, mình sẽ tìm phòng, giữ chỗ, gửi mail mời. Không lo dò lịch, chốt phòng nhẹ tênh! 🙌✨

**🎉 Fun (EN):**
> 🕊️ **Chim Báo Bão** here! Need a meeting room? Just say the word — I'll find the room, hold it, and send the invite. No more calendar hunting, room locked in a snap! 🙌✨

---

## ✨ Tính năng (bản mới nhất)

**Đặt phòng thông minh**
- Đặt bằng **chat hoặc giọng nói** (voice hands-free — nói xong tự gửi).
- Tự lọc 6 phòng theo **sức chứa, giờ hoạt động (8–18h), hạn đặt 14 ngày**.
- **🧠 Gợi ý giờ "biết nghĩ"**: tránh trùng lịch của cả người mời lẫn người dự, quét nhiều ngày khi kín, **giải thích vì sao** chọn khung đó.
- **🛡️ Chống đặt trùng chắc chắn**: chặn trùng phòng (kể cả **đè giờ một phần**) **và** trùng giờ của chính người đặt; kiểm tra **tất định ở phía server** (không phụ thuộc model nên không bao giờ lọt).
- **Họp định kỳ** (hàng ngày/tuần/2 tuần): giữ cùng phòng, tự né khi trùng.
- **Huỷ / dời** lịch một chạm.

**Email & nhắc lịch**
- **Gửi mail mời thật** qua SMTP (không giả lập).
- **AI soạn nội dung mail** theo mục đích họp; **dịch Anh / Trung / Nhật**; chữ ký tuỳ chỉnh; CC/BCC; xem trước.
- Mail xác nhận thiết kế đồng bộ thẻ hero (đẹp, nhận diện thương hiệu).
- **🔔 Nhắc lịch họp** trong app (toast trước giờ họp 30 phút).

**Trải nghiệm & cá nhân hoá**
- **🕊️ Mascot BookBird** hải âu đập cánh, chào theo ngữ cảnh — tăng cảm tình.
- **↩ Trả lời trích dẫn** từng tin nhắn của agent → giữ mạch hội thoại, không hiểu nhầm.
- Chào theo **tên người dùng**; **ghi nhớ thói quen** (phòng hay dùng, người hay mời) để prefill.
- **Bản đồ nhiệt tình trạng phòng** theo từng giờ; danh bạ người nhận gõ-là-gợi-ý.
- **Song ngữ Việt–Anh**, responsive trên điện thoại.
- Đăng nhập email/tên hoặc **Google Sign-In**; **chế độ khách** + nút **tạo data demo**.

**Nền tảng:** triển khai trên **GreenNode AgentBase** (runtime + Memory để cá nhân hoá theo người dùng).

---

## Mô tả (Tiếng Việt — ~180 từ)

Mỗi lần đặt một phòng họp, nhân viên mất 5–10 phút: mở lịch từng phòng, nhẩm sức chứa, canh khung giờ trống, rồi tự soạn mail mời người tham dự — và vẫn thường xuyên bị trùng lịch hoặc quên các buổi họp định kỳ. Nhân với cả phòng ban mỗi ngày, đó là hàng giờ công bị lãng phí.

**Chim Báo Bão** là trợ lý đặt phòng họp bằng **chat và giọng nói**, đặt xong một phòng chỉ trong 10 giây. Người dùng chỉ cần nói nhu cầu ("đặt phòng cho 5 người chiều mai"); agent tự lọc phòng theo sức chứa, giờ hoạt động và hạn 14 ngày, **gợi ý giờ tránh trùng lịch của mọi người và giải thích vì sao**, **chống đặt trùng chắc chắn**, rồi **tự gửi mail mời thật**. Agent còn đặt **họp định kỳ**, **nhắc trước giờ họp**, **ghi nhớ thói quen** từng người, hỗ trợ **huỷ/dời lịch**, **soạn & dịch mail (Anh–Trung–Nhật)** và **song ngữ Việt–Anh**.

**Giá trị:** rút việc đặt phòng từ vài phút xuống vài giây, **xoá trùng lịch**, và chuẩn hoá thư mời họp cho toàn phòng ban — triển khai sẵn trên GreenNode AgentBase.

## Description (English — ~165 words)

Every room booking costs an employee 5–10 minutes: opening each room's calendar, guessing capacity, hunting for a free slot, then hand-writing the invite — and still ending up with double-bookings or forgotten recurring meetings. Across a whole department, that's hours wasted daily.

**Chim Báo Bão** is a **chat- and voice-based** meeting-room assistant that books a room in 10 seconds. Users simply state their need ("book a room for 5 people tomorrow afternoon"); the agent filters rooms by capacity, working hours and the 14-day window, **suggests a clash-free slot for everyone and explains why**, **prevents double-bookings reliably**, and **emails the real invite**. It also schedules **recurring meetings**, **sends reminders**, **learns each user's habits**, handles **cancel/reschedule**, **drafts & translates invites (EN–ZH–JA)**, and works in **both Vietnamese and English**.

**Value:** turns a multi-minute chore into seconds, eliminates double-bookings, and standardizes invites department-wide — already deployed on GreenNode AgentBase.

---

## Kịch bản demo video (~2 phút 40) — bản tối ưu cho voting

> Cao trào = **"agent biết suy nghĩ"** (ngày kín → tự tìm ngày khác + giải thích) → **chống trùng** → **confetti** → **mail thật về inbox**. Mỗi cảnh có lời thoại (voiceover) + thao tác.

**Cảnh 1 — Painpoint (0:00–0:15)**
- 🎙️ *"Đặt phòng họp tưởng nhanh, mà thực tế: mở lịch từng phòng, canh sức chứa, dò giờ ai cũng rảnh, rồi gõ mail mời… vẫn trùng. Cả phòng ban mỗi ngày — mất hàng giờ."*
- 🎬 Cảnh nhiều tab lịch rối.

**Cảnh 2 — Gặp BookBird (0:15–0:33)**
- 🎙️ *"BookBird — trợ lý đặt phòng bằng chat và giọng nói. Không cần đăng nhập, bấm Trải nghiệm là chạy."*
- 🎬 Mở app → hero animation chạy → **mascot hải âu đập cánh ở góc** chào → bấm **"Trải nghiệm khách"** → dashboard có sẵn dữ liệu.

**Cảnh 3 — ⭐ MAGIC MOMENT: agent biết suy nghĩ (0:33–1:15)**
- 🎙️ *"Hôm nay phòng kín hết. Nhưng BookBird không bó tay — nó quét cả tuần, tránh lịch bận của mọi người, và nói rõ vì sao."*
- 🎬 Bấm **"🎬 Tạo data demo"** → **bản đồ nhiệt** đỏ rực (phòng bận) → gửi câu hỏi → agent trả lời kèm dòng **"🧠 Mình đã cân nhắc lịch bận: …"** → **dừng nhấn mạnh dòng 🧠**.

**Cảnh 4 — Chống trùng + chốt phòng + ăn mừng (1:15–1:50)**
- 🎙️ *"Thử đặt trùng giờ xem — BookBird từ chối ngay. Chọn khung trống, nó giữ chỗ, soạn mail mời và gửi thật trong vài giây."*
- 🎬 Đặt trùng 1 phòng đã bận → agent **từ chối + gợi ý phòng khác** → chọn khung trống → **thẻ Xác nhận** → "Xem trước mail" → **Xác nhận** → **🎉 confetti** → cắt sang **inbox Gmail thật**.

**Cảnh 5 — AI email đa ngôn ngữ + voice (1:50–2:15)**
- 🎙️ *"Email do AI soạn theo mục đích, dịch được Anh – Trung – Nhật. Và bạn có thể ra lệnh bằng giọng nói."*
- 🎬 Form đặt phòng → **"✨ Soạn tự động"** → chọn 中文 → **"🌐 Dịch"**. Bấm mic nói 1 câu → agent tự xử lý.

**Cảnh 6 — Nhanh gọn phần còn lại (2:15–2:30)**
- 🎙️ *"Họp định kỳ tự giữ phòng, nhắc trước giờ họp, huỷ/dời một chạm, song ngữ, dùng tốt trên điện thoại."*
- 🎬 Lướt: chuỗi định kỳ → toast **nhắc họp** → Huỷ/Dời → bấm **EN** → nhịp mobile (mascot vẫn đập cánh).

**Cảnh 7 — Chốt (2:30–2:40)**
- 🎙️ *"BookBird — nói một câu, có ngay phòng họp. Không lo dò lịch, chốt phòng nhẹ tênh."*
- 🎬 Logo BookBird + footer "Track Chat Agent · GreenNode AgentBase · Chim Báo Bão".

**Tip quay (để ăn vote):**
- **Cảnh 3 + 4 là vàng** — để người xem ĐỌC được dòng "🧠" và thấy agent **từ chối đặt trùng**. Đây là thứ khác biệt nhất.
- Cảnh 4: nhớ **cắt sang inbox thật** — bằng chứng "không giả lập".
- Dùng **chế độ khách + nút Tạo data demo** để có sẵn bối cảnh.
- Quay desktop cho rõ, chèn 1 nhịp mobile. Bật sẵn micro. Tổng ≤ 3 phút.

> Kịch bản chi tiết để bỏ vào app tạo clip AI: xem `DEMO-CLIP.md`.
