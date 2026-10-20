"""Guards for the mcp-tools AgentCore Runtime Pulumi stack and smoke helpers."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MCP_TOOLS_STACK = ROOT / "infrastructure" / "pulumi" / "mcp_tools"
SCRIPTS = ROOT / "infrastructure" / "scripts"

sys.path.insert(0, str(MCP_TOOLS_STACK))
sys.path.insert(0, str(SCRIPTS))
import endpoint as mcp_endpoint  # noqa: E402
import smoke_mcp_tools as smoke  # noqa: E402


def test_local_endpoint_helper_encodes_arn() -> None:
    arn = "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/opensource_mcp_tools-abc"
    url = mcp_endpoint.runtime_mcp_endpoint(arn, "eu-central-1")
    assert "arn%3Aaws%3Abedrock-agentcore" in url
    assert url.endswith("/invocations?qualifier=DEFAULT")


def test_stack_does_not_import_gateway_module() -> None:
    program = (MCP_TOOLS_STACK / "__main__.py").read_text()
    assert "from mcp_targets import" not in program
    assert "sys.path.insert" not in program
    assert "from endpoint import runtime_mcp_endpoint" in program
    assert "BOOTSTRAP_ECR_ONLY" in program
    assert 'image_tag_mutability="IMMUTABLE"' in program
    assert '{"scan_on_push": True}' in program or "{'scan_on_push': True}" in program
    assert 'server_protocol": "MCP"' in program or 'server_protocol": "MCP"' in program


def test_deploy_workflow_builds_arm64_and_bootstraps_ecr() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy-mcp-tools.yml").read_text()
    assert "platforms: linux/arm64" in workflow
    assert "BOOTSTRAP_ECR_ONLY" in workflow
    assert "Verify image architecture is arm64" in workflow
    assert "smoke_mcp_tools.py" in workflow
    assert "context: opensource/mcp-tools" in workflow


def test_readme_covers_bootstrap_rollback_and_smoke() -> None:
    readme = (MCP_TOOLS_STACK / "README.md").read_text()
    for needle in (
        "BOOTSTRAP_ECR_ONLY",
        "linux/arm64",
        "pulumi preview",
        "pulumi up",
        "smoke_mcp_tools.py",
        "mcp_endpoint",
        "Rollback",
        "scan_on_push",
        "DuckDuckGo",
    ):
        assert needle in readme, needle


def test_smoke_catalog_requires_weather_by_default() -> None:
    full = {
        "webSearch",
        "searchMapPlaces",
        "findNearbyPlaces",
        "searchLocation",
        "getForecast",
    }
    smoke.assert_tool_catalog(full, expect_weather=True)
    try:
        smoke.assert_tool_catalog(full - {"getForecast"}, expect_weather=True)
    except AssertionError as exc:
        assert "weather tools required" in str(exc)
    else:
        raise AssertionError("expected missing-weather assertion")


def test_smoke_catalog_web_and_map_only_mode() -> None:
    smoke.assert_tool_catalog(
        {"webSearch", "searchMapPlaces", "findNearbyPlaces"},
        expect_weather=False,
    )
    try:
        smoke.assert_tool_catalog(
            {"webSearch", "searchMapPlaces", "findNearbyPlaces", "getForecast"},
            expect_weather=False,
        )
    except AssertionError as exc:
        assert "weather tools are present" in str(exc)
    else:
        raise AssertionError("expected weather-tools assertion")


def test_stack_defaults_enable_weather_and_workflow_gates_scan() -> None:
    program = (MCP_TOOLS_STACK / "__main__.py").read_text()
    assert 'config.get("enable_weather_tools") or "true"' in program
    workflow = (ROOT / ".github" / "workflows" / "deploy-mcp-tools.yml").read_text()
    assert "image-scan-complete" in workflow
    assert "describe-image-scan-findings" in workflow
    assert 'EXPECT_WEATHER_TOOLS: "true"' in workflow


def test_smoke_image_tag_check() -> None:
    smoke.check_image_tag("123.dkr.ecr.eu-central-1.amazonaws.com/repo:abc", "abc")
    try:
        smoke.check_image_tag("123.dkr.ecr.eu-central-1.amazonaws.com/repo:abc", "def")
    except AssertionError as exc:
        assert "image tag mismatch" in str(exc)
    else:
        raise AssertionError("expected tag mismatch")


def test_weather_tools_disabled_at_import(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_WEATHER_TOOLS", "false")
    monkeypatch.setenv("MCP_STATELESS_HTTP", "true")
    sys.path.insert(0, str(ROOT / "opensource" / "mcp-tools"))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    assert server._ENABLE_WEATHER_TOOLS is False
    tools = getattr(server.mcp._tool_manager, "_tools", {})
    assert "webSearch" in tools
    assert "searchLocation" not in tools
    assert "getForecast" not in tools
