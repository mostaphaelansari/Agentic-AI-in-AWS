# Tool Backends, Lambda, MCP, and Open-Source Execution

## Overview

The tools in this project do not all require AWS Lambda.

Lambda is used only when a backend needs an adapter between the AgentCore Gateway and an upstream API that does not provide an MCP-compatible interface.

The other tools can be called directly because they already have one of the following:

* A callable MCP server
* An HTTP service compatible with the Gateway
* An external MCP provider
* A local Python implementation in the open-source setup

From the agent's perspective, all Gateway-hosted tools are exposed uniformly through a single MCP endpoint.

---

## The Three Backend Patterns

| Tools                               | Backend                             | How the Gateway calls it |
| ----------------------------------- | ----------------------------------- | ------------------------ |
| `getForecast`, `searchLocation`     | Our Python Lambda                   | Lambda ARN               |
| OpenStreetMap and web-search tools  | Our MCP server on AgentCore Runtime | MCP HTTPS endpoint       |
| Booking, flight, and Geoapify tools | External MCP or API providers       | Remote HTTPS endpoint    |

The main architecture is:

```text
                         ┌─ Open-Meteo → Lambda
Agent → one Gateway URL ─┼─ OSM/search → AgentCore Runtime
                         ├─ Booking/flights → RapidAPI
                         └─ Geoapify → remote MCP server
```

Lambda is therefore not the general mechanism used to call every function. It is one possible backend adapter.

---

# 1. Open-Meteo Uses Lambda in the AWS Setup

Open-Meteo provides ordinary REST endpoints. It does not directly provide the MCP interface expected by the AgentCore Gateway configuration.

Our Lambda translates MCP tool invocations into Open-Meteo REST requests:

```text
Gateway tool call
  → Lambda
  → https://api.open-meteo.com/...
  → Lambda returns JSON
```

The translation is implemented in:

[open_meteo_lambda.py](/home/a953083/github/Aws-agentic-blueprint/infrastructure/gateway/open_meteo_lambda.py:25)

Conceptually, the Lambda selects the appropriate Open-Meteo endpoint based on the requested tool:

```python
if tool_name == "getForecast":
    url = "https://api.open-meteo.com/v1/forecast?" + ...
```

For example, the Gateway may invoke the Lambda with arguments such as:

```json
{
  "latitude": 48.8566,
  "longitude": 2.3522,
  "current_weather": true
}
```

The Lambda translates those arguments into a request similar to:

```text
https://api.open-meteo.com/v1/forecast?latitude=48.8566&longitude=2.3522&current_weather=true
```

The complete request path is:

```text
Agent asks for weather
        ↓
AgentCore Gateway invokes Lambda
        ↓
Lambda constructs an Open-Meteo HTTP request
        ↓
Open-Meteo returns weather data
        ↓
Lambda returns JSON to the Gateway
        ↓
The result returns to the agent
```

---

## What AWS Lambda Is

AWS Lambda runs a small piece of code in AWS without requiring us to create or manage a traditional server.

A Lambda function can look like this:

```python
def lambda_handler(event, context):
    name = event["name"]
    return {"message": f"Hello, {name}"}
```

AWS executes the function whenever another system invokes it.

Lambda is useful for short, event-driven operations such as:

* Processing an API request
* Reacting when a file is uploaded
* Handling a queue message
* Running scheduled work
* Transforming data between systems
* Implementing a tool that an AI agent can call

AWS starts enough Lambda instances to handle incoming requests. Billing is generally based on the number of invocations and the amount of execution time.

---

## Why Use Lambda for Open-Meteo?

The Lambda creates a controlled boundary between the agent and the external Open-Meteo API.

It allows us to:

* Expose only approved operations
* Validate tool arguments
* Translate tool inputs into REST parameters
* Hide upstream API details from the agent
* Normalize responses
* Handle errors consistently
* Monitor requests through AWS
* Control invocation permissions through the Gateway

The work is simple and short:

```text
Receive parameters
  → call an external API
  → return JSON
```

That makes it a suitable Lambda workload.

---

## Lambda Versus a Normal Server

| Lambda                                 | Normal server or container                 |
| -------------------------------------- | ------------------------------------------ |
| Starts when invoked                    | Usually remains running                    |
| AWS manages the underlying machines    | More runtime configuration must be managed |
| Good for short, independent operations | Good for long-running or complex services  |
| Automatically scales per request       | Scaling must be configured                 |
| May experience cold-start delay        | Usually remains immediately available      |
| Has execution and runtime limits       | Offers more runtime flexibility            |

In this project, Lambda fits the Open-Meteo adapter because the operation is short and stateless.

The larger MCP tool collection instead uses AgentCore Runtime because it contains several tools and behaves as a complete service.

---

# 2. The Larger Tool Collection Uses AgentCore Runtime

The repository contains a complete MCP server in:

[server.py](/home/a953083/github/Aws-agentic-blueprint/opensource/mcp-tools/server.py:1)

This server contains tools such as:

* Web search
* OpenStreetMap place search
* Nearby-place lookup
* Open-Meteo operations in the open-source configuration
* Other shared utility tools

Because it is already an MCP server, the AgentCore Gateway can call it directly over HTTP:

```text
Agent
  → AgentCore Gateway
  → AgentCore Runtime MCP endpoint
  → mcp-tools server
```

The Gateway target configuration is conceptually:

```python
target_configuration={
    "mcp": {
        "mcp_server": {
            "endpoint": mcp_tools_endpoint
        }
    }
}
```

This configuration appears in:

[**main**.py](/home/a953083/github/Aws-agentic-blueprint/infrastructure/pulumi/gateway/__main__.py:267)

---

## Why Runtime Instead of Separate Lambdas?

The MCP tools server is a multi-tool Python application with:

* An existing MCP protocol implementation
* Several tools in one process
* Shared code and dependencies
* Streamable HTTP handling
* Shared initialization logic
* More control over execution
* A single deployment unit

Turning each function into a separate Lambda would require:

* A Lambda wrapper for every function
* Separate deployment configuration
* Individual tool schemas
* Additional routing configuration
* Repeated dependency packaging
* More operational overhead

That additional complexity would provide little benefit because the tools already work as one MCP service.

---

# 3. External Providers Are Called Remotely

Some tools are already implemented and hosted by external providers.

These include:

* Booking tools
* AeroDataBox tools
* Air Scraper tools
* Geoapify tools

Their MCP targets are defined in:

[mcp_targets.py](/home/a953083/github/Aws-agentic-blueprint/infrastructure/pulumi/gateway/mcp_targets.py:29)

They are connected to the Gateway in:

[**main**.py](/home/a953083/github/Aws-agentic-blueprint/infrastructure/pulumi/gateway/__main__.py:265)

Because these providers already expose callable remote services, we do not need to place Lambda between the Gateway and the provider.

---

## RapidAPI-Based Booking and Flight Tools

Booking and flight tools are exposed through RapidAPI's MCP endpoint.

The request path is:

```text
Agent
  → AgentCore Gateway
  → API Gateway header proxy
  → https://mcp.rapidapi.com
```

API Gateway is used only to add RapidAPI's required `x-api-host` header.

The AgentCore Gateway adds the API key used to authenticate the request.

The external provider then executes the selected function and returns the result.

---

## Geoapify

Geoapify exposes a remote MCP endpoint directly:

```text
Agent
  → AgentCore Gateway
  → https://api.geoapify.com/v1/mcp
```

No Lambda adapter is needed because Geoapify already understands the required request format.

---

# 4. Functions, Tools, MCP Servers, and the Gateway

The important concepts are related but distinct.

* A **function** is code that performs an operation.
* A **tool** is a function described in a format that an AI agent can discover and call.
* An **MCP server** publishes one or more tools using the Model Context Protocol.
* The **AgentCore Gateway** combines tools from several backends behind one address.
* A **Lambda** is one possible place where the code behind a tool can execute.

Lambda and MCP are not direct alternatives.

Lambda is an execution environment. MCP is a protocol for describing, discovering, and invoking tools.

A tool may execute through:

* Lambda
* An MCP server in a container
* A remote provider's MCP server
* A local Python function
* Another HTTP-compatible backend

---

## How an MCP Server Describes Tools

An MCP server publishes a tool catalog such as:

```text
search_places
get_nearby_places
web_search
search_hotels
find_flights
```

For every tool, it describes:

* The tool's name
* What it does
* Its required parameters
* The parameter types
* Its input schema
* How the tool can be invoked

A simplified definition may look like this:

```json
{
  "name": "search_places",
  "description": "Search for geographic places",
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string"
      }
    },
    "required": ["query"]
  }
}
```

The model can then produce a tool call such as:

```json
{
  "name": "search_places",
  "arguments": {
    "query": "Eiffel Tower"
  }
}
```

MCP does not replace the underlying function. It standardizes how that function is described, discovered, and called.

---

# 5. Our MCP Functions

The repository's MCP server contains ordinary Python functions registered as tools.

Conceptually, a tool may be declared like this:

```python
@mcp.tool()
def search_places(query: str):
    # Perform the search
    return results
```

The registration turns the function into a discoverable MCP tool.

The deployment path is:

```text
Python functions
      ↓ registered with MCP
Our MCP server
      ↓ deployed as a container
AgentCore Runtime
      ↓ attached as a target
AgentCore Gateway
```

One MCP server can publish many functions, so there is no need to create one Lambda for every tool.

---

# 6. External MCP Functions

Some tools are implemented and hosted by other providers.

For these tools, we configure only:

* The provider endpoint
* Authentication credentials
* Required headers
* Gateway routing

The request flow is:

```text
Our agent
   → our AgentCore Gateway
   → provider's MCP server
   → provider executes its function
   → result returns to our agent
```

We do not own or run the provider's underlying implementation.

---

# 7. How the Agent Discovers All Tools

The custom agent connects to the AgentCore Gateway using a single MCP client:

```python
mcp = get_gateway_mcp_client(gateway_url)
tools = mcp.list_tools_sync()
```

This code appears in:

[agent.py](/home/a953083/github/Aws-agentic-blueprint/agents/custom/agent.py:605)

The authenticated Gateway client is created in:

[gateway.py](/home/a953083/github/Aws-agentic-blueprint/agents/custom/mcp_client/gateway.py:39)

The call:

```python
mcp.list_tools_sync()
```

asks the Gateway:

> Which tools are currently available?

The Gateway collects tool definitions from all configured targets and returns one combined list.

Conceptually, the result looks like this:

```text
Gateway
├── Open-Meteo Lambda
│   ├── searchLocation
│   └── getForecast
├── Our MCP server
│   ├── web_search
│   ├── search_places
│   └── get_nearby_places
├── Booking MCP
│   └── hotel tools
├── Flight MCP servers
│   └── flight and airport tools
└── Geoapify MCP
    └── geocoding tools
```

The actual names may include target prefixes such as:

```text
open-meteo-tools___searchLocation
open-meteo-tools___getForecast
mcp-tools___search_places
booking___search_hotels
aerodatabox___...
geoapify___...
```

The prefix tells the Gateway which target owns the tool.

---

# 8. How a Tool Is Called

Suppose the user asks:

> Find hotels near the Eiffel Tower.

The execution may proceed as follows:

1. The model examines the available tool descriptions.
2. It selects a place-search tool.
3. The MCP client sends a `tools/call` request to the Gateway.
4. The Gateway examines the tool prefix.
5. It routes the request to the corresponding backend.
6. The backend executes the underlying function.
7. The result returns to the model.
8. The model may call a Booking tool using the location result.
9. The model produces a final answer for the user.

```text
User
  ↓
Model chooses a tool
  ↓
MCP client sends tools/call
  ↓
AgentCore Gateway routes by tool name
  ↓
Backend executes the function
  ↓
JSON result returns to the model
  ↓
Model answers the user
```

The agent does not need to know whether the backend is:

* Lambda
* AgentCore Runtime
* RapidAPI
* Geoapify
* Another remote MCP service

It calls every Gateway-hosted tool through the same MCP client and the same `GATEWAY_URL`.

---

# 9. The Open-Source Setup

In the open-source setup, Open-Meteo is called directly by Python code.

The AWS Lambda and AgentCore Gateway are replaced by a normal MCP server or by local Python tools.

```text
AWS version:
Agent → AgentCore Gateway → Lambda → Open-Meteo API

Open-source MCP version:
Agent → local or hosted MCP server → Open-Meteo API

Open-source local-tool version:
Agent process → local Python function → Open-Meteo API
```

Neither open-source path requires:

* AWS Lambda
* The AWS SDK
* AgentCore Gateway
* An AWS account

The agent process or MCP server provides the required compute.

---

# 10. Open-Meteo Functions in the Open-Source MCP Server

The Open-Meteo functions are implemented in:

[server.py](/home/a953083/github/Aws-agentic-blueprint/opensource/mcp-tools/server.py:385)

The location-search function is conceptually:

```python
def searchLocation(
    name: str,
    count: int = 3,
    language: str = "en",
) -> str:
    params = {
        "name": name,
        "count": count,
        "language": language,
    }

    url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        + urllib.parse.urlencode(params)
    )

    data = _fetch(url)
    return json.dumps(data)
```

The forecast function works in the same way:

```python
def getForecast(latitude, longitude, ...):
    params = {
        "latitude": latitude,
        "longitude": longitude,
        # Additional forecast parameters
    }

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(params)
    )

    return json.dumps(_fetch(url))
```

The functions use Python's standard HTTP client:

```python
urllib.request.urlopen(url)
```

The Python code therefore calls Open-Meteo directly.

---

# 11. How the Open-Source Functions Become MCP Tools

The functions are registered with FastMCP:

```python
mcp.tool()(searchLocation)
mcp.tool()(getForecast)
```

This registration tells the MCP server to publish the Python functions as tools that an AI agent can discover and invoke.

FastMCP can generate tool definitions from:

* Function names
* Function parameters
* Python type annotations
* Default values
* Docstrings

The agent may discover a definition approximately like this:

```json
{
  "name": "searchLocation",
  "description": "Resolve a city name to latitude and longitude candidates",
  "inputSchema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string"
      },
      "count": {
        "type": "integer"
      },
      "language": {
        "type": "string"
      }
    },
    "required": ["name"]
  }
}
```

The model can then invoke it with:

```json
{
  "name": "searchLocation",
  "arguments": {
    "name": "Paris",
    "count": 3,
    "language": "en"
  }
}
```

---

# 12. How the Open-Source MCP Server Runs

The server is started using Streamable HTTP transport:

```python
mcp.run(transport="streamable-http")
```

By default, it listens at an endpoint similar to:

```text
http://localhost:8000/mcp
```

The sequence is:

1. Run `opensource/mcp-tools/server.py`.
2. FastMCP starts an HTTP server.
3. The agent connects to the `/mcp` endpoint.
4. The agent requests the available tools.
5. FastMCP reports `searchLocation` and `getForecast`.
6. The model selects one of the tools.
7. FastMCP executes the corresponding Python function.
8. The function calls Open-Meteo directly.
9. The result returns through the MCP server to the model.

Example:

```text
User: “What is the weather in Paris?”
                  ↓
Agent calls searchLocation(name="Paris")
                  ↓
FastMCP executes the Python function
                  ↓
GET geocoding-api.open-meteo.com
                  ↓
Coordinates return to the agent
                  ↓
Agent calls getForecast(latitude=..., longitude=...)
                  ↓
GET api.open-meteo.com
                  ↓
Forecast returns to the agent
```

---

# 13. Local Tool Fallback

The custom open-source agent also contains local copies of the Open-Meteo functions in:

[tools.py](/home/a953083/github/Aws-agentic-blueprint/opensource/agents/custom/tools.py:30)

These are local Strands tools rather than MCP tools.

The agent can execute them directly inside its own process:

```text
Agent process
  → local Python function
  → Open-Meteo API
```

The service selects the local functions with logic similar to:

```python
selected_tools = [
    tools.searchLocation,
    tools.getForecast,
]
```

This configuration is located in:

[service.py](/home/a953083/github/Aws-agentic-blueprint/opensource/agents/custom/service.py:691)

The open-source version therefore supports two Open-Meteo execution paths:

```text
1. Agent → MCP server → Open-Meteo API

2. Agent → local Python tool → Open-Meteo API
```

Neither path needs Lambda.

---

# 14. AWS and Open-Source Comparison

| Concern              | AWS configuration            | Open-source configuration                     |
| -------------------- | ---------------------------- | --------------------------------------------- |
| Tool discovery       | AgentCore Gateway            | MCP server or local tool registration         |
| Open-Meteo execution | AWS Lambda                   | MCP server or agent process                   |
| Tool protocol        | MCP through Gateway          | MCP directly, or local Strands tools          |
| Compute              | Lambda and AgentCore Runtime | Local process or hosted container             |
| Gateway              | AgentCore Gateway            | Optional                                      |
| AWS account required | Yes                          | No                                            |
| Open-Meteo HTTP call | Performed by Lambda          | Performed directly by Python                  |
| Multiple tools       | Aggregated by Gateway        | Published by one MCP server or loaded locally |

---

# 15. Final Architecture Summary

The AWS setup uses several backend types behind one Gateway:

```text
Agent
  ↓
One MCP client
  ↓
GATEWAY_URL
  ↓
AgentCore Gateway
  ├── Lambda adapter for Open-Meteo
  ├── AgentCore Runtime for our MCP server
  ├── RapidAPI MCP endpoints
  └── Geoapify MCP endpoint
```

The open-source setup removes the AWS-specific routing layer:

```text
Agent
  ├── local or hosted MCP server
  │      └── Python functions call external APIs
  │
  └── local Strands tools
         └── Python functions call external APIs
```

The central distinction is:

> Lambda is an execution mechanism, while MCP is a protocol for exposing and calling tools.

A tool does not inherently require Lambda. Lambda is useful when we need a small AWS-hosted adapter, as with Open-Meteo in the AgentCore Gateway deployment.

Tools that already exist behind an MCP or HTTP service can be called directly. In the open-source setup, the same operations can run inside a normal MCP server or directly inside the agent process.
