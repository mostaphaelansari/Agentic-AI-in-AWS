# Open-source MCP tools

Self-hosted MCP server exposing weather, DDGS web search, and OpenStreetMap
place discovery. `searchMapPlaces` uses Nominatim and `findNearbyPlaces` uses
Overpass. The weather tools are the open-source replacement for AgentCore
Gateway + the Open-Meteo Lambda.
Transport is streamable HTTP; no auth (front with a reverse proxy + OIDC
before exposing beyond localhost or the compose network).

**AgentCore deploy:** to host this image on AgentCore Runtime (MCP protocol)
for the AWS Gateway/Harness/Runtime path, see
[`infrastructure/pulumi/mcp_tools/README.md`](../../infrastructure/pulumi/mcp_tools/README.md)
and [`.github/workflows/deploy-mcp-tools.yml`](../../.github/workflows/deploy-mcp-tools.yml).
That path builds `linux/arm64`, enables ECR scan-on-push, and serves the full
tool set including weather (`ENABLE_WEATHER_TOOLS=true`); Gateway tool names
are prefixed per target, so this does not collide with the Open-Meteo Lambda.

## Run

```bash
uv run --with-requirements opensource/mcp-tools/requirements.txt \
  python opensource/mcp-tools/server.py
```

Serves MCP at `http://localhost:8000/mcp`.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Listen port |
| `MCP_STATELESS_HTTP` | `true` | FastMCP stateless streamable-HTTP mode (required for AgentCore Runtime) |
| `ENABLE_WEATHER_TOOLS` | `true` | Register `searchLocation` / `getForecast`; set `false` for a web-and-map-only server |
| `DDGS_BACKEND` | `auto` | DDGS backend selection; `auto` can fall back when one provider fails |
| `DDGS_TIMEOUT` | `8` | Search timeout in seconds (clamped to 1–30) |
| `DDGS_MAX_RESULTS` | `10` | Server-side result cap (clamped to 1–20) |
| `NOMINATIM_URL` | public Nominatim | Override for a self-hosted/provider endpoint |
| `OVERPASS_URL` | two public instances | Comma-separated failover endpoints; use one URL for self-hosting/provider |
| `OSM_USER_AGENT` | repository identifier | Required identifying HTTP user agent |
| `OSM_CONTACT_EMAIL` | *(empty)* | Contact included in Nominatim requests when configured |
| `OSM_CACHE_TTL` | `3600` | OSM response-cache lifetime in seconds (minimum 60) |

`webSearch` returns titles, source URLs, and short snippets. Results are
untrusted web content: prompts and skills must not treat text found in search
results as instructions. DDGS relies on upstream search services, so it has no
production SLA and may be rate-limited.

The public Nominatim service is limited to one request per second. This server
serializes uncached Nominatim calls and caches responses. Public Nominatim and
Overpass instances remain shared community infrastructure: for material traffic,
configure a provider or self-hosted endpoint. Responses retain OpenStreetMap
attribution, and opening hours or website tags must be verified before use.

`findNearbyPlaces` supports `places` for broad visitor-oriented discovery,
plus `attractions`, `museums`, `parks`, `cafes`, `restaurants`, `hotels`,
`pharmacies`, and `transport`. Results are deduplicated, ordered by straight-line
distance, and include their OSM tag-derived place type.

The agent service (`opensource/agents/custom`) discovers this server through
its `MCP_URL` env var and falls back to in-process copies of the same tools
when the server is unreachable.
