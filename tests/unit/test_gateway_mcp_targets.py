"""Unit tests for Gateway MCP target helpers and weather-tool gating."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PULUMI = ROOT / "infrastructure" / "pulumi" / "gateway"
MCP_TOOLS = ROOT / "opensource" / "mcp-tools"

sys.path.insert(0, str(GATEWAY_PULUMI))
import mcp_targets  # noqa: E402


def test_runtime_mcp_endpoint_encodes_arn() -> None:
    arn = "arn:aws:bedrock-agentcore:eu-central-1:123:runtime/opensource_mcp_tools-abc"
    url = mcp_targets.runtime_mcp_endpoint(arn, "eu-central-1")
    assert url.startswith("https://bedrock-agentcore.eu-central-1.amazonaws.com/runtimes/")
    assert "arn%3Aaws%3Abedrock-agentcore" in url
    assert url.endswith("/invocations?qualifier=DEFAULT")


def test_selected_targets_respect_secret_presence() -> None:
    present = {"rapidapi_key", "geoapify_api_key"}
    rapid = mcp_targets.selected_rapidapi_specs(present)
    remote = mcp_targets.selected_remote_mcp_specs(present)
    providers = mcp_targets.credential_provider_config_names(present)

    assert [spec.name for spec in rapid] == ["booking", "aerodatabox", "air-scraper"]
    assert [spec.name for spec in remote] == ["geoapify"]
    assert providers == ["rapidapi_key", "geoapify_api_key"]


def test_aerodatabox_prefers_dedicated_key_over_fallback() -> None:
    present = {"aerodatabox_rapidapi_key", "rapidapi_key"}
    chosen = mcp_targets.resolve_key_config_name(
        present,
        "aerodatabox_rapidapi_key",
        ("flight_rapidapi_key", "rapidapi_key"),
    )
    assert chosen == "aerodatabox_rapidapi_key"
    assert mcp_targets.credential_provider_config_names(present) == [
        "rapidapi_key",
        "aerodatabox_rapidapi_key",
    ]


def test_api_key_and_mcp_server_shapes() -> None:
    cred = mcp_targets.api_key_credential_provider_configuration(
        "arn:aws:bedrock-agentcore:eu-central-1:123:token-vault/default/apikeycredentialprovider/x"
    )
    assert cred["api_key"]["credential_parameter_name"] == "x-api-key"
    assert cred["api_key"]["credential_location"] == "HEADER"

    target = mcp_targets.mcp_server_target_configuration("https://example.com/mcp")
    assert target == {"mcp": {"mcp_server": {"endpoint": "https://example.com/mcp"}}}

    headers = mcp_targets.rapidapi_proxy_request_parameters("booking-com15.p.rapidapi.com")
    assert headers == {"overwrite:header.x-api-host": "booking-com15.p.rapidapi.com"}


def test_gateway_program_declares_optional_mcp_targets() -> None:
    program = (GATEWAY_PULUMI / "__main__.py").read_text()
    assert "AgentcoreApiKeyCredentialProvider" in program
    assert 'name="mcp-tools"' in program
    assert "name=spec.name" in program
    assert "apigatewayv2.Api" in program
    assert "mcp_tools_stack" in program
    assert 'config.get_bool("enable_travel_rag")' in program
    assert 'name="travel-rag"' in program
    assert '"search_travel_guides"' in program
    assert "storage_configuration={" in program
    assert '"type": "PINECONE"' in program
    assert '"bedrock:AssociateThirdPartyKnowledgeBase"' in program
    assert '"bedrock:ThirdPartyKnowledgeBaseCredentialsSecretArn"' in program
    assert '"bedrock:Retrieve"' in program
    assert '"bedrock:Rerank"' in program
    assert 'config.get("travel_rag_reranking_model_id")' in program
    assert 'config.get("travel_rag_evaluation_namespace")' in program
    assert 'config.get("travel_rag_evaluation_1024_namespace")' in program
    assert 'config.get("travel_rag_evaluation_1024_pinecone_index_host")' in program
    assert '"travel-rag-evaluation-1024-knowledge-base"' in program
    assert '"connection_string": evaluation_1024_pinecone_index_host' in program
    assert '"travel-rag-evaluation-knowledge-base"' in program
    assert (
        '"overrideSearchType": _search_type()'
        in (ROOT / "infrastructure" / "gateway" / "travel_rag_lambda.py").read_text()
    )
    assert "BucketVersioningV2" in program


def test_weather_tools_gated_in_source() -> None:
    source = (MCP_TOOLS / "server.py").read_text()
    assert 'os.environ.get("ENABLE_WEATHER_TOOLS", "true")' in source
    assert "if _ENABLE_WEATHER_TOOLS:" in source
    assert "mcp.tool()(searchLocation)" in source
    assert "stateless_http=_STATELESS_HTTP" in source


def test_gateway_smoke_matcher_handles_prefixed_names() -> None:
    scripts = ROOT / "infrastructure" / "scripts"
    sys.path.insert(0, str(scripts))
    import smoke_gateway_tools as smoke

    names = [
        "open-meteo-tools___searchLocation",
        "open-meteo-tools___getForecast",
        "mcp-tools___webSearch",
        "booking___Search_Hotels",
    ]
    assert smoke._matches(names, "searchLocation")
    assert smoke._matches(names, "getForecast")
    assert smoke._matches(names, "webSearch")
    assert smoke._matches(names, "booking___")
    assert not smoke._matches(names, "geoapify___")
    assert not smoke._matches(names, "findNearbyPlaces")
    assert not smoke._matches(names, "search_travel_guides")


def test_managed_and_runtime_prompts_share_the_same_neutral_baseline() -> None:
    managed = (ROOT / "agents" / "managed" / "system-prompt.md").read_text()
    runtime = (ROOT / "agents" / "custom" / "prompts" / "system.md").read_text()
    assert managed == runtime
    assert "## Skill selection" in managed
    assert "meaning and intent" in managed
    assert "not from literal\nkeyword matching" in managed


def test_managed_and_runtime_prompts_leave_skills_to_progressive_disclosure() -> None:
    managed = (ROOT / "agents" / "managed" / "system-prompt.md").read_text()
    runtime = (ROOT / "agents" / "custom" / "prompts" / "system.md").read_text()
    for prompt in (managed, runtime):
        assert "## Open-source skills" not in prompt
        assert "### Skill:" not in prompt
        assert "searchLocation" not in prompt
        assert "webSearch" not in prompt
        assert "code_interpreter" not in prompt


def test_managed_harness_pulumi_publishes_opensource_skills_to_s3() -> None:
    program = (ROOT / "infrastructure" / "pulumi" / "managed_agent" / "__main__.py").read_text()

    assert "aws_native.bedrockagentcore.Harness" in program
    assert 'opensource" / "agents" / "custom" / "skills' in program
    assert "harness-skills" in program
    assert "BucketObjectv2" in program
    assert "s3:GetObject" in program
    assert "s3:ListBucket" in program
    assert "skill_uris" in program
    assert 'skills=[{"s3": {"uri": skill_uri}} for skill_uri in skill_uris]' in program


def test_custom_runtime_loads_shared_skills_from_s3() -> None:
    agent = (ROOT / "agents" / "custom" / "agent.py").read_text()
    program = (ROOT / "infrastructure" / "pulumi" / "custom_agent" / "__main__.py").read_text()

    assert "SKILLS_S3_BUCKET" in agent
    assert "def _scan_s3_skills" in agent
    assert "def _build_skills_plugin" in agent
    assert "from strands import AgentSkills, Skill" in agent
    assert "AgentSkills(skills=skills, strict=True)" in agent
    assert "def _system_prompt_for" not in agent
    assert "system_prompt=_load_system_prompt()" in agent
    assert 'config.require("skills_bucket")' in program
    assert '"s3:ListBucket"' in program
    assert '"s3:GetObject"' in program
    assert "skills_bucket_read_policy" in program
