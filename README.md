# 🕊️ BookBird — Meeting Room Booking Assistant

> **Chim Báo Bão** · Track: Chat Agent · Built & deployed on **GreenNode AgentBase**

Booking a meeting room usually takes several minutes: opening each room's calendar, checking capacity, hunting for a free slot everyone shares, and hand-writing invitation emails — often ending in double-bookings or forgotten recurring meetings.

**BookBird** is a chat- and voice-powered meeting room booking assistant that dramatically reduces this effort. Users simply describe their meeting needs via chat or voice, and the agent automatically:

- Filters available rooms based on capacity, operating hours, and the 14-day booking window
- Suggests meeting times that avoid conflicts across all participants' calendars
- Generates multilingual meeting invitation emails automatically
- Supports recurring meeting scheduling
- Sends pre-meeting reminders
- Learns individual user preferences and booking habits
- Handles meeting cancellations and rescheduling

**Value Proposition:** BookBird reduces the meeting room booking process from several minutes to just a few seconds, eliminates scheduling conflicts, and standardizes meeting invitations across teams and departments — all built and deployed on **GreenNode AgentBase**.

---

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env   # điền LLM_API_KEY, SMTP_*, (tuỳ chọn) Google Calendar
python main.py         # http://localhost:8080
```

Or with Docker:

```bash
docker build -t bookbird .
docker run -p 8080:8080 --env-file .env bookbird
```

## Configuration (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL` | ✅ | LLM (OpenAI-compatible, e.g. GreenNode MaaS) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | optional | Send real invitation emails (omit → skip sending) |
| `GOOGLE_SA_CREDENTIALS_B64` + `ROOM_A..F_CALENDAR_ID` | optional | Use real Google Calendar; omit → MOCK mode (in-memory) |
| `MEMORY_ID`, `MEMORY_STRATEGY_ID` | optional | Remember user habits via GreenNode Memory |

See [`GCAL-SETUP.md`](GCAL-SETUP.md) to connect Google Calendar, and [`SUBMISSION.md`](SUBMISSION.md) for the full description & demo script.

## Test

```bash
pip install pytest && pytest -q tests
```
