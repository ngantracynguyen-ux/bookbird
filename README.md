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

## 🤖 Model & Stack

| | |
|---|---|
| **LLM model** | `minimax/minimax-m2.5` — a reasoning model served via **GreenNode MaaS** (OpenAI-compatible API). Set with the `LLM_MODEL` env var; swappable for any OpenAI-compatible model. |
| **Agent framework** | LangChain `create_agent` + LangGraph (tool-calling agent, checkpointer for conversation memory) |
| **Backend** | FastAPI + Uvicorn · single-file embedded web UI |
| **Calendar** | Google Calendar API (service account) with in-memory MOCK fallback |
| **Email** | SMTP (real invitation emails) |
| **Memory** | GreenNode Memory (`AgentBaseMemoryEvents`) for per-user preferences |
| **Deploy** | Docker → GreenNode AgentBase runtime |

---

## 🧩 Skills & Capabilities

The agent exposes the following **skills (tools)** the LLM calls to get work done:

| Skill (tool) | What it does |
|--------------|--------------|
| `current_date` | Resolves "today / tomorrow / next week" and the 14-day booking window |
| `check_rooms` | Lists rooms free for a given date/time/capacity |
| `suggest_time` | **Explainable AI** — finds a slot free for *everyone*, scanning across days when full, and explains *why* (🧠 "considered busy calendars…") |
| `prepare_booking` | Builds a booking draft (auto-picks the smallest fitting room) for review |
| `book_recurring` | Schedules recurring meetings (daily/weekly/biweekly), holding the same room & dodging conflicts |
| `my_schedule` | Shows the user's upcoming meetings (day/week/month) |
| `book_like_last` | Smart default — suggests a booking from the user's habits |
| `cancel_booking` / `reschedule_booking` | Cancel or move a meeting — **only the organizer can edit their own bookings** |
| `compose_email` | AI-drafts the invitation body from the meeting purpose |
| `translate_email` | Translates the invite to English / Chinese / Japanese / Vietnamese |

**Platform & UX skills**

- 💬 **Chat + 🎤 voice** input (hands-free), **bilingual VI/EN**, mobile-friendly
- 📅 **Google Calendar** sync (company-wide shared rooms) with anti-double-booking — same room, overlapping time, *and* the organizer's own clashes
- 🛡️ **Ownership rules** — room status is shared across the company, but cancel/reschedule is limited to the booking's creator
- ✉️ **Real email** invites via SMTP · CC/BCC · custom signature · live preview
- 🔔 **In-app reminders** before upcoming meetings
- 🗺️ Room **availability heatmap**, ↩️ quote-reply on each message, 🕊️ animated mascot, 🎉 confetti on success
- 🧠 **Memory** — learns favorite room, frequent attendees, common purpose (GreenNode Memory)

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
