import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence, Set

from .config import settings

logger = logging.getLogger("ziqu.db")

_thread_local = threading.local()
_connections_lock = threading.Lock()
_all_connections: Set[sqlite3.Connection] = set()
_write_guard = threading.RLock()

_pg_pool: Any = None
_pg_pool_lock = threading.Lock()


def is_postgres_enabled() -> bool:
    return bool(settings.DATABASE_URL)


def _rewrite_two_arg_max_to_greatest(sql: str) -> str:
    """
    Replace SQLite two-argument MAX(arg1, arg2) with PostgreSQL GREATEST(arg1, arg2)
    while leaving aggregate single-argument MAX(col) intact and skipping string literals.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single_quote = False

    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_single_quote and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single_quote = not in_single_quote
            i += 1
            continue

        if not in_single_quote and (ch == "M" or ch == "m"):
            prev_ok = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")
            if prev_ok and sql[i : i + 3].lower() == "max":
                j = i + 3
                while j < n and sql[j].isspace():
                    j += 1
                if j < n and sql[j] == "(":
                    # Scan arguments inside MAX(...)
                    depth = 1
                    k = j + 1
                    top_level_commas = 0
                    q = False
                    while k < n and depth > 0:
                        c = sql[k]
                        if c == "'":
                            if q and k + 1 < n and sql[k + 1] == "'":
                                k += 2
                                continue
                            q = not q
                        elif not q:
                            if c == "(":
                                depth += 1
                            elif c == ")":
                                depth -= 1
                            elif c == "," and depth == 1:
                                top_level_commas += 1
                        k += 1
                    if top_level_commas >= 1:
                        out.append("GREATEST(")
                        i = j + 1
                        continue
        out.append(ch)
        i += 1
    return "".join(out)


_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


def translate_sql_for_postgres(sql: str, has_params: bool = True) -> str:
    """Translate SQLite-flavored SQL statements to PostgreSQL syntax safely."""
    rewritten = _rewrite_two_arg_max_to_greatest(sql)
    has_insert_or_ignore = bool(_INSERT_OR_IGNORE_RE.search(rewritten))
    if has_insert_or_ignore:
        rewritten = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", rewritten)

    out: list[str] = []
    i = 0
    n = len(rewritten)
    in_single_quote = False
    while i < n:
        ch = rewritten[i]
        if ch == "'":
            out.append(ch)
            if in_single_quote and i + 1 < n and rewritten[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single_quote = not in_single_quote
            i += 1
            continue
        if not in_single_quote:
            if ch == "?" and has_params:
                out.append("%s")
                i += 1
                continue
            if ch == "%" and has_params:
                out.append("%%")
                i += 1
                continue
        out.append(ch)
        i += 1

    final_sql = "".join(out)
    if has_insert_or_ignore and "on conflict" not in final_sql.lower():
        stripped = final_sql.rstrip()
        if stripped.endswith(";"):
            final_sql = stripped[:-1] + " ON CONFLICT DO NOTHING;"
        else:
            final_sql = stripped + " ON CONFLICT DO NOTHING"
    return final_sql


def _sanitize_pg_param(val: Any) -> Any:
    if isinstance(val, str) and "\x00" in val:
        return val.replace("\x00", "")
    return val


def _sanitize_pg_params(params: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(_sanitize_pg_param(p) for p in params)


class _PostgresCompatConnection:
    """Thin adapter giving a psycopg3 connection the same ergonomics as sqlite3.Connection."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        has_params = bool(params)
        pg_sql = translate_sql_for_postgres(sql, has_params=has_params)
        cur = self._conn.cursor()
        if has_params:
            cur.execute(pg_sql, _sanitize_pg_params(params))
        else:
            cur.execute(pg_sql)
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
        pg_sql = translate_sql_for_postgres(sql, has_params=True)
        cur = self._conn.cursor()
        sanitized_seq = [_sanitize_pg_params(row) for row in seq_of_params]
        if sanitized_seq:
            cur.executemany(pg_sql, sanitized_seq)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


def is_unique_violation(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "23505":
        return True
    msg = str(exc).lower()
    return "unique" in msg or "duplicate key" in msg or "primary key" in msg


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None:
            return _pg_pool
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pg_pool = ConnectionPool(
            conninfo=settings.DATABASE_URL,
            min_size=min(settings.DB_POOL_MIN, 1),
            max_size=settings.DB_POOL_MAX,
            timeout=30.0,
            max_idle=45.0,
            max_lifetime=300.0,
            reconnect_timeout=30.0,
            check=ConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "autocommit": False,
                "connect_timeout": 15,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
        return _pg_pool


def _configure_sqlite_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-65536;")
    conn.execute("PRAGMA mmap_size=268435456;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=10000;")
    return conn


def _create_sqlite_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.DATABASE_PATH,
        check_same_thread=False,
        timeout=30.0,
        isolation_level="IMMEDIATE",
    )
    _configure_sqlite_connection(conn)
    with _connections_lock:
        _all_connections.add(conn)
    return conn


def _get_pooled_sqlite_connection() -> sqlite3.Connection:
    conn: Optional[sqlite3.Connection] = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            if conn.in_transaction:
                conn.rollback()
            return conn
        except sqlite3.Error:
            with _connections_lock:
                _all_connections.discard(conn)
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    conn = _create_sqlite_connection()
    _thread_local.conn = conn
    return conn


def _reset_pg_pool() -> None:
    global _pg_pool
    with _pg_pool_lock:
        old = _pg_pool
        _pg_pool = None
    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def close_all_connections() -> None:
    _reset_pg_pool()

    with _connections_lock:
        conns = list(_all_connections)
        _all_connections.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    if hasattr(_thread_local, "conn"):
        _thread_local.conn = None


@contextmanager
def get_db(write: bool = False) -> Iterator[Any]:
    if is_postgres_enabled():
        raw_conn = None
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            pool = _get_pg_pool()
            try:
                ctx = pool.connection()
                raw_conn = ctx.__enter__()
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("PostgreSQL pool checkout failed (attempt %d/2): %s", attempt + 1, exc)
                _reset_pg_pool()
                if attempt == 0:
                    time.sleep(0.35)
        if raw_conn is None:
            raise last_exc  # type: ignore[misc]

        adapter = _PostgresCompatConnection(raw_conn)
        try:
            yield adapter
            raw_conn.commit()
        except Exception:
            try:
                raw_conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        return

    conn = _get_pooled_sqlite_connection()
    acquired = False
    if write:
        _write_guard.acquire()
        acquired = True
    try:
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        if acquired:
            _write_guard.release()


def run_wal_checkpoint() -> None:
    if is_postgres_enabled():
        return
    try:
        with get_db() as db:
            db.execute("PRAGMA wal_checkpoint(PASSIVE);")
    except Exception as exc:
        logger.debug("WAL checkpoint skipped: %s", exc)


def cleanup_expired_records() -> None:
    now_val = int(time.time() * 1000)
    expired_story_cutoff = now_val - (48 * 60 * 60 * 1000)
    old_notif_cutoff = now_val - (30 * 24 * 60 * 60 * 1000)
    try:
        with get_db(write=True) as db:
            db.execute(
                "DELETE FROM password_resets WHERE used = 1 OR expires_at < ?",
                (now_val - 3_600_000,),
            )
            stale_stories = [
                r["story_id"]
                for r in db.execute(
                    "SELECT story_id FROM stories WHERE expires_at < ? LIMIT 500",
                    (expired_story_cutoff,),
                ).fetchall()
            ]
            if stale_stories:
                placeholders = ",".join("?" for _ in stale_stories)
                db.execute(f"DELETE FROM story_viewers WHERE story_id IN ({placeholders})", tuple(stale_stories))
                db.execute(f"DELETE FROM story_reactions WHERE story_id IN ({placeholders})", tuple(stale_stories))
                db.execute(f"DELETE FROM stories WHERE story_id IN ({placeholders})", tuple(stale_stories))

            db.execute(
                "DELETE FROM notifications WHERE is_read = 1 AND timestamp < ?",
                (old_notif_cutoff,),
            )
    except Exception as exc:
        logger.debug("Cleanup skipped: %s", exc)


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_lower TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    profile_image TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    followers_count INTEGER DEFAULT 0,
    following_count INTEGER DEFAULT 0,
    posts_count INTEGER DEFAULT 0,
    is_verified INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    is_banned INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users(username_lower);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned, created_at DESC);

CREATE TABLE IF NOT EXISTS password_resets (
    id TEXT PRIMARY KEY,
    uid TEXT NOT NULL,
    email TEXT NOT NULL,
    reset_code TEXT NOT NULL,
    token_hash TEXT DEFAULT '',
    failed_attempts INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT 0,
    expires_at INTEGER NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_password_resets_lookup
    ON password_resets(uid, used, expires_at DESC);

CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    user_profile_image TEXT DEFAULT '',
    media_url TEXT NOT NULL,
    thumbnail_url TEXT DEFAULT '',
    media_type TEXT DEFAULT 'image',
    caption TEXT DEFAULT '',
    mood TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    likes_count INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    shares_count INTEGER DEFAULT 0,
    saves_count INTEGER DEFAULT 0,
    watch_time_ms INTEGER DEFAULT 0,
    views_count INTEGER DEFAULT 0,
    completions_count INTEGER DEFAULT 0,
    rewatch_count INTEGER DEFAULT 0,
    is_reel INTEGER DEFAULT 0,
    is_verified INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_posts_user_ts ON posts(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_posts_type_ts ON posts(media_type, timestamp DESC);

CREATE TABLE IF NOT EXISTS likes (
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_likes_user ON likes(user_id, post_id);

CREATE TABLE IF NOT EXISTS saved_posts (
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    saved_at INTEGER NOT NULL,
    PRIMARY KEY (post_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_saved_posts_user ON saved_posts(user_id, saved_at DESC);

CREATE TABLE IF NOT EXISTS comments (
    comment_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    user_profile_image TEXT DEFAULT '',
    comment_text TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    likes_count INTEGER DEFAULT 0,
    parent_id TEXT DEFAULT '',
    replied_to_user_id TEXT DEFAULT '',
    replied_to_username TEXT DEFAULT '',
    is_verified INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_comments_user_id ON comments(user_id);

CREATE TABLE IF NOT EXISTS follows (
    follower_uid TEXT NOT NULL,
    following_uid TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (follower_uid, following_uid)
);

CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_uid, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_uid, timestamp DESC);

CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    user_profile_image TEXT DEFAULT '',
    media_url TEXT NOT NULL,
    media_type TEXT DEFAULT 'image',
    timestamp INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    is_verified INTEGER DEFAULT 0,
    original_post_id TEXT DEFAULT '',
    original_author_username TEXT DEFAULT '',
    caption TEXT DEFAULT '',
    sticker TEXT DEFAULT '',
    text_x REAL DEFAULT 0.5,
    text_y REAL DEFAULT 0.5,
    text_color TEXT DEFAULT '#FFFFFF',
    text_scale REAL DEFAULT 1.0,
    text_rotation REAL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_stories_expires ON stories(expires_at);
CREATE INDEX IF NOT EXISTS idx_stories_user_exp ON stories(user_id, expires_at);

CREATE TABLE IF NOT EXISTS story_viewers (
    story_id TEXT NOT NULL,
    viewer_uid TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (story_id, viewer_uid)
);

CREATE INDEX IF NOT EXISTS idx_story_viewers_story_ts ON story_viewers(story_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS story_reactions (
    story_id TEXT NOT NULL,
    reactor_uid TEXT NOT NULL,
    reactor_username TEXT DEFAULT '',
    reactor_profile_image TEXT DEFAULT '',
    emoji TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (story_id, reactor_uid)
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user1_uid TEXT NOT NULL,
    user2_uid TEXT NOT NULL,
    last_message TEXT DEFAULT '',
    last_message_time INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    sender_username TEXT DEFAULT '',
    sender_profile_image TEXT DEFAULT '',
    message TEXT DEFAULT '',
    media_url TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    type TEXT DEFAULT 'text',
    seen INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    deleted_for_json TEXT DEFAULT '{}',
    reactions_json TEXT DEFAULT '{}',
    forwarded INTEGER DEFAULT 0,
    reply_to_message_id TEXT DEFAULT '',
    reply_to_sender_username TEXT DEFAULT '',
    reply_to_text TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages(conversation_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_chat_messages_pair ON chat_messages(conversation_id, message_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_unread
    ON chat_messages(conversation_id, receiver_id, seen) WHERE seen = 0;

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    group_image TEXT DEFAULT '',
    admin_id TEXT NOT NULL,
    admin_username TEXT DEFAULT '',
    last_message TEXT DEFAULT '',
    last_message_sender_id TEXT DEFAULT '',
    last_message_sender_name TEXT DEFAULT '',
    last_message_time INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL,
    uid TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, uid)
);

CREATE INDEX IF NOT EXISTS idx_group_members_uid ON group_members(uid);

CREATE TABLE IF NOT EXISTS group_messages (
    message_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    receiver_id TEXT DEFAULT '',
    sender_username TEXT DEFAULT '',
    sender_profile_image TEXT DEFAULT '',
    message TEXT DEFAULT '',
    media_url TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    type TEXT DEFAULT 'text',
    seen INTEGER DEFAULT 0,
    deleted INTEGER DEFAULT 0,
    deleted_for_json TEXT DEFAULT '{}',
    reactions_json TEXT DEFAULT '{}',
    forwarded INTEGER DEFAULT 0,
    reply_to_message_id TEXT DEFAULT '',
    reply_to_sender_username TEXT DEFAULT '',
    reply_to_text TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_group_messages_gid ON group_messages(group_id, timestamp);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    receiver_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_username TEXT DEFAULT '',
    sender_profile_image TEXT DEFAULT '',
    type TEXT NOT NULL,
    post_id TEXT DEFAULT '',
    post_media_url TEXT DEFAULT '',
    comment_id TEXT DEFAULT '',
    text TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    group_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    is_read INTEGER DEFAULT 0,
    pushed_to_system INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_notifications_receiver ON notifications(receiver_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications(receiver_id, type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(receiver_id, is_read, timestamp DESC);

CREATE TABLE IF NOT EXISTS blocks (
    block_id TEXT PRIMARY KEY,
    blocker_uid TEXT NOT NULL,
    blocker_username TEXT DEFAULT '',
    blocked_uid TEXT NOT NULL,
    blocked_username TEXT DEFAULT '',
    blocked_display_name TEXT DEFAULT '',
    blocked_profile_image TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    UNIQUE(blocker_uid, blocked_uid)
);

CREATE INDEX IF NOT EXISTS idx_blocks_blocker ON blocks(blocker_uid);
CREATE INDEX IF NOT EXISTS idx_blocks_blocked ON blocks(blocked_uid);

CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    reporter_uid TEXT NOT NULL,
    reporter_username TEXT DEFAULT '',
    reported_uid TEXT NOT NULL,
    reported_username TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    details TEXT DEFAULT '',
    target_type TEXT DEFAULT 'user',
    target_id TEXT DEFAULT '',
    timestamp INTEGER NOT NULL,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS user_presence (
    uid TEXT PRIMARY KEY,
    is_online INTEGER DEFAULT 0,
    last_seen INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_user_presence_online_seen
    ON user_presence(is_online, last_seen);

CREATE INDEX IF NOT EXISTS idx_posts_reel_ts ON posts(is_reel, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_u1 ON conversations(user1_uid, last_message_time DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_u2 ON conversations(user2_uid, last_message_time DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications(receiver_id, pushed_to_system, timestamp);
CREATE INDEX IF NOT EXISTS idx_reports_status_ts ON reports(status, timestamp DESC);
"""

_POSTGRES_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        uid TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        username_lower TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        profile_image TEXT DEFAULT '',
        created_at BIGINT NOT NULL,
        followers_count BIGINT DEFAULT 0,
        following_count BIGINT DEFAULT 0,
        posts_count BIGINT DEFAULT 0,
        is_verified INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0,
        is_banned INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_users_username_lower ON users(username_lower)",
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_users_created_at ON users(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS password_resets (
        id TEXT PRIMARY KEY,
        uid TEXT NOT NULL,
        email TEXT NOT NULL,
        reset_code TEXT NOT NULL,
        token_hash TEXT DEFAULT '',
        failed_attempts INTEGER DEFAULT 0,
        created_at BIGINT DEFAULT 0,
        expires_at BIGINT NOT NULL,
        used INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_password_resets_lookup ON password_resets(uid, used, expires_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash)",
    """
    CREATE TABLE IF NOT EXISTS posts (
        post_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        user_profile_image TEXT DEFAULT '',
        media_url TEXT NOT NULL,
        thumbnail_url TEXT DEFAULT '',
        media_type TEXT DEFAULT 'image',
        caption TEXT DEFAULT '',
        mood TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        likes_count BIGINT DEFAULT 0,
        comments_count BIGINT DEFAULT 0,
        shares_count BIGINT DEFAULT 0,
        saves_count BIGINT DEFAULT 0,
        watch_time_ms BIGINT DEFAULT 0,
        views_count BIGINT DEFAULT 0,
        completions_count BIGINT DEFAULT 0,
        rewatch_count BIGINT DEFAULT 0,
        is_reel INTEGER DEFAULT 0,
        is_verified INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_posts_user_ts ON posts(user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_posts_type_ts ON posts(media_type, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_posts_reel_ts ON posts(is_reel, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS likes (
        post_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        PRIMARY KEY (post_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_likes_user ON likes(user_id, post_id)",
    """
    CREATE TABLE IF NOT EXISTS saved_posts (
        post_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        saved_at BIGINT NOT NULL,
        PRIMARY KEY (post_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_saved_posts_user ON saved_posts(user_id, saved_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS comments (
        comment_id TEXT PRIMARY KEY,
        post_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        user_profile_image TEXT DEFAULT '',
        comment_text TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        likes_count BIGINT DEFAULT 0,
        parent_id TEXT DEFAULT '',
        replied_to_user_id TEXT DEFAULT '',
        replied_to_username TEXT DEFAULT '',
        is_verified INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comments_post_id ON comments(post_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_comments_user_id ON comments(user_id)",
    """
    CREATE TABLE IF NOT EXISTS follows (
        follower_uid TEXT NOT NULL,
        following_uid TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        PRIMARY KEY (follower_uid, following_uid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_uid, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_uid, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS stories (
        story_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        username TEXT NOT NULL,
        user_profile_image TEXT DEFAULT '',
        media_url TEXT NOT NULL,
        media_type TEXT DEFAULT 'image',
        timestamp BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        is_verified INTEGER DEFAULT 0,
        original_post_id TEXT DEFAULT '',
        original_author_username TEXT DEFAULT '',
        caption TEXT DEFAULT '',
        sticker TEXT DEFAULT '',
        text_x DOUBLE PRECISION DEFAULT 0.5,
        text_y DOUBLE PRECISION DEFAULT 0.5,
        text_color TEXT DEFAULT '#FFFFFF',
        text_scale DOUBLE PRECISION DEFAULT 1.0,
        text_rotation DOUBLE PRECISION DEFAULT 0.0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stories_expires ON stories(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_stories_user_exp ON stories(user_id, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS story_viewers (
        story_id TEXT NOT NULL,
        viewer_uid TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        PRIMARY KEY (story_id, viewer_uid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_story_viewers_story_ts ON story_viewers(story_id, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS story_reactions (
        story_id TEXT NOT NULL,
        reactor_uid TEXT NOT NULL,
        reactor_username TEXT DEFAULT '',
        reactor_profile_image TEXT DEFAULT '',
        emoji TEXT NOT NULL,
        timestamp BIGINT NOT NULL,
        PRIMARY KEY (story_id, reactor_uid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        conversation_id TEXT PRIMARY KEY,
        user1_uid TEXT NOT NULL,
        user2_uid TEXT NOT NULL,
        last_message TEXT DEFAULT '',
        last_message_time BIGINT DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_conversations_u1 ON conversations(user1_uid, last_message_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_u2 ON conversations(user2_uid, last_message_time DESC)",
    """
    CREATE TABLE IF NOT EXISTS chat_messages (
        message_id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        receiver_id TEXT NOT NULL,
        sender_username TEXT DEFAULT '',
        sender_profile_image TEXT DEFAULT '',
        message TEXT DEFAULT '',
        media_url TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        type TEXT DEFAULT 'text',
        seen INTEGER DEFAULT 0,
        deleted INTEGER DEFAULT 0,
        deleted_for_json TEXT DEFAULT '{}',
        reactions_json TEXT DEFAULT '{}',
        forwarded INTEGER DEFAULT 0,
        reply_to_message_id TEXT DEFAULT '',
        reply_to_sender_username TEXT DEFAULT '',
        reply_to_text TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages(conversation_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_pair ON chat_messages(conversation_id, message_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_messages_unread ON chat_messages(conversation_id, receiver_id, seen) WHERE seen = 0",
    """
    CREATE TABLE IF NOT EXISTS groups (
        group_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        group_image TEXT DEFAULT '',
        admin_id TEXT NOT NULL,
        admin_username TEXT DEFAULT '',
        last_message TEXT DEFAULT '',
        last_message_sender_id TEXT DEFAULT '',
        last_message_sender_name TEXT DEFAULT '',
        last_message_time BIGINT DEFAULT 0,
        created_at BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_members (
        group_id TEXT NOT NULL,
        uid TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        joined_at BIGINT NOT NULL,
        PRIMARY KEY (group_id, uid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_members_uid ON group_members(uid)",
    """
    CREATE TABLE IF NOT EXISTS group_messages (
        message_id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        receiver_id TEXT DEFAULT '',
        sender_username TEXT DEFAULT '',
        sender_profile_image TEXT DEFAULT '',
        message TEXT DEFAULT '',
        media_url TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        type TEXT DEFAULT 'text',
        seen INTEGER DEFAULT 0,
        deleted INTEGER DEFAULT 0,
        deleted_for_json TEXT DEFAULT '{}',
        reactions_json TEXT DEFAULT '{}',
        forwarded INTEGER DEFAULT 0,
        reply_to_message_id TEXT DEFAULT '',
        reply_to_sender_username TEXT DEFAULT '',
        reply_to_text TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_group_messages_gid ON group_messages(group_id, timestamp)",
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id TEXT PRIMARY KEY,
        receiver_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        sender_username TEXT DEFAULT '',
        sender_profile_image TEXT DEFAULT '',
        type TEXT NOT NULL,
        post_id TEXT DEFAULT '',
        post_media_url TEXT DEFAULT '',
        comment_id TEXT DEFAULT '',
        text TEXT DEFAULT '',
        conversation_id TEXT DEFAULT '',
        group_id TEXT DEFAULT '',
        title TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        is_read INTEGER DEFAULT 0,
        pushed_to_system INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notifications_receiver ON notifications(receiver_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_type ON notifications(receiver_id, type, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(receiver_id, is_read, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications(receiver_id, pushed_to_system, timestamp)",
    """
    CREATE TABLE IF NOT EXISTS blocks (
        block_id TEXT PRIMARY KEY,
        blocker_uid TEXT NOT NULL,
        blocker_username TEXT DEFAULT '',
        blocked_uid TEXT NOT NULL,
        blocked_username TEXT DEFAULT '',
        blocked_display_name TEXT DEFAULT '',
        blocked_profile_image TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        UNIQUE(blocker_uid, blocked_uid)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blocks_blocker ON blocks(blocker_uid)",
    "CREATE INDEX IF NOT EXISTS idx_blocks_blocked ON blocks(blocked_uid)",
    """
    CREATE TABLE IF NOT EXISTS reports (
        report_id TEXT PRIMARY KEY,
        reporter_uid TEXT NOT NULL,
        reporter_username TEXT DEFAULT '',
        reported_uid TEXT NOT NULL,
        reported_username TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        details TEXT DEFAULT '',
        target_type TEXT DEFAULT 'user',
        target_id TEXT DEFAULT '',
        timestamp BIGINT NOT NULL,
        status TEXT DEFAULT 'pending'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reports_status_ts ON reports(status, timestamp DESC)",
    """
    CREATE TABLE IF NOT EXISTS user_presence (
        uid TEXT PRIMARY KEY,
        is_online INTEGER DEFAULT 0,
        last_seen BIGINT DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_presence_online_seen ON user_presence(is_online, last_seen)",
]


_POSTGRES_ADDITIVE_COLUMNS = [
    ("users", "username", "TEXT DEFAULT ''"),
    ("users", "username_lower", "TEXT DEFAULT ''"),
    ("users", "email", "TEXT DEFAULT ''"),
    ("users", "password_hash", "TEXT DEFAULT ''"),
    ("users", "display_name", "TEXT DEFAULT ''"),
    ("users", "bio", "TEXT DEFAULT ''"),
    ("users", "profile_image", "TEXT DEFAULT ''"),
    ("users", "created_at", "BIGINT DEFAULT 0"),
    ("users", "followers_count", "BIGINT DEFAULT 0"),
    ("users", "following_count", "BIGINT DEFAULT 0"),
    ("users", "posts_count", "BIGINT DEFAULT 0"),
    ("users", "is_verified", "INTEGER DEFAULT 0"),
    ("users", "is_admin", "INTEGER DEFAULT 0"),
    ("users", "is_banned", "INTEGER DEFAULT 0"),
    ("password_resets", "uid", "TEXT DEFAULT ''"),
    ("password_resets", "email", "TEXT DEFAULT ''"),
    ("password_resets", "reset_code", "TEXT DEFAULT ''"),
    ("password_resets", "token_hash", "TEXT DEFAULT ''"),
    ("password_resets", "failed_attempts", "INTEGER DEFAULT 0"),
    ("password_resets", "created_at", "BIGINT DEFAULT 0"),
    ("password_resets", "expires_at", "BIGINT DEFAULT 0"),
    ("password_resets", "used", "INTEGER DEFAULT 0"),
    ("posts", "user_id", "TEXT DEFAULT ''"),
    ("posts", "username", "TEXT DEFAULT ''"),
    ("posts", "user_profile_image", "TEXT DEFAULT ''"),
    ("posts", "media_url", "TEXT DEFAULT ''"),
    ("posts", "thumbnail_url", "TEXT DEFAULT ''"),
    ("posts", "media_type", "TEXT DEFAULT 'image'"),
    ("posts", "caption", "TEXT DEFAULT ''"),
    ("posts", "mood", "TEXT DEFAULT ''"),
    ("posts", "timestamp", "BIGINT DEFAULT 0"),
    ("posts", "likes_count", "BIGINT DEFAULT 0"),
    ("posts", "comments_count", "BIGINT DEFAULT 0"),
    ("posts", "shares_count", "BIGINT DEFAULT 0"),
    ("posts", "saves_count", "BIGINT DEFAULT 0"),
    ("posts", "watch_time_ms", "BIGINT DEFAULT 0"),
    ("posts", "views_count", "BIGINT DEFAULT 0"),
    ("posts", "completions_count", "BIGINT DEFAULT 0"),
    ("posts", "rewatch_count", "BIGINT DEFAULT 0"),
    ("posts", "is_reel", "INTEGER DEFAULT 0"),
    ("posts", "is_verified", "INTEGER DEFAULT 0"),
    ("likes", "timestamp", "BIGINT DEFAULT 0"),
    ("saved_posts", "saved_at", "BIGINT DEFAULT 0"),
    ("comments", "post_id", "TEXT DEFAULT ''"),
    ("comments", "user_id", "TEXT DEFAULT ''"),
    ("comments", "username", "TEXT DEFAULT ''"),
    ("comments", "user_profile_image", "TEXT DEFAULT ''"),
    ("comments", "comment_text", "TEXT DEFAULT ''"),
    ("comments", "timestamp", "BIGINT DEFAULT 0"),
    ("comments", "likes_count", "BIGINT DEFAULT 0"),
    ("comments", "parent_id", "TEXT DEFAULT ''"),
    ("comments", "replied_to_user_id", "TEXT DEFAULT ''"),
    ("comments", "replied_to_username", "TEXT DEFAULT ''"),
    ("comments", "is_verified", "INTEGER DEFAULT 0"),
    ("follows", "timestamp", "BIGINT DEFAULT 0"),
    ("stories", "user_id", "TEXT DEFAULT ''"),
    ("stories", "username", "TEXT DEFAULT ''"),
    ("stories", "user_profile_image", "TEXT DEFAULT ''"),
    ("stories", "media_url", "TEXT DEFAULT ''"),
    ("stories", "media_type", "TEXT DEFAULT 'image'"),
    ("stories", "timestamp", "BIGINT DEFAULT 0"),
    ("stories", "expires_at", "BIGINT DEFAULT 0"),
    ("stories", "is_verified", "INTEGER DEFAULT 0"),
    ("stories", "original_post_id", "TEXT DEFAULT ''"),
    ("stories", "original_author_username", "TEXT DEFAULT ''"),
    ("stories", "caption", "TEXT DEFAULT ''"),
    ("stories", "sticker", "TEXT DEFAULT ''"),
    ("stories", "text_x", "DOUBLE PRECISION DEFAULT 0.5"),
    ("stories", "text_y", "DOUBLE PRECISION DEFAULT 0.5"),
    ("stories", "text_color", "TEXT DEFAULT '#FFFFFF'"),
    ("stories", "text_scale", "DOUBLE PRECISION DEFAULT 1.0"),
    ("stories", "text_rotation", "DOUBLE PRECISION DEFAULT 0.0"),
    ("story_viewers", "timestamp", "BIGINT DEFAULT 0"),
    ("story_reactions", "reactor_username", "TEXT DEFAULT ''"),
    ("story_reactions", "reactor_profile_image", "TEXT DEFAULT ''"),
    ("story_reactions", "emoji", "TEXT DEFAULT ''"),
    ("story_reactions", "timestamp", "BIGINT DEFAULT 0"),
    ("conversations", "user1_uid", "TEXT DEFAULT ''"),
    ("conversations", "user2_uid", "TEXT DEFAULT ''"),
    ("conversations", "last_message", "TEXT DEFAULT ''"),
    ("conversations", "last_message_time", "BIGINT DEFAULT 0"),
    ("chat_messages", "conversation_id", "TEXT DEFAULT ''"),
    ("chat_messages", "sender_id", "TEXT DEFAULT ''"),
    ("chat_messages", "receiver_id", "TEXT DEFAULT ''"),
    ("chat_messages", "sender_username", "TEXT DEFAULT ''"),
    ("chat_messages", "sender_profile_image", "TEXT DEFAULT ''"),
    ("chat_messages", "message", "TEXT DEFAULT ''"),
    ("chat_messages", "media_url", "TEXT DEFAULT ''"),
    ("chat_messages", "timestamp", "BIGINT DEFAULT 0"),
    ("chat_messages", "type", "TEXT DEFAULT 'text'"),
    ("chat_messages", "seen", "INTEGER DEFAULT 0"),
    ("chat_messages", "deleted", "INTEGER DEFAULT 0"),
    ("chat_messages", "deleted_for_json", "TEXT DEFAULT '{}'"),
    ("chat_messages", "reactions_json", "TEXT DEFAULT '{}'"),
    ("chat_messages", "forwarded", "INTEGER DEFAULT 0"),
    ("chat_messages", "reply_to_message_id", "TEXT DEFAULT ''"),
    ("chat_messages", "reply_to_sender_username", "TEXT DEFAULT ''"),
    ("chat_messages", "reply_to_text", "TEXT DEFAULT ''"),
    ("groups", "name", "TEXT DEFAULT ''"),
    ("groups", "description", "TEXT DEFAULT ''"),
    ("groups", "group_image", "TEXT DEFAULT ''"),
    ("groups", "admin_id", "TEXT DEFAULT ''"),
    ("groups", "admin_username", "TEXT DEFAULT ''"),
    ("groups", "last_message", "TEXT DEFAULT ''"),
    ("groups", "last_message_sender_id", "TEXT DEFAULT ''"),
    ("groups", "last_message_sender_name", "TEXT DEFAULT ''"),
    ("groups", "last_message_time", "BIGINT DEFAULT 0"),
    ("groups", "created_at", "BIGINT DEFAULT 0"),
    ("group_members", "is_admin", "INTEGER DEFAULT 0"),
    ("group_members", "joined_at", "BIGINT DEFAULT 0"),
    ("group_messages", "group_id", "TEXT DEFAULT ''"),
    ("group_messages", "sender_id", "TEXT DEFAULT ''"),
    ("group_messages", "receiver_id", "TEXT DEFAULT ''"),
    ("group_messages", "sender_username", "TEXT DEFAULT ''"),
    ("group_messages", "sender_profile_image", "TEXT DEFAULT ''"),
    ("group_messages", "message", "TEXT DEFAULT ''"),
    ("group_messages", "media_url", "TEXT DEFAULT ''"),
    ("group_messages", "timestamp", "BIGINT DEFAULT 0"),
    ("group_messages", "type", "TEXT DEFAULT 'text'"),
    ("group_messages", "seen", "INTEGER DEFAULT 0"),
    ("group_messages", "deleted", "INTEGER DEFAULT 0"),
    ("group_messages", "deleted_for_json", "TEXT DEFAULT '{}'"),
    ("group_messages", "reactions_json", "TEXT DEFAULT '{}'"),
    ("group_messages", "forwarded", "INTEGER DEFAULT 0"),
    ("group_messages", "reply_to_message_id", "TEXT DEFAULT ''"),
    ("group_messages", "reply_to_sender_username", "TEXT DEFAULT ''"),
    ("group_messages", "reply_to_text", "TEXT DEFAULT ''"),
    ("notifications", "receiver_id", "TEXT DEFAULT ''"),
    ("notifications", "sender_id", "TEXT DEFAULT ''"),
    ("notifications", "sender_username", "TEXT DEFAULT ''"),
    ("notifications", "sender_profile_image", "TEXT DEFAULT ''"),
    ("notifications", "type", "TEXT DEFAULT ''"),
    ("notifications", "post_id", "TEXT DEFAULT ''"),
    ("notifications", "post_media_url", "TEXT DEFAULT ''"),
    ("notifications", "comment_id", "TEXT DEFAULT ''"),
    ("notifications", "text", "TEXT DEFAULT ''"),
    ("notifications", "conversation_id", "TEXT DEFAULT ''"),
    ("notifications", "group_id", "TEXT DEFAULT ''"),
    ("notifications", "title", "TEXT DEFAULT ''"),
    ("notifications", "timestamp", "BIGINT DEFAULT 0"),
    ("notifications", "is_read", "INTEGER DEFAULT 0"),
    ("notifications", "pushed_to_system", "INTEGER DEFAULT 0"),
    ("blocks", "blocker_uid", "TEXT DEFAULT ''"),
    ("blocks", "blocker_username", "TEXT DEFAULT ''"),
    ("blocks", "blocked_uid", "TEXT DEFAULT ''"),
    ("blocks", "blocked_username", "TEXT DEFAULT ''"),
    ("blocks", "blocked_display_name", "TEXT DEFAULT ''"),
    ("blocks", "blocked_profile_image", "TEXT DEFAULT ''"),
    ("blocks", "timestamp", "BIGINT DEFAULT 0"),
    ("reports", "reporter_uid", "TEXT DEFAULT ''"),
    ("reports", "reporter_username", "TEXT DEFAULT ''"),
    ("reports", "reported_uid", "TEXT DEFAULT ''"),
    ("reports", "reported_username", "TEXT DEFAULT ''"),
    ("reports", "reason", "TEXT DEFAULT ''"),
    ("reports", "details", "TEXT DEFAULT ''"),
    ("reports", "target_type", "TEXT DEFAULT 'user'"),
    ("reports", "target_id", "TEXT DEFAULT ''"),
    ("reports", "timestamp", "BIGINT DEFAULT 0"),
    ("reports", "status", "TEXT DEFAULT 'pending'"),
    ("user_presence", "is_online", "INTEGER DEFAULT 0"),
    ("user_presence", "last_seen", "BIGINT DEFAULT 0"),
]


def _init_postgres() -> None:
    from urllib.parse import urlparse
    import psycopg

    parsed = urlparse(settings.DATABASE_URL)
    logger.info(
        "Connecting to PostgreSQL host=%s db=%s user=%s (pool min=%s, max=%s)",
        parsed.hostname or "<unknown>",
        (parsed.path or "").lstrip("/") or "<default>",
        parsed.username or "<unknown>",
        settings.DB_POOL_MIN,
        settings.DB_POOL_MAX,
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            with psycopg.connect(
                settings.DATABASE_URL,
                connect_timeout=20,
                prepare_threshold=None,
            ) as test_conn:
                test_conn.execute("SELECT 1")
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "PostgreSQL preflight attempt %d/3 failed for host=%s: %s",
                attempt,
                parsed.hostname or "<unknown>",
                exc,
            )
            if attempt < 3:
                time.sleep(2.0)

    if last_exc is not None:
        logger.error(
            "PostgreSQL preflight connection failed for host=%s user=%s: %s. "
            "Verify DATABASE_URL in Render Environment matches your active Neon/Postgres project and password.",
            parsed.hostname or "<unknown>",
            parsed.username or "<unknown>",
            last_exc,
        )
        raise last_exc

    with get_db(write=True) as db:
        table_stmts = [s for s in _POSTGRES_SCHEMA_STATEMENTS if "CREATE TABLE" in s.upper()]
        index_stmts = [s for s in _POSTGRES_SCHEMA_STATEMENTS if "CREATE TABLE" not in s.upper()]

        for stmt in table_stmts:
            db.execute(stmt)

        # Ensure all additive columns exist for older PostgreSQL schemas BEFORE creating indexes on them
        col_rows = db.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"
        ).fetchall()
        existing_cols = {(r["table_name"], r["column_name"]) for r in col_rows}
        for table_name, col_name, col_def in _POSTGRES_ADDITIVE_COLUMNS:
            if (table_name, col_name) not in existing_cols:
                db.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                existing_cols.add((table_name, col_name))

        db.execute(
            "UPDATE users SET username_lower = lower(COALESCE(NULLIF(username, ''), uid)) "
            "WHERE username_lower IS NULL OR username_lower = ''"
        )

        for stmt in index_stmts:
            db.execute(stmt)

        for admin_email in settings.ADMIN_EMAIL_SET:
            db.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (admin_email,))


def _init_sqlite() -> None:
    logger.warning(
        "DATABASE_URL is not set — using local SQLite (%s). Note: Render Free resets local disk on restart/deploy!",
        settings.DATABASE_PATH,
    )
    sqlite_stmts = [s.strip() for s in _SQLITE_SCHEMA.split(";") if s.strip()]
    table_stmts = [s for s in sqlite_stmts if s.upper().startswith("CREATE TABLE")]
    index_stmts = [s for s in sqlite_stmts if not s.upper().startswith("CREATE TABLE")]

    with get_db(write=True) as db:
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA wal_autocheckpoint=1000;")
        for stmt in table_stmts:
            db.execute(f"{stmt};")

        sqlite_col_cache: dict[str, set[str]] = {}
        for table_name, col_name, col_def in _POSTGRES_ADDITIVE_COLUMNS:
            cols = sqlite_col_cache.get(table_name)
            if cols is None:
                cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
                sqlite_col_cache[table_name] = cols
            if col_name not in cols:
                sqlite_def = col_def.replace("BIGINT", "INTEGER").replace("DOUBLE PRECISION", "REAL")
                db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {sqlite_def};")
                cols.add(col_name)

        db.execute(
            "UPDATE users SET username_lower = lower(COALESCE(NULLIF(username, ''), uid)) "
            "WHERE username_lower IS NULL OR username_lower = ''"
        )

        for stmt in index_stmts:
            db.execute(f"{stmt};")
        db.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash);")

        for admin_email in settings.ADMIN_EMAIL_SET:
            db.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (admin_email,))
        db.execute("PRAGMA optimize;")


def init_db() -> None:
    if is_postgres_enabled():
        _init_postgres()
    else:
        _init_sqlite()
