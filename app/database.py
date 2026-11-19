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


def translate_sql_for_postgres(sql: str) -> str:
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
            if ch == "?":
                out.append("%s")
                i += 1
                continue
            if ch == "%":
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


class _PostgresCompatConnection:
    """Thin adapter giving a psycopg3 connection the same ergonomics as sqlite3.Connection."""

    def __init__(self, raw_conn: Any) -> None:
        self._conn = raw_conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        pg_sql = translate_sql_for_postgres(sql)
        cur = self._conn.cursor()
        if params:
            cur.execute(pg_sql, params)
        else:
            cur.execute(pg_sql)
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> Any:
        pg_sql = translate_sql_for_postgres(sql)
        cur = self._conn.cursor()
        cur.executemany(pg_sql, seq_of_params)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


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
            min_size=settings.DB_POOL_MIN,
            max_size=settings.DB_POOL_MAX,
            timeout=15.0,
            max_idle=180.0,
            reconnect_timeout=30.0,
            check=ConnectionPool.check_connection,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,
                "autocommit": False,
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


def close_all_connections() -> None:
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.close()
        except Exception:
            pass
        _pg_pool = None

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
        pool = _get_pg_pool()
        with pool.connection() as raw_conn:
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


def _init_postgres() -> None:
    logger.info("Initializing persistent PostgreSQL database pool (min=%s, max=%s)", settings.DB_POOL_MIN, settings.DB_POOL_MAX)
    with get_db(write=True) as db:
        for stmt in _POSTGRES_SCHEMA_STATEMENTS:
            db.execute(stmt)

        # Ensure additive columns exist for older schemas
        user_cols = {
            r["column_name"]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'users'"
            ).fetchall()
        }
        if "is_admin" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin INTEGER DEFAULT 0")
        if "is_banned" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned INTEGER DEFAULT 0")

        pr_cols = {
            r["column_name"]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'password_resets'"
            ).fetchall()
        }
        if "token_hash" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS token_hash TEXT DEFAULT ''")
        if "failed_attempts" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0")
        if "created_at" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS created_at BIGINT DEFAULT 0")

        for admin_email in settings.ADMIN_EMAIL_SET:
            db.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (admin_email,))


def _init_sqlite() -> None:
    logger.warning(
        "DATABASE_URL is not set — using local SQLite (%s). Note: Render Free resets local disk on restart/deploy!",
        settings.DATABASE_PATH,
    )
    with get_db(write=True) as db:
        db.execute("PRAGMA journal_mode=WAL;")
        db.execute("PRAGMA wal_autocheckpoint=1000;")
        db.executescript(_SQLITE_SCHEMA)

        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
        if "is_banned" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0;")

        pr_cols = {r["name"] for r in db.execute("PRAGMA table_info(password_resets)").fetchall()}
        if "token_hash" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN token_hash TEXT DEFAULT '';")
        if "failed_attempts" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN failed_attempts INTEGER DEFAULT 0;")
        if "created_at" not in pr_cols:
            db.execute("ALTER TABLE password_resets ADD COLUMN created_at INTEGER DEFAULT 0;")
        db.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash);")

        for admin_email in settings.ADMIN_EMAIL_SET:
            db.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (admin_email,))
        db.execute("PRAGMA optimize;")


def init_db() -> None:
    if is_postgres_enabled():
        _init_postgres()
    else:
        _init_sqlite()
