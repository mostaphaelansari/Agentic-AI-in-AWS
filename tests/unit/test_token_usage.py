"""Stopped Runtime token-usage reconciliation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services.token_usage import TOKEN_USAGE_AVAILABLE, _usage_from_events  # noqa: E402


def test_session_usage_is_read_from_plain_application_log() -> None:
    session_id = "streamlit_demo-abc--def"
    events = [
        {
            "timestamp": 1,
            "message": (
                "INFO comparison_token_usage="
                + json.dumps(
                    {
                        "session_id": session_id,
                        "usage": {"inputTokens": 120, "outputTokens": 30},
                    }
                )
            ),
        }
    ]

    result = _usage_from_events(events, session_id)

    assert result["status"] == TOKEN_USAGE_AVAILABLE
    assert result["usage"]["inputTokens"] == 120


def test_session_usage_is_read_from_a_structured_log_envelope() -> None:
    session_id = "streamlit_demo-abc--def"
    line = "comparison_token_usage=" + json.dumps(
        {"session_id": session_id, "usage": {"totalTokens": 150}}
    )
    events = [{"timestamp": 2, "message": json.dumps({"body": line})}]

    result = _usage_from_events(events, session_id)

    assert result["status"] == TOKEN_USAGE_AVAILABLE
    assert result["usage"]["totalTokens"] == 150
