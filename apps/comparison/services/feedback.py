"""Durable per-agent answer feedback stored in SQLite."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

FEEDBACK_DB_ENV = "FEEDBACK_DB_PATH"
SESSION_COST_DB_ENV = "SESSION_COST_DB_PATH"
VALID_RATINGS = {"up", "down"}
MAX_STORED_TEXT_CHARS = 100_000


class FeedbackStoreError(RuntimeError):
    """Raised when feedback cannot be validated or persisted."""


def _db_path() -> Path:
    configured = os.environ.get(FEEDBACK_DB_ENV) or os.environ.get(SESSION_COST_DB_ENV)
    return Path(configured or "var/session-costs.sqlite3")


def _bounded(value: object) -> str:
    text = str(value or "")
    return text[:MAX_STORED_TEXT_CHARS]


def _connect() -> sqlite3.Connection:
    path = _db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_feedback ("
            "ledger_id TEXT NOT NULL, run_id TEXT NOT NULL, architecture TEXT NOT NULL, "
            "rating TEXT NOT NULL CHECK(rating IN ('up', 'down')), prompt TEXT NOT NULL, "
            "answer TEXT NOT NULL, session_id TEXT NOT NULL, parent_id TEXT, "
            "branch_source_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (ledger_id, run_id, architecture))"
        )
        return connection
    except (OSError, sqlite3.Error) as exc:
        raise FeedbackStoreError("Feedback database is unavailable.") from exc


def save_feedback(
    *,
    ledger_id: str,
    run_id: str,
    architecture: str,
    rating: str,
    prompt: str,
    answer: str,
    session_id: str,
    parent_id: str | None = None,
    branch_source_id: str | None = None,
) -> None:
    """Insert or replace one architecture's rating for a run."""
    if rating not in VALID_RATINGS:
        raise FeedbackStoreError("Feedback rating must be 'up' or 'down'.")
    if not ledger_id or not run_id or not architecture:
        raise FeedbackStoreError("Feedback is missing its chat, run, or agent identifier.")
    now = time.time()
    try:
        with _connect() as connection:
            connection.execute(
                "INSERT INTO agent_feedback (ledger_id, run_id, architecture, rating, "
                "prompt, answer, session_id, parent_id, branch_source_id, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ledger_id, run_id, architecture) DO UPDATE SET "
                "rating=excluded.rating, prompt=excluded.prompt, answer=excluded.answer, "
                "session_id=excluded.session_id, parent_id=excluded.parent_id, "
                "branch_source_id=excluded.branch_source_id, updated_at=excluded.updated_at",
                (
                    ledger_id,
                    run_id,
                    architecture,
                    rating,
                    _bounded(prompt),
                    _bounded(answer),
                    session_id,
                    parent_id,
                    branch_source_id,
                    now,
                    now,
                ),
            )
    except (OSError, sqlite3.Error) as exc:
        raise FeedbackStoreError("Feedback could not be saved.") from exc


def get_feedback(ledger_id: str, run_id: str, architecture: str) -> dict | None:
    """Load the current rating and update timestamp for one answer."""
    if not ledger_id or not run_id or not architecture:
        return None
    try:
        with _connect() as connection:
            row = connection.execute(
                "SELECT rating, updated_at FROM agent_feedback "
                "WHERE ledger_id = ? AND run_id = ? AND architecture = ?",
                (ledger_id, run_id, architecture),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise FeedbackStoreError("Feedback could not be loaded.") from exc
    return {"rating": row[0], "updated_at": row[1]} if row else None


def feedback_default(feedback: dict | None) -> int | None:
    """Map persisted ratings to Streamlit's thumbs widget values."""
    if not feedback:
        return None
    return 1 if feedback.get("rating") == "up" else 0


def feedback_rating(value: int) -> str:
    """Map Streamlit's thumbs widget value to the persisted rating."""
    if value not in {0, 1}:
        raise FeedbackStoreError("Unexpected thumbs feedback value.")
    return "up" if value == 1 else "down"
