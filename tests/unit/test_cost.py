"""Tests for measured-usage cost conversion (inference, caching, Gateway)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services.cost import (  # noqa: E402
    builtin_tool_calls,
    builtin_tool_kinds,
    cache_savings,
    gateway_cost,
    gateway_operations,
    inference_cost,
    uncached_inference_cost,
)


def usage(fresh_input=0, cache_read=0, cache_write=0, output=0) -> dict:
    return {
        "fresh_input": fresh_input,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": output,
    }


def test_inference_cost_applies_per_mtok_list_rates():
    cost = inference_cost(
        usage(fresh_input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, output=1_000_000)
    )

    assert cost["input_cost"] == pytest.approx(3.00)
    assert cost["cache_read_cost"] == pytest.approx(0.30)
    assert cost["cache_write_cost"] == pytest.approx(3.75)
    assert cost["output_cost"] == pytest.approx(15.00)
    assert cost["total_cost"] == pytest.approx(22.05)


def test_cache_savings_nets_read_discount_against_write_premium():
    # Reads save (3.00 - 0.30)/MTok; writes cost an extra (3.75 - 3.00)/MTok.
    savings = cache_savings(usage(cache_read=2_000_000, cache_write=1_000_000))
    assert savings == pytest.approx(2 * 2.70 - 0.75)

    # Writes that never pay back are a net loss.
    assert cache_savings(usage(cache_write=1_000_000)) == pytest.approx(-0.75)


def test_uncached_inference_cost_prices_all_input_side_tokens_as_fresh():
    cost = uncached_inference_cost(
        usage(fresh_input=1_000_000, cache_read=1_000_000, cache_write=1_000_000, output=1_000_000)
    )

    assert cost == pytest.approx(24.00)


def test_gateway_operations_excludes_browser_actions():
    metrics = {"tool_names": ["open_meteo_tools", "browser_navigate", "code_interpreter"]}
    assert gateway_operations(metrics) == 1
    assert builtin_tool_calls(metrics) == 2
    assert gateway_operations({"browse_calls": 7}) == 0
    assert builtin_tool_calls({"browse_calls": 7}) == 7
    assert gateway_operations({"tool_calls": 4}) == 4
    assert gateway_operations({}) == 0


def test_builtin_tool_kinds_select_the_agentcore_compute_meters():
    metrics = {"tool_names": ["browser_navigate", "code_interpreter", "code_interpreter"]}
    assert builtin_tool_kinds(metrics) == ["browser", "code_interpreter"]
    assert builtin_tool_kinds({"browse_calls": 2}) == ["browser"]


def test_gateway_cost_is_per_thousand_operations():
    assert gateway_cost(1000) == pytest.approx(0.005)
    assert gateway_cost(0) == 0.0
