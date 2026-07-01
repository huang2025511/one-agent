"""Cost tracking and budget management for LLM usage.

Tracks per-call cost via SQLite, enforces daily/monthly budgets, and
provides query APIs for dashboards and alerting.

Thread-safe: uses a ``threading.Lock`` to serialize writes, plus
``check_same_thread=False`` so the connection can be shared across
async tasks.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List

from core.db import create_sqlite_connection

logger = logging.getLogger(__name__)

# Default per-token cost (USD per token) when no MODEL_COST entry exists.
_DEFAULT_COST_PER_TOKEN = 0.0001


class CostTracker:
    """Track LLM costs by provider/model with budget enforcement.

    Stores every call in a local SQLite database and exposes summary
    queries for daily / monthly / per-provider / per-model breakdowns.

    Budget checks are soft — exceeding the budget logs a warning but
    does not raise an exception.  The caller (LLMProvider) is responsible
    for deciding whether to downgrade or reject the request.
    """

    def __init__(
        self,
        db_path: str = "data/memory/costs.db",
        daily_budget: float = 1.0,
        monthly_budget: float = 20.0,
    ) -> None:
        self._conn = create_sqlite_connection(db_path)
        self._lock = threading.Lock()
        self._daily_budget = daily_budget
        self._monthly_budget = monthly_budget
        self._migrate()

    # ------------------------------------------------------------------ schema
    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS cost_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                provider    TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                tokens_prompt     INTEGER DEFAULT 0,
                tokens_completion INTEGER DEFAULT 0,
                cost_usd    REAL    DEFAULT 0.0,
                session_id  TEXT    DEFAULT '',
                created_at  REAL
            );
            CREATE INDEX IF NOT EXISTS idx_cost_log_date ON cost_log(created_at);
        """)
        self._conn.commit()

    # ---------------------------------------------------------------- recording
    def record(
        self,
        provider: str,
        model: str,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        session_id: str = "",
    ) -> float:
        """Record a cost entry.  Returns the computed cost in USD.

        The cost is calculated by looking up the model in ``MODEL_COST``
        (per-1K-token price), falling back to the default 0.0001 USD/token.
        """
        try:
            from models.tiers import MODEL_COST
        except ImportError:
            MODEL_COST: Dict[str, float] = {}  # type: ignore[no-redef]

        total_tokens = tokens_prompt + tokens_completion
        if total_tokens <= 0:
            cost = 0.0
        else:
            # MODEL_COST stores price per 1K tokens; convert to per-token
            price_per_1k = MODEL_COST.get(
                model,
                MODEL_COST.get(f"{provider}/{model}", _DEFAULT_COST_PER_TOKEN * 1000),
            )
            cost = price_per_1k * (total_tokens / 1000.0)

        cost = round(cost, 8)

        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO cost_log
                       (provider, model, tokens_prompt, tokens_completion,
                        cost_usd, session_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (provider, model, tokens_prompt, tokens_completion,
                     cost, session_id, time.time()),
                )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.exception("cost_tracker: failed to persist cost entry: %s", exc)
            raise  # propagate so caller can fall back to estimated cost

        # Check budget (fire-and-forget — never block the caller)
        try:
            self.check_budget()
        except Exception:
            pass

        return cost

    # ---------------------------------------------------------------- queries
    def _daily_start(self) -> float:
        """Unix timestamp of the start of today (local time)."""
        import datetime
        now = datetime.datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    def _monthly_start(self) -> float:
        """Unix timestamp of the start of the current month (local time)."""
        import datetime
        now = datetime.datetime.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    def _cost_since(self, since: float) -> float:
        try:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cost_log WHERE created_at >= ?",
                (since,),
            ).fetchone()
            return round(row["total"], 8) if row else 0.0
        except sqlite3.Error as exc:
            logger.exception("cost_tracker query failed: %s", exc)
            return 0.0

    def daily_cost(self) -> Dict[str, Any]:
        cost = self._cost_since(self._daily_start())
        return {
            "cost": cost,
            "budget": self._daily_budget,
            "remaining": round(max(0.0, self._daily_budget - cost), 8),
            "exceeded": cost > self._daily_budget,
        }

    def monthly_cost(self) -> Dict[str, Any]:
        cost = self._cost_since(self._monthly_start())
        return {
            "cost": cost,
            "budget": self._monthly_budget,
            "remaining": round(max(0.0, self._monthly_budget - cost), 8),
            "exceeded": cost > self._monthly_budget,
        }

    def check_budget(self) -> Dict[str, Any]:
        with self._lock:
            daily = self.daily_cost()
            monthly = self.monthly_cost()
        return {
            "daily": daily,
            "monthly": monthly,
            "overall_exceeded": daily["exceeded"] or monthly["exceeded"],
        }

    def by_provider(self) -> Dict[str, float]:
        rows = self._conn.execute(
            "SELECT provider, COALESCE(SUM(cost_usd), 0) AS total "
            "FROM cost_log GROUP BY provider ORDER BY total DESC"
        ).fetchall()
        return {r["provider"]: round(r["total"], 8) for r in rows}

    def by_model(self) -> Dict[str, float]:
        rows = self._conn.execute(
            "SELECT model, COALESCE(SUM(cost_usd), 0) AS total "
            "FROM cost_log GROUP BY model ORDER BY total DESC"
        ).fetchall()
        return {r["model"]: round(r["total"], 8) for r in rows}

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM cost_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def total_cost(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cost_log"
        ).fetchone()
        return round(row["total"], 8) if row else 0.0

    def total_tokens(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_prompt + tokens_completion), 0) AS total FROM cost_log"
        ).fetchone()
        return int(row["total"]) if row else 0

    def close(self) -> None:
        """Close the database connection."""
        try:
            if self._conn:
                self._conn.close()
                self._conn = None
        except Exception:
            pass

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        if hasattr(self, '_conn') and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
