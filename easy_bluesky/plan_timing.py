"""plan_timing.py — SQLite-backed plan duration recording and estimation.

Records completed-plan timing (num_points, per_step_s, t_init_s) and
estimates future plan duration using three tiers, in order of accuracy:

  1. Feature model  — num_points × mean_per_step_s + 2 × t_move
                      where t_move = live t_init_s if available, else DB mean.
  2. Regression     — numpy lstsq over [num_points, 1] → total_s, used when
                      >= MIN_RUNS records exist for the plan name.
  3. Mean           — simple per-plan-name mean total_s from the DB.

Confidence labels: "live" > "regression" > "history (N)" > "mean (N)".
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import numpy as np

# Minimum completed runs before regression is used instead of mean.
MIN_REGRESSION_RUNS = 8

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS plan_timing (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_name    TEXT    NOT NULL,
    num_points   INTEGER,
    t_init_s     REAL,
    per_step_s   REAL,
    total_s      REAL,
    recorded_at  TEXT    NOT NULL,
    kwargs_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_name ON plan_timing (plan_name);
"""


class PlanTimingDB:
    """Thread-safe SQLite store for plan timing data."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.executescript(_CREATE_SQL)
        # Per-plan cache: plan_name → (mean_per_step_s, mean_t_init_s, count,
        #                              reg_coefs or None)
        self._cache: dict = {}
        self._rebuild_cache()

    # ── Public API ─────────────────────────────────────────────────────────────

    def record(
        self,
        plan_name: str,
        num_points: int,
        t_init_s: float,
        per_step_s: float,
        total_s: float,
        kwargs: dict | None = None,
    ) -> None:
        """Persist one completed-plan timing record and invalidate the cache."""
        import datetime
        if not plan_name or num_points < 1 or per_step_s <= 0 or total_s <= 0:
            return
        kwargs_json = json.dumps(kwargs) if kwargs else None
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            with self._connect() as con:
                con.execute(
                    "INSERT INTO plan_timing "
                    "(plan_name, num_points, t_init_s, per_step_s, total_s, recorded_at, kwargs_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (plan_name, num_points, t_init_s, per_step_s, total_s, ts, kwargs_json),
                )
        self._rebuild_cache()

    def estimate(
        self,
        plan_name: str,
        num_points: int,
        t_init_s: float = 0.0,
    ) -> tuple[float | None, str]:
        """Return (estimated_seconds, confidence) for a plan.

        t_init_s: live-measured init time from the current scan (0 = not yet known).
        confidence is one of: "live", "regression", "history (N)", "mean (N)", "none".
        """
        if num_points < 1:
            return None, "none"

        entry = self._cache.get(plan_name)
        if entry is None:
            return None, "none"

        mean_per_step, mean_t_init, count, reg_coefs = entry
        t_move = t_init_s if t_init_s > 0.0 else (mean_t_init or 0.0)

        # ── Tier 1: live t_init + feature model ────────────────────────────────
        if t_init_s > 0.0 and mean_per_step is not None:
            secs = num_points * mean_per_step + 2.0 * t_init_s
            return secs, "live"

        # ── Tier 2: regression (≥ MIN_REGRESSION_RUNS records) ─────────────────
        if reg_coefs is not None:
            a, b = reg_coefs          # total_s ≈ a * num_points + b
            secs = max(0.0, a * num_points + b)
            return secs, f"regression ({count})"

        # ── Tier 3: feature model with DB mean t_init ──────────────────────────
        if mean_per_step is not None:
            secs = num_points * mean_per_step + 2.0 * t_move
            return secs, f"history ({count})"

        # ── Tier 4: plain mean total_s ──────────────────────────────────────────
        rows = self._query_rows(plan_name)
        if rows:
            mean_total = float(np.mean([r[4] for r in rows if r[4]]))
            return mean_total, f"mean ({count})"

        return None, "none"

    def plan_names(self) -> list[str]:
        with self._lock:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT DISTINCT plan_name FROM plan_timing ORDER BY plan_name"
                ).fetchall()
        return [r[0] for r in rows]

    # ── Internal ───────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path), timeout=5)

    def _query_rows(self, plan_name: str) -> list:
        with self._lock:
            with self._connect() as con:
                return con.execute(
                    "SELECT plan_name, num_points, t_init_s, per_step_s, total_s "
                    "FROM plan_timing WHERE plan_name = ? AND per_step_s > 0 AND total_s > 0",
                    (plan_name,),
                ).fetchall()

    def _rebuild_cache(self) -> None:
        """Recompute per-plan statistics from the DB."""
        cache: dict = {}
        with self._lock:
            with self._connect() as con:
                names = [
                    r[0] for r in con.execute(
                        "SELECT DISTINCT plan_name FROM plan_timing"
                    ).fetchall()
                ]
                for name in names:
                    rows = con.execute(
                        "SELECT num_points, t_init_s, per_step_s, total_s "
                        "FROM plan_timing "
                        "WHERE plan_name = ? AND per_step_s > 0 AND total_s > 0",
                        (name,),
                    ).fetchall()
                    if not rows:
                        continue
                    count = len(rows)
                    per_step_vals = [r[2] for r in rows if r[2] and r[2] > 0]
                    t_init_vals   = [r[1] for r in rows if r[1] and r[1] > 0]
                    mean_per_step = float(np.mean(per_step_vals)) if per_step_vals else None
                    mean_t_init   = float(np.mean(t_init_vals))   if t_init_vals   else None

                    reg_coefs = None
                    if count >= MIN_REGRESSION_RUNS:
                        pts = np.array([[r[0], r[3]] for r in rows
                                        if r[0] and r[0] > 0 and r[3] and r[3] > 0])
                        if len(pts) >= MIN_REGRESSION_RUNS:
                            X = np.column_stack([pts[:, 0], np.ones(len(pts))])
                            try:
                                coefs, _, _, _ = np.linalg.lstsq(X, pts[:, 1], rcond=None)
                                if coefs[0] > 0:   # sanity: slope must be positive
                                    reg_coefs = (float(coefs[0]), float(coefs[1]))
                            except Exception:
                                pass

                    cache[name] = (mean_per_step, mean_t_init, count, reg_coefs)

        self._cache = cache
