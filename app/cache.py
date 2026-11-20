import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Generic, Optional, Tuple, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Thread-safe bounded LRU + TTL cache with O(1) get/set/invalidate."""

    def __init__(self, max_size: int = 10_000, default_ttl_s: float = 10.0) -> None:
        self._max_size = max(16, int(max_size))
        self._default_ttl_s = max(0.1, float(default_ttl_s))
        self._lock = threading.RLock()
        self._data: "OrderedDict[Any, Tuple[float, T]]" = OrderedDict()

    def get(self, key: Any) -> Optional[T]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: T, ttl_s: Optional[float] = None) -> T:
        ttl = self._default_ttl_s if ttl_s is None else max(0.05, float(ttl_s))
        expires_at = time.monotonic() + ttl
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (expires_at, value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)
        return value

    def delete(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_where(self, predicate: Callable[[Any], bool]) -> None:
        with self._lock:
            keys = [k for k in self._data.keys() if predicate(k)]
            for k in keys:
                self._data.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def purge_expired(self) -> int:
        now = time.monotonic()
        removed = 0
        with self._lock:
            expired_keys = [k for k, (exp, _) in self._data.items() if exp <= now]
            for k in expired_keys:
                self._data.pop(k, None)
                removed += 1
        return removed


class SlidingWindowLimiter:
    """Bounded per-IP sliding-window rate limiter using minimal memory."""

    def __init__(self, max_buckets: int = 50_000) -> None:
        self._max_buckets = max(1000, int(max_buckets))
        self._lock = threading.Lock()
        # key -> (window_start_monotonic, count)
        self._buckets: "OrderedDict[str, Tuple[float, int]]" = OrderedDict()

    def allow(self, key: str, limit: int, window_s: float) -> Tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                self._buckets[key] = (now, 1)
                if len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)
                return True, 0

            win_start, count = entry
            elapsed = now - win_start
            if elapsed >= window_s:
                self._buckets.move_to_end(key)
                self._buckets[key] = (now, 1)
                return True, 0

            if count >= limit:
                retry_after = max(1, int(window_s - elapsed) + 1)
                return False, retry_after

            self._buckets.move_to_end(key)
            self._buckets[key] = (win_start, count + 1)
            return True, 0

    def purge_older_than(self, max_age_s: float = 120.0) -> int:
        cutoff = time.monotonic() - max_age_s
        removed = 0
        with self._lock:
            stale = [k for k, (start, _) in self._buckets.items() if start < cutoff]
            for k in stale:
                self._buckets.pop(k, None)
                removed += 1
        return removed


# ---- Application-wide bounded caches ----

# uid -> {"exists": bool, "is_banned": bool, "is_admin": bool, "email": str}
auth_status_cache: TTLCache[Dict[str, Any]] = TTLCache(max_size=50_000, default_ttl_s=10.0)

# (uid, base_url) -> serialized user dict
user_profile_cache: TTLCache[Dict[str, Any]] = TTLCache(max_size=25_000, default_ttl_s=20.0)

# username_lower -> uid
username_uid_cache: TTLCache[str] = TTLCache(max_size=25_000, default_ttl_s=30.0)

# uid -> frozenset[str] of blocked peer IDs
blocked_peers_cache: TTLCache[frozenset] = TTLCache(max_size=25_000, default_ttl_s=15.0)

# ("feed"|"reels"|"explore", limit, base_url) -> list[dict]
hot_feed_cache: TTLCache[list] = TTLCache(max_size=64, default_ttl_s=8.0)

# ("stories", base_url) -> list[dict]
hot_stories_cache: TTLCache[list] = TTLCache(max_size=16, default_ttl_s=8.0)

# ("users_list", limit, base_url) -> list[dict]
hot_users_list_cache: TTLCache[list] = TTLCache(max_size=16, default_ttl_s=15.0)

# Rate limiter buckets
rate_limiter = SlidingWindowLimiter(max_buckets=50_000)


def invalidate_feed_caches() -> None:
    hot_feed_cache.clear()


def invalidate_stories_cache() -> None:
    hot_stories_cache.clear()


def invalidate_user_caches(uid: str, username_lower: Optional[str] = None) -> None:
    if not uid:
        return
    auth_status_cache.delete(uid)
    user_profile_cache.delete_where(lambda k: isinstance(k, tuple) and len(k) >= 1 and k[0] == uid)
    blocked_peers_cache.delete(uid)
    hot_users_list_cache.clear()
    if username_lower:
        username_uid_cache.delete(username_lower.lower())
    else:
        username_uid_cache.clear()


def purge_all_expired() -> None:
    auth_status_cache.purge_expired()
    user_profile_cache.purge_expired()
    username_uid_cache.purge_expired()
    blocked_peers_cache.purge_expired()
    hot_feed_cache.purge_expired()
    hot_stories_cache.purge_expired()
    hot_users_list_cache.purge_expired()
    rate_limiter.purge_older_than(120.0)
