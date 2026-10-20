"""Pure helpers for AgentCore Gateway MCP-server target configuration.

RapidAPI's shared MCP host (`https://mcp.rapidapi.com`) requires both
`x-api-key` and `x-api-host`. AgentCore Gateway only injects one outbound
API-key header per target, so RapidAPI targets are fronted by API Gateway
HTTP APIs that statically inject `x-api-host` while the Gateway credential
provider injects `x-api-key`.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any


def runtime_mcp_endpoint(runtime_arn: str, aws_region: str) -> str:
    """Build the streamable-HTTP MCP invoke URL for an AgentCore Runtime."""
    encoded = urllib.parse.quote(runtime_arn, safe="")
    return (
        f"https://bedrock-agentcore.{aws_region}.amazonaws.com"
        f"/runtimes/{encoded}/invocations?qualifier=DEFAULT"
    )


@dataclass(frozen=True)
class RapidApiMcpSpec:
    """One RapidAPI MCP integration exposed through a Gateway target."""

    name: str
    host: str
    key_config_name: str
    fallback_key_config_names: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class RemoteMcpSpec:
    """Direct MCP-server target that only needs an API key header."""

    name: str
    endpoint: str
    key_config_name: str
    credential_parameter_name: str = "x-api-key"
    description: str = ""


RAPIDAPI_MCP_SPECS: tuple[RapidApiMcpSpec, ...] = (
    RapidApiMcpSpec(
        name="booking",
        host="booking-com15.p.rapidapi.com",
        key_config_name="rapidapi_key",
        description="Booking.com hotel search via RapidAPI MCP",
    ),
    RapidApiMcpSpec(
        name="aerodatabox",
        host="aerodatabox.p.rapidapi.com",
        key_config_name="aerodatabox_rapidapi_key",
        fallback_key_config_names=("flight_rapidapi_key", "rapidapi_key"),
        description="AeroDataBox flight/airport data via RapidAPI MCP",
    ),
    RapidApiMcpSpec(
        name="air-scraper",
        host="sky-scrapper.p.rapidapi.com",
        key_config_name="air_scraper_rapidapi_key",
        fallback_key_config_names=("flight_rapidapi_key", "rapidapi_key"),
        description="Air Scraper flight shopping via RapidAPI MCP",
    ),
)

GEOAPIFY_MCP_SPEC = RemoteMcpSpec(
    name="geoapify",
    endpoint="https://api.geoapify.com/v1/mcp",
    key_config_name="geoapify_api_key",
    description="Geoapify geocoding MCP",
)

RAPIDAPI_UPSTREAM = "https://mcp.rapidapi.com"
API_KEY_HEADER = "x-api-key"
API_HOST_HEADER = "x-api-host"

SECRET_CONFIG_NAMES: tuple[str, ...] = (
    "rapidapi_key",
    "aerodatabox_rapidapi_key",
    "air_scraper_rapidapi_key",
    "flight_rapidapi_key",
    "geoapify_api_key",
)


def resolve_key_config_name(
    present: set[str] | frozenset[str],
    primary: str,
    fallbacks: tuple[str, ...] = (),
) -> str | None:
    """Return the first present config name among primary and fallbacks."""
    for name in (primary, *fallbacks):
        if name in present:
            return name
    return None


def selected_rapidapi_specs(
    present: set[str] | frozenset[str],
) -> list[RapidApiMcpSpec]:
    """Return RapidAPI specs that have a resolvable API-key config name."""
    return [
        spec
        for spec in RAPIDAPI_MCP_SPECS
        if resolve_key_config_name(present, spec.key_config_name, spec.fallback_key_config_names)
    ]


def selected_remote_mcp_specs(
    present: set[str] | frozenset[str],
) -> list[RemoteMcpSpec]:
    """Return direct remote MCP specs that have a resolvable API-key config."""
    return [
        spec
        for spec in (GEOAPIFY_MCP_SPEC,)
        if resolve_key_config_name(present, spec.key_config_name)
    ]


def credential_provider_config_names(
    present: set[str] | frozenset[str],
) -> list[str]:
    """Unique Pulumi secret config names that should become Identity providers."""
    names: list[str] = []
    for spec in RAPIDAPI_MCP_SPECS:
        chosen = resolve_key_config_name(
            present, spec.key_config_name, spec.fallback_key_config_names
        )
        if chosen and chosen not in names:
            names.append(chosen)
    geo = resolve_key_config_name(present, GEOAPIFY_MCP_SPEC.key_config_name)
    if geo and geo not in names:
        names.append(geo)
    return names


def api_key_credential_provider_configuration(
    provider_arn: Any,
    *,
    parameter_name: str = API_KEY_HEADER,
) -> dict[str, Any]:
    """Pulumi credential_provider_configuration for HEADER API-key injection."""
    return {
        "api_key": {
            "provider_arn": provider_arn,
            "credential_location": "HEADER",
            "credential_parameter_name": parameter_name,
            "credential_prefix": "",
        }
    }


def mcp_server_target_configuration(endpoint: str) -> dict[str, Any]:
    """Pulumi target_configuration for a dynamic MCP-server target."""
    return {"mcp": {"mcp_server": {"endpoint": endpoint}}}


def rapidapi_proxy_request_parameters(host: str) -> dict[str, str]:
    """API Gateway HTTP_PROXY request parameters that inject x-api-host."""
    return {f"overwrite:header.{API_HOST_HEADER}": host}
