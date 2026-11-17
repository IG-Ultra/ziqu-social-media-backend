import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DATABASE_PATH, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-16000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
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

            CREATE TABLE IF NOT EXISTS password_resets (
                id TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                email TEXT NOT NULL,
                reset_code TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER DEFAULT 0
            );

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
            CREATE INDEX IF NOT EXISTS idx_posts_timestamp ON posts(timestamp DESC);

            CREATE TABLE IF NOT EXISTS likes (
                post_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (post_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS saved_posts (
                post_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (post_id, user_id)
            );

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

            CREATE TABLE IF NOT EXISTS follows (
                follower_uid TEXT NOT NULL,
                following_uid TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                PRIMARY KEY (follower_uid, following_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_uid);

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

            CREATE INDEX IF NOT EXISTS idx_posts_reel_ts ON posts(is_reel, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_u1 ON conversations(user1_uid, last_message_time DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_u2 ON conversations(user2_uid, last_message_time DESC);
            CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications(receiver_id, pushed_to_system, timestamp);
            CREATE INDEX IF NOT EXISTS idx_reports_status_ts ON reports(status, timestamp DESC);
            """
        )

        # Safe additive migrations for existing databases
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
        if "is_banned" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0;")

        for admin_email in settings.ADMIN_EMAIL_SET:
            db.execute("UPDATE users SET is_admin = 1 WHERE lower(email) = ?", (admin_email,))
