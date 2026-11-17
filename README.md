# ZIQU Python Backend (FastAPI + SQLite + WebSockets)

Complete self-hosted Python backend powering all ZIQU Android features without Firebase:
- **Authentication**: Register, Login (Email or `@username`), Username availability check & change, Forgot/Reset Password (6-digit code + optional SMTP), JWT Bearer tokens, PBKDF2-SHA256 password hashing.
- **Posts & Reels**: Upload images/videos to local server `/media/...`, Feed, Reels feed, Explore, Delete post, Watch-time/Completion/Rewatch/Share metrics.
- **Interactions**: Like/Unlike toggle, Save/Unsave posts, Nested comments & replies with delete cascade.
- **Stories**: 24-hour expiring stories, text overlay transforms & stickers, Reel-to-story sharing, viewers list, emoji reactions.
- **Follow System**: Follow/Unfollow, Followers & Following lists, Remove follower, Automatic non-negative counters.
- **Real-time Chat & Groups**: 1-on-1 DMs, Typing indicators, Online/Last-seen presence, Read receipts (`seen`), Message reactions, Forward, Unsend / Delete-for-me, Multi-admin Group Chats, System event messages, WebSocket `/ws` live delivery.
- **Notifications & Push Sync**: Activity feed notifications + background Worker polling/ack queue.
- **Safety & Moderation**: Bidirectional Block/Unblock enforcement + User/Content reporting.

---

## Quick Start

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # Optional: customize SECRET_KEY / PORT / SMTP
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Verify server is running:
- Health Check: `http://localhost:8000/health`
- Interactive Swagger API Docs: `http://localhost:8000/docs`

## Connecting the ZIQU Android App

- **Android Emulator**: Default URL `http://10.0.2.2:8000/` connects automatically to `localhost:8000` on your host machine.
- **Physical Phone (same Wi-Fi)**: Find your computer's LAN IP (e.g. `192.168.1.25`), tap the Server Settings icon on the Login screen (or in Profile menu), and enter `http://192.168.1.25:8000/`.
- **Production Cloud (VPS / Render / Railway)**: Deploy this `backend/` folder with start command `uvicorn main:app --host 0.0.0.0 --port $PORT` and paste your `https://...` URL in the app's Server Settings.
