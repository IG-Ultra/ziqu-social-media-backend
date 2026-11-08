# 🚀 Ziqu Backend System (Instagram Clone in Python & SQL)

Ziqu social media app ke liye complete production-ready backend engine, designed exactly like Instagram's scalable architecture.

---

## 🌟 Key Features (Kya-Kya Included Hai)
1. **Authentication & Security**:
   - JWT (JSON Web Tokens) with 7-day expiration
   - Bcrypt password hashing
   - Username availability checks & regex validation
2. **Instagram-Style Feed Algorithm**:
   - Push & Pull hybrid model with SQL CTEs
   - Dynamic engagement scoring: `(likes * 2 + comments * 5) / (hours_old + 2)^1.5`
   - Personalized following feed + Explore grid
3. **Reels & Video Engine**:
   - 9:16 vertical video feed with infinite scroll pagination
   - Audio metadata and play-rate engagement ranking
4. **Stories (24-Hour Expiration TTL)**:
   - Auto-expiring queries: `WHERE expires_at > NOW()`
   - Story views tracking with viewer list
5. **Social Graph (Follows & Profiles)**:
   - Follow / Unfollow with automatic private account approval flow
   - Follower & Following counters with SQL indexes
6. **Interactions**:
   - Double-tap likes with atomic SQL triggers
   - Threaded/Nested comment replies (`parent_comment_id`)
   - Bookmark / Saved posts
7. **Direct Messaging (DMs)**:
   - 1-on-1 private chat messaging with timestamp ordering

---

## 🛠️ Tech Stack
- **Framework**: Python 3.11+ & FastAPI (Asynchronous high-performance REST API)
- **Database**: PostgreSQL 16 (Relational DB with indexes, foreign keys & triggers)
- **ORM**: SQLAlchemy 2.0 (Type-safe models with `Mapped[]` & `mapped_column`)
- **Validation**: Pydantic v2
- **Deployment**: Docker & Docker Compose (FastAPI + PostgreSQL + Redis)

---

## ⚡ Quick Start Guide (Kaise Run Karein)

### Option 1: Docker ke sath 1-Command me Run karein (Recommended)
```bash
# 1. Clone ya download kiya hua folder open karein
cd ziqu-backend

# 2. Docker compose start karein
docker-compose up --build
```
FastAPI Server ab live ho jayega: **http://localhost:8000**
Interactive Swagger Documentation: **http://localhost:8000/docs**

---

### Option 2: Local Python Environment me Run karein
```bash
# 1. Virtual environment banayein
python -m venv venv
source venv/bin/activate  # Windows me: venv\Scripts\activate

# 2. Dependencies install karein
pip install -r requirements.txt

# 3. .env file banayein
cp .env.example .env

# 4. PostgreSQL me schema create karein
psql -U postgres -d ziqu_db -f schema.sql

# 5. Server start karein
uvicorn app.main:app --reload --port 8000
```

---

## 📱 Mobile App (React Native / Flutter) Integration
API base URL ko apne frontend me configure karein:
```javascript
const API_URL = "http://localhost:8000/api/v1";

// 1. User Register / Login
const response = await fetch(`${API_URL}/auth/login`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    username_or_email: "rohit_sharma",
    password: "mypassword123"
  })
});
const { access_token } = await response.json();

// 2. Feed Fetch with Bearer Token
const feedRes = await fetch(`${API_URL}/feed`, {
  headers: { "Authorization": `Bearer ${access_token}` }
});
const feedPosts = await feedRes.json();
```
