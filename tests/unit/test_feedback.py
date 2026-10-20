from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services import feedback  # noqa: E402


def _save(rating: str = "up") -> None:
    feedback.save_feedback(
        ledger_id="chat-1",
        run_id="run-1",
        architecture="Managed agent",
        rating=rating,
        prompt="How is the weather?",
        answer="Sunny.",
        session_id="session-1",
        parent_id="parent-1",
        branch_source_id="edited-1",
    )


def test_feedback_is_persisted_and_can_be_changed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "feedback.sqlite3"
    monkeypatch.setenv(feedback.FEEDBACK_DB_ENV, str(path))

    _save("up")
    assert feedback.get_feedback("chat-1", "run-1", "Managed agent")["rating"] == "up"

    _save("down")
    stored = feedback.get_feedback("chat-1", "run-1", "Managed agent")
    assert stored["rating"] == "down"
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*), rating, prompt, answer, session_id, parent_id, "
            "branch_source_id FROM agent_feedback"
        ).fetchone()
    assert row == (
        1,
        "down",
        "How is the weather?",
        "Sunny.",
        "session-1",
        "parent-1",
        "edited-1",
    )


def test_feedback_is_isolated_per_architecture(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(feedback.FEEDBACK_DB_ENV, str(tmp_path / "feedback.sqlite3"))
    _save("up")
    feedback.save_feedback(
        ledger_id="chat-1",
        run_id="run-1",
        architecture="Custom agent",
        rating="down",
        prompt="prompt",
        answer="answer",
        session_id="session-2",
    )

    assert feedback.get_feedback("chat-1", "run-1", "Managed agent")["rating"] == "up"
    assert feedback.get_feedback("chat-1", "run-1", "Custom agent")["rating"] == "down"


def test_feedback_validation_and_widget_mapping(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(feedback.FEEDBACK_DB_ENV, str(tmp_path / "feedback.sqlite3"))

    with pytest.raises(feedback.FeedbackStoreError, match="must be"):
        _save("maybe")

    assert feedback.feedback_default(None) is None
    assert feedback.feedback_default({"rating": "down"}) == 0
    assert feedback.feedback_default({"rating": "up"}) == 1
    assert feedback.feedback_rating(0) == "down"
    assert feedback.feedback_rating(1) == "up"
    with pytest.raises(feedback.FeedbackStoreError, match="Unexpected"):
        feedback.feedback_rating(2)
