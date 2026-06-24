"""User Profile — personalized preferences and behavior tracking.

Tracks user habits across sessions:
- Language preference (auto-detected from user input)
- Frequently used skills (skill usage frequency)
- Response format preference (concise/detailed/code-heavy)
- Preferred model tier (trivial/simple/complex/expert)
- Topic interests (extracted from conversation topics)
- Time patterns (when user typically interacts)

All data is persisted in SQLite and survives restarts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class UserProfileStore:
    """SQLite-backed user profile store with auto-learning.

    Schema:
        - preferences: key-value store for user preferences
        - skill_usage: skill name + count + last_used timestamp
        - topics: topic keyword + count + last_seen
        - patterns: interaction patterns (time of day, session length)
    """

    def __init__(self, db_path: str = "data/memory/user_profile.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._write_lock = threading.RLock()
        self._init_schema()

        # In-memory cache for fast access
        self._cache: Dict[str, Any] = {}
        self._load_cache()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        with self._write_lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS skill_usage (
                    skill TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    last_used REAL,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS topics (
                    topic TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    last_seen REAL,
                    category TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS patterns (
                    pattern_type TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_skill_usage_count ON skill_usage(count DESC);
                CREATE INDEX IF NOT EXISTS idx_topics_count ON topics(count DESC);
            """)
            self._conn.commit()

    def _load_cache(self) -> None:
        """Load preferences into memory cache."""
        try:
            cur = self._conn.execute("SELECT key, value FROM preferences")
            for key, value in cur:
                try:
                    self._cache[key] = json.loads(value)
                except json.JSONDecodeError:
                    self._cache[key] = value
        except sqlite3.Error:
            logger.exception("Failed to load profile cache")

    # ============================================================ Preferences

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get a preference value (cached)."""
        return self._cache.get(key, default)

    def set_preference(self, key: str, value: Any) -> None:
        """Set a preference value (persisted + cached)."""
        now = time.time()
        serialized = json.dumps(value) if not isinstance(value, str) else value
        with self._write_lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO preferences(key, value, updated_at) VALUES (?, ?, ?)",
                (key, serialized, now),
            )
            self._conn.commit()
        self._cache[key] = value
        logger.debug("Preference set: %s = %s", key, value)

    def get_all_preferences(self) -> Dict[str, Any]:
        """Get all preferences."""
        return dict(self._cache)

    # ============================================================ Skill Usage

    def record_skill_usage(
        self,
        skill: str,
        success: bool = True,
        increment: int = 1,
    ) -> None:
        """Record a skill usage event."""
        now = time.time()
        with self._write_lock:
            # Upsert skill usage
            self._conn.execute(
                """
                INSERT INTO skill_usage(skill, count, last_used, success_count, failure_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(skill) DO UPDATE SET
                    count = count + ?,
                    last_used = ?,
                    success_count = success_count + ?,
                    failure_count = failure_count + ?
                """,
                (skill, increment, now, 1 if success else 0, 0 if success else 1,
                 increment, now, 1 if success else 0, 0 if success else 1),
            )
            self._conn.commit()

    def get_top_skills(self, limit: int = 10) -> List[Tuple[str, int]]:
        """Get most frequently used skills."""
        cur = self._conn.execute(
            "SELECT skill, count FROM skill_usage ORDER BY count DESC LIMIT ?",
            (limit,),
        )
        return [(row[0], row[1]) for row in cur]

    def get_skill_success_rate(self, skill: str) -> float:
        """Get success rate for a skill (0.0-1.0)."""
        cur = self._conn.execute(
            "SELECT success_count, failure_count FROM skill_usage WHERE skill = ?",
            (skill,),
        )
        row = cur.fetchone()
        if not row or row[0] + row[1] == 0:
            return 0.5  # Unknown skill, assume neutral
        return row[0] / (row[0] + row[1])

    # ============================================================ Topics

    def record_topic(self, topic: str, category: str = "") -> None:
        """Record a conversation topic."""
        now = time.time()
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO topics(topic, count, last_seen, category)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(topic) DO UPDATE SET
                    count = count + 1,
                    last_seen = ?,
                    category = COALESCE(?, category)
                """,
                (topic, now, category, now, category),
            )
            self._conn.commit()

    def get_top_topics(self, limit: int = 20) -> List[Tuple[str, int, str]]:
        """Get most frequently discussed topics."""
        cur = self._conn.execute(
            "SELECT topic, count, category FROM topics ORDER BY count DESC LIMIT ?",
            (limit,),
        )
        return [(row[0], row[1], row[2]) for row in cur]

    def get_recent_topics(self, hours: int = 24) -> List[str]:
        """Get topics from the last N hours."""
        cutoff = time.time() - hours * 3600
        cur = self._conn.execute(
            "SELECT topic FROM topics WHERE last_seen > ? ORDER BY last_seen DESC",
            (cutoff,),
        )
        return [row[0] for row in cur]

    # ============================================================ Patterns

    def record_pattern(self, pattern_type: str, data: Dict[str, Any]) -> None:
        """Record an interaction pattern."""
        now = time.time()
        serialized = json.dumps(data)
        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO patterns(pattern_type, data, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pattern_type) DO UPDATE SET
                    data = ?,
                    updated_at = ?
                """,
                (pattern_type, serialized, now, serialized, now),
            )
            self._conn.commit()

    def get_pattern(self, pattern_type: str) -> Optional[Dict[str, Any]]:
        """Get a recorded pattern."""
        cur = self._conn.execute(
            "SELECT data FROM patterns WHERE pattern_type = ?",
            (pattern_type,),
        )
        row = cur.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return None
        return None

    def record_time_pattern(self) -> None:
        """Record current interaction time for pattern analysis."""
        hour = time.localtime().tm_hour
        pattern = self.get_pattern("time_of_day") or {"hours": {}}
        hours = pattern.get("hours", {})
        hours[str(hour)] = hours.get(str(hour), 0) + 1
        pattern["hours"] = hours
        self.record_pattern("time_of_day", pattern)

    def get_active_hours(self) -> List[int]:
        """Get hours when user is most active."""
        pattern = self.get_pattern("time_of_day")
        if not pattern:
            return []
        hours = pattern.get("hours", {})
        # Return top 3 active hours
        sorted_hours = sorted(hours.items(), key=lambda x: x[1], reverse=True)
        return [int(h[0]) for h in sorted_hours[:3]]

    # ============================================================ Summary

    def get_profile_summary(self) -> Dict[str, Any]:
        """Get a comprehensive profile summary."""
        return {
            "preferences": self.get_all_preferences(),
            "top_skills": self.get_top_skills(5),
            "top_topics": self.get_top_topics(10),
            "active_hours": self.get_active_hours(),
        }

    def clear(self) -> None:
        """Clear all profile data (for testing/reset)."""
        with self._write_lock:
            self._conn.executescript(
                "DELETE FROM preferences; DELETE FROM skill_usage; DELETE FROM topics; DELETE FROM patterns;"
            )
            self._conn.commit()
        self._cache.clear()


# Singleton instance (lazy-loaded)
_profile_store: Optional[UserProfileStore] = None


def get_profile_store() -> UserProfileStore:
    """Get the shared UserProfileStore instance."""
    global _profile_store
    if _profile_store is None:
        _profile_store = UserProfileStore()
    return _profile_store