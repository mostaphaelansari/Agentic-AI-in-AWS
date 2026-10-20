"""Prevent benchmark architectures from silently drifting to different tools."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_aws_agents_use_the_same_gateway_stack() -> None:
    managed = _read("infrastructure/pulumi/managed_agent/__main__.py")
    custom = _read("infrastructure/pulumi/custom_agent/__main__.py")
    production = _read("infrastructure/pulumi/custom_agent/Pulumi.production.yaml")

    assert 'gateway_stack.require_output("gateway_arn")' in managed
    assert 'gateway_stack.require_output("gateway_url")' in custom
    assert 'config.get("runtime_gateway_url") or gateway_url' in custom
    assert "runtime_gateway_url:" not in production


def test_browser_is_opt_in_and_disabled_for_parity() -> None:
    managed = _read("infrastructure/pulumi/managed_agent/__main__.py")
    custom_stack = _read("infrastructure/pulumi/custom_agent/__main__.py")
    custom_agent = _read("agents/custom/agent.py")

    assert 'config.get_bool("enable_browser") or False' in managed
    assert 'config.get("enable_browser") or "false"' in custom_stack
    assert 'os.environ.get("ENABLE_BROWSER", "false")' in custom_agent
    assert "if ENABLE_BROWSER:" in custom_agent
    assert '["maxTokens", "memory", "model", "tools"]' not in managed


def test_custom_agent_tool_call_cap_is_configurable() -> None:
    custom_stack = _read("infrastructure/pulumi/custom_agent/__main__.py")
    custom_agent = _read("agents/custom/agent.py")
    production = _read("infrastructure/pulumi/custom_agent/Pulumi.production.yaml")

    assert 'os.environ.get("MAX_TOOL_CALLS", "12")' in custom_agent
    assert 'config.get_int("max_tool_calls") or 12' in custom_stack
    assert '"MAX_TOOL_CALLS": str(max_tool_calls)' in custom_stack
    assert "agentic-blueprint-custom-agent:max_tool_calls: 12" in production
    assert 'config.get_int("bedrock_max_tokens") or 4096' in custom_stack
    assert '"BEDROCK_MAX_TOKENS": str(bedrock_max_tokens)' in custom_stack
    assert "agentic-blueprint-custom-agent:bedrock_max_tokens: 4096" in production


def test_opensource_tool_call_cap_is_forwarded_by_compose() -> None:
    service = _read("opensource/agents/custom/service.py")
    compose = _read("opensource/compose/docker-compose.yml")
    example_env = _read("opensource/compose/.env.example")

    assert 'os.environ.get("MAX_TOOL_CALLS", "12")' in service
    assert "MAX_TOOL_CALLS: ${MAX_TOOL_CALLS:-12}" in compose
    assert "MAX_TOOL_CALLS=12" in example_env


def test_managed_agent_has_room_for_complex_tool_workflows() -> None:
    managed_stack = _read("infrastructure/pulumi/managed_agent/__main__.py")
    production = _read("infrastructure/pulumi/managed_agent/Pulumi.production.yaml")
    harness_backend = _read("apps/comparison/backends/harness.py")

    assert 'config.get_int("max_iterations") or 12' in managed_stack
    assert 'agentic-blueprint-managed-agent:max_iterations: "12"' in production
    assert '"maxTokens": HARNESS_MAX_TOKENS' in harness_backend


def test_all_paths_have_shared_mcp_and_code_execution() -> None:
    managed = _read("infrastructure/pulumi/managed_agent/__main__.py")
    custom = _read("agents/custom/agent.py")
    oss_service = _read("opensource/agents/custom/service.py")
    compose = _read("opensource/compose/docker-compose.yml")
    mcp_server = _read("opensource/mcp-tools/server.py")
    hosted_mcp_config = _read("infrastructure/pulumi/mcp_tools/Pulumi.production.yaml")

    assert '"type": "agentcore_gateway"' in managed
    assert "mcp.list_tools_sync()" in custom
    assert '"name": "code_interpreter"' in managed
    assert "interpreter.code_interpreter" in custom
    assert "build_code_interpreter_tool" in oss_service
    assert "MCP_URL: http://mcp-tools:8000/mcp" in compose
    # AWS Gateway already has its Open-Meteo target, so the hosted copy must
    # not publish a second pair of identically capable weather tools.
    assert 'enable_weather_tools: "false"' in hosted_mcp_config
    for tool_name in (
        "webSearch",
        "searchMapPlaces",
        "findNearbyPlaces",
        "searchLocation",
        "getForecast",
    ):
        assert f"def {tool_name}(" in mcp_server
