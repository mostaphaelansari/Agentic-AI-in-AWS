"""Tests for stable Managed-versus-Custom display order."""

from __future__ import annotations

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from components import comparison  # noqa: E402


def test_custom_agent_stays_after_managed_agent() -> None:
    states = {
        "Custom agent": {"arch_name": "Custom agent"},
        "Managed agent": {"arch_name": "Managed agent"},
    }

    assert [name for name, _ in comparison._ordered_agent_items(states)] == [
        "Managed agent",
        "Custom agent",
    ]


def test_unknown_agents_keep_input_order_after_known_agents() -> None:
    states = {
        "Other agent": {"arch_name": "Other agent"},
        "Custom agent": {"arch_name": "Custom agent"},
        "Managed agent": {"arch_name": "Managed agent"},
    }

    assert [name for name, _ in comparison._ordered_agent_items(states)] == [
        "Managed agent",
        "Custom agent",
        "Other agent",
    ]


def test_metric_chart_serializes_with_ordered_inline_data() -> None:
    chart = comparison._metric_chart(
        [("Managed agent", 18.22), ("Custom agent", 9.53)],
        "Latency",
        "seconds",
    )

    spec = chart.to_dict()

    assert spec["data"]["values"][0]["Agent"] == "Managed agent"
    assert spec["data"]["values"][1]["Agent"] == "Custom agent"
    assert spec["encoding"]["x"]["sort"] == ["Managed agent", "Custom agent"]
