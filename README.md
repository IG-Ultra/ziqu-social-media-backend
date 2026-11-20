# ZIQU Python Backend (FastAPI + PostgreSQL/SQLite + Cloudinary + WebSockets, Render-Ready)

Complete self-hosted Python backend powering all ZIQU Android features without Firebase:
- **Permanent Production Database (PostgreSQL + Connection Pooling)**: Automatically uses PostgreSQL (`psycopg_pool`) whenever `DATABASE_URL` is configured (works with Neon.tech free tier, Supabase, or Render PostgreSQL) so accounts, posts, reels, chats, follows, and reports **never wipe on Render Free restarts/redeploys**. Automatically falls back to local SQLite WAL mode for local dev.
- **Permanent Cloud Media Hosting (Cloudinary + Local Fallback)**: Automatically uploads photos, avatars, stories, and reels to Cloudinary CDN when `CLOUDINARY_URL` is set so media links never disappear on container restarts.
- **Persistent Real-Time WebSockets (`/ws`)**: Bidirectional FastAPI WebSocket endpoint supporting instant 1-on-1 & group messaging, `ws_ack` delivery confirmations, live typing indicators, read receipts (`seen`), reactions, online/last-seen presence, notifications, and Render reverse-proxy heartbeat keep-alives.
- **Authentication**: Register, Login (Email or `@username`), Username availability check & change, Forgot/Reset Password (Resend/Brevo/SMTP email links & codes), JWT Bearer tokens, PBKDF2-SHA256 password hashing.
- **Posts & Reels**: Upload images/videos, Feed, Reels feed, Explore, Delete post, Watch-time/Completion/Rewatch/Share metrics.
- **Interactions**: Like/Unlike toggle, Save/Unsave posts, Nested comments & replies with delete cascade.
- **Stories**: 24-hour expiring stories, text overlay transforms & stickers, Reel-to-story sharing, viewers list, emoji reactions.
- **Follow System**: Follow/Unfollow, Followers & Following lists, Remove follower, Automatic non-negative counters.
- **Safety & Moderation**: Bidirectional Block/Unblock enforcement across every endpoint, deduplicated reporting, and full Owner Admin Panel.

---

## Deploying to Render (Recommended — Zero Data Loss Setup)

### Step 1: Attach Permanent PostgreSQL + Media Storage (Free Tier)
Render's free web service container resets its local disk whenever it redeploys or restarts. To keep 100% of your data permanently (like Instagram):
1. **Database (`DATABASE_URL`)**: Create a free PostgreSQL database on [Neon.tech](https://neon.tech) or [Supabase](https://supabase.com) (free tier does not expire) and copy the connection string (`postgresql://...`). Add it in Render Dashboard -> **Environment** -> `DATABASE_URL`.
2. **Media Storage (`CLOUDINARY_URL`)**: Create a free account at [Cloudinary](https://cloudinary.com) and copy your `CLOUDINARY_URL` (`cloudinary://API_KEY:API_SECRET@CLOUD_NAME`). Add it in Render Dashboard -> **Environment** -> `CLOUDINARY_URL`.
3. Verify at `https://your-service.onrender.com/health` — it will report `"database": "postgresql"` and `"mediaStorage": "cloudinary"`.

### Option 1: Render Blueprint (`render.yaml` included at repo root)
1. Push this repository to GitHub/GitLab.
2. In Render Dashboard -> **New +** -> **Blueprint**, select your repo.
3. Render automatically reads `/render.yaml` (`rootDir: backend`) and sets up:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*'`
   - **Health Check Path**: `/health`
   - **Auto-generated `SECRET_KEY`**

> **Important for WebSockets on Render**: Always run with `--workers 1` so all connected WebSocket clients share the same in-memory `ConnectionManager`.

### Option 2: Docker Web Service on Render (Fixes `open Dockerfile: no such file or directory`)
- Both `/Dockerfile` (repo root) and `/backend/Dockerfile` (if you pushed only the `backend/` folder) are included.
- Select **Docker** runtime on Render (leave **Root Directory** blank if pushing the repo root, or set `backend` if using `backend/Dockerfile`).
- Render automatically detects the `Dockerfile` and runs Uvicorn on `$PORT` with `--workers 1 --proxy-headers`.

### Option 3: Manual Python Web Service
- **Root Directory**: `backend`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*'`
- **Health Check Path**: `/health`
- **Environment Variables**:
  - `SECRET_KEY`: long random string
  - `PUBLIC_BASE_URL` *(optional)*: `https://your-service-name.onrender.com` (Render also provides `RENDER_EXTERNAL_URL` automatically)

---

## Local Quick Start

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Verify server is running:
- Health Check: `http://localhost:8000/health`
- WebSocket Endpoint: `ws://localhost:8000/ws?token=<JWT>` (or `wss://your-app.onrender.com/ws?token=<JWT>`)
- Interactive Swagger API Docs: `http://localhost:8000/docs`

## Connecting the ZIQU Android App

- **Render Hosting**: Tap the server icon at the top-right of the **Login** screen and enter `https://your-service-name.onrender.com/` (the app automatically connects REST to `https://...` and WebSockets to `wss://.../ws?token=...`).
- **Android Emulator (Local)**: `http://10.0.2.2:8000/` connects automatically to `localhost:8000`.
- **Physical Phone (Local Wi-Fi)**: Enter `http://<YOUR_PC_LAN_IP>:8000/`.
