---
title: "Structured Output And JSON Validation"
date: 2026-06-24
as_of: 2026-06-24
last_verified: 2026-06-24
author: Mostapha EL ANSARI
tags:
  - aws
  - bedrock
  - agentcore
  - strands
  - json
  - schema-validation
  - reliability
status: internal
---

# Structured Output And JSON Validation

> [!summary]
> Structured output is a contract between a model and downstream software. **JSON mode** aims to produce syntactically valid JSON. **Schema-constrained output** also restricts its shape and types. **Application validation** checks business meaning and authorization. These are separate guarantees. For this repository, prefer Bedrock native structured outputs when the selected model and API path support them, validate the result again at the application boundary, and use bounded repair retries only for validation failures that a second model attempt can actually fix.

## Definition

Structured output means that a model response is intended for software consumption rather than direct display as unconstrained prose. The output usually follows JSON Schema, a typed application model, or a tool-input schema.

There are four distinct levels that are often incorrectly called “JSON mode”:

| Level | What is enforced | What can still fail |
|---|---|---|
| Prompt-only JSON | The prompt asks the model to return JSON | Markdown fences, commentary, truncated JSON, missing fields, wrong types, invented fields |
| JSON mode | The provider constrains output to valid JSON syntax | Required fields, enums, object shape, domain rules, factual correctness |
| JSON Schema structured output | The provider constrains output to a declared schema | Cross-field rules, authorization, source fidelity, business invariants, downstream state changes |
| Application validation | Code parses and validates schema plus domain rules | External state can still change between validation and commit; authorization and idempotency remain necessary |

Read these levels as increasing strength, not as interchangeable names for the same feature. Prompt-only JSON asks nicely. JSON mode improves syntax. Schema-constrained output improves shape and types. Application validation is still where this project checks business meaning, evidence, permissions, current state, and side-effect safety.

The reliability stack should therefore be read from bottom to top:

```text
Model generates a candidate
  -> provider guarantees JSON syntax or schema, when supported
  -> application parses into a typed object
  -> schema validation checks shape and types
  -> domain validation checks meaning and cross-field rules
  -> authorization and current-state checks decide whether an action is allowed
  -> side effect executes with idempotency and audit controls
```

The model's response is only a candidate. Provider constraints make it easier to parse, but the application still owns validation before any side effect.

### Valid JSON Is Not Valid Data

This object is valid JSON:

```json
{
  "city": "Paris",
  "temperature_c": "warm"
}
```

It is not valid for a schema that requires `temperature_c` to be a number. Even a schema-valid object can still be false, unauthorized, or unsafe:

```json
{
  "account_id": "another-tenant",
  "refund_amount": 1000000
}
```

JSON and schema validation answer:

```text
Can the program safely interpret this value in the expected shape?
```

They do not answer:

```text
Is the value true, permitted, current, or safe to commit?
```

### Output Schemas And Tool Schemas Are Different Contracts

An **output schema** defines the final machine-readable response. A **tool schema** defines the arguments the model may send to one tool. A model can generate valid tool arguments and still produce an invalid final response, or the reverse.

For an agent workflow, validate both boundaries:

```text
Model -> tool arguments -> tool execution -> tool result -> final structured response
          validate            validate/normalize          validate
```

## Why It Matters

Unstructured model text becomes dangerous when another component treats it as a command, event, database record, API request, routing decision, or policy result. A single missing field or invented enum can turn a fluent answer into a production failure.

Structured output improves:

- **Reliability:** downstream code receives predictable keys and types.
- **Security:** unknown fields and unexpected values can be rejected before execution.
- **Compatibility:** schema versions make producer/consumer changes explicit.
- **Observability:** failures can be counted by parse, schema, domain, and execution stage.
- **Evaluation:** exact structural assertions can be tested without an LLM judge.
- **Cost and latency:** provider-constrained output avoids many model repair calls caused by prompt-only JSON.

It also creates a clearer ownership boundary:

| Concern | Primary owner |
|---|---|
| JSON syntax | Provider JSON mode or application parser |
| Required keys and types | JSON Schema, provider structured output, Pydantic, or equivalent validator |
| Cross-field consistency | Application validators |
| Claims supported by source data | Retrieval/grounding and deterministic evidence checks |
| Tool permission | AgentCore Policy or application authorization |
| Current balance, inventory, workflow state | System of record at commit time |
| Safe retries and duplicate prevention | Application orchestration and idempotency controls |

## When It Bites

### Prompt-Only JSON Looks Reliable In Demos

“Return only JSON” often works on easy prompts and then fails under long context, adversarial input, model changes, tool use, or output truncation. Typical failures include:

- prose before or after the object;
- fenced code blocks around JSON;
- single quotes, comments, trailing commas, or unescaped control characters;
- omitted required properties;
- a string where a number or boolean is required;
- extra fields that downstream code accidentally trusts;
- a correct prefix cut off by the output-token limit.

For example, a model might return a visually helpful fenced block:

````text
```json
{"city": "Paris", "temperature_c": 18}
```
````

or a human-friendly preface:

```text
Sure, here is the JSON:
{"city": "Paris", "temperature_c": 18}
```

Both can be reasonable chat answers and still fail a strict JSON parser because the complete response is not one raw JSON payload. A prompt-only contract also cannot stop schema problems such as `{"temperature_c": "warm"}` when the application expects a number.

Do not use regex to extract the first `{...}` block. Nested objects, braces inside strings, and multiple candidates make that approach brittle. Parse the complete expected payload with a real JSON parser.

### Schema-Valid Output Can Be Semantically Wrong

A schema can require `unit` to be `celsius` or `fahrenheit`; it cannot prove that `18` was actually returned by the weather service. It can require an `account_id`; it cannot prove that the caller owns the account.

Domain checks remain mandatory for:

- totals that must equal the sum of line items;
- start dates that must precede end dates;
- identifiers that must exist and belong to the current tenant;
- values that must match tool-returned evidence;
- operations that require approval or a current system-of-record check;
- fields whose allowed values depend on workflow state.

### The Schema Itself Can Be Invalid Or Unsupported

Amazon Bedrock validates schemas against a supported subset of JSON Schema Draft 2020-12. Supported features include basic types, enums, constants, limited composition, internal references, common string formats, and limited `minItems`. Current documented exclusions include recursive schemas, external references, numeric limits such as `minimum`, string length limits, and `additionalProperties` values other than `false`.

This matters when generating a schema from Pydantic or another type system: the generated schema may be valid JSON Schema but still contain keywords Bedrock does not support. Treat provider-schema compilation as a deployment check, not as something to discover for the first time on production traffic.

### First Use Can Have Different Latency

Bedrock compiles a grammar for a new schema. AWS documents that first-time compilation can take up to a few minutes; successful grammars are cached for 24 hours, and identical schemas in the same account reuse the cache. Prefer stable, named, versioned schemas such as `weather-answer.v1`, `refund-command.v1`, or `comparison-report.v1` instead of generating a slightly different schema per request.

### Streaming Is Not A Validated Object Yet

A partial JSON stream is usually not parseable, and an early field can be superseded by later output. Do not execute actions or publish a structured result from text deltas. Buffer until the provider reports completion, parse and validate, then release the object.

Until the final chunk arrives, the object is incomplete. Even if an early fragment appears to say `action: refund`, the final response might later change direction, fail validation, be truncated, or be replaced by a refusal. Progressive display is acceptable for user experience only when the partial content is clearly provisional and cannot trigger a side effect.

### Truncation And Refusal Need Their Own Paths

A response stopped by a maximum-token limit is not an ordinary parse error. Increasing the budget, shrinking the schema, or reducing requested content is more useful than asking the model to “fix the JSON.” Likewise, safety refusal, guardrail intervention, throttling, and provider errors should not be fed back as schema mistakes.

Classify before retrying:

| Failure class | Example | Correct response |
|---|---|---|
| Request/schema error | Unsupported schema keyword returns HTTP 400 | Fix the schema; do not retry unchanged |
| Transient provider error | Throttling or temporary service failure | SDK/backoff retry with idempotency |
| Truncation | Output reaches token limit | Reduce output or increase budget within policy |
| JSON parse failure | Prompt-only output contains prose or malformed JSON | One bounded repair attempt or fail closed |
| Schema failure | Missing field or wrong enum | Return concise validation errors for bounded repair |
| Domain failure | End date before start date | Repair only if intent is unambiguous; otherwise ask or reject |
| Authorization/state failure | Tenant mismatch or stale balance | Reject and re-read state; never ask the model to override it |

### Blind Retries Multiply Cost And Risk

Each repair attempt is another model call, with more input tokens, latency, and another chance to drift from the original evidence. An unbounded retry loop can also hide a broken schema or incompatible model.

Use a strict retry budget, normally zero retries for native schema-constrained output and at most one or two repair attempts for prompt/tool-based structured output. Record every attempt. If the workflow can produce side effects, generate and validate the final command before executing anything; never replay a non-idempotent side effect merely because response parsing failed.

### Schema Evolution Breaks Consumers Quietly

Adding a required field is a breaking change. Renaming an enum can break routing. Allowing arbitrary extra fields can create mass-assignment vulnerabilities or accidental behavior changes.

Version schemas as contracts:

```json
{
  "schema_version": "weather-answer.v1",
  "location": "Paris",
  "temperature_c": 18.2
}
```

Use `additionalProperties: false` where supported, keep old consumers in mind, and test producer and consumer changes together.

## AWS Option Vs Do-It-Yourself

| Need | AWS option | Do-it-yourself option | Practical note |
|---|---|---|---|
| Schema-constrained final output | Bedrock structured outputs with `outputConfig.textFormat`, provider-specific `output_config.format`, or `response_format` | Prompt for JSON, parse, validate, and repair | Prefer native constraints when the model/API supports them. |
| Strict tool arguments | Bedrock tool definition with `strict: true` | Validate every tool input before execution and return errors to the loop | Keep application authorization even with strict schemas. |
| Plain JSON mode | Available on documented provider/model-specific paths such as supported imported models | Prompt-only JSON plus a parser | JSON syntax alone is weaker than schema adherence. |
| Typed Python result | Strands `structured_output_model` with Pydantic | Call Bedrock directly and validate with Pydantic/jsonschema | Strands' abstraction is convenient but is not the same mechanism as Bedrock native output schema. |
| Schema validation | Bedrock validates its supported schema and constrains generation | Pydantic, `jsonschema`, or another validator | Application validation is still useful for defense in depth and business rules. |
| Repair after validation failure | Strands can return Pydantic validation errors through its structured-output tool flow | Build an explicit bounded repair loop | Separate validation retries from transient API retries. |
| Workflow orchestration | AgentCore Runtime hosts the code; CloudWatch/OTel captures telemetry | Custom service, queue, and trace stack | Runtime does not define the output contract for the application. |
| Authorization | AgentCore Policy for governed Gateway calls | Application middleware or policy engine | Schema validity never grants permission. |

### Bedrock Native Structured Outputs

Amazon Bedrock currently exposes two complementary controls:

1. **JSON Schema output format** constrains the model's text response.
2. **Strict tool use** constrains the selected tool name and tool input against its schema.

For the Converse APIs, final-output configuration uses `outputConfig.textFormat`. For Anthropic Claude through `InvokeModel`, it uses `output_config.format`. Open-weight model request shapes use `response_format`. AWS documents support across Converse, ConverseStream, InvokeModel, InvokeModelWithResponseStream, cross-Region inference, and batch inference, subject to model support.

The Anthropic-style Messages API on the `bedrock-mantle` endpoint does not accept this structured-output parameter. AWS also documents that Anthropic citations and structured outputs cannot be enabled together. Confirm the exact model, endpoint, region, and feature combination before standardizing a shared client.

The model configured in this repository is `eu.anthropic.claude-sonnet-4-6`. Its Bedrock model card lists:

- `bedrock-runtime`, Invoke, and Converse support;
- structured outputs support;
- the EU geo inference ID used by this project.

That makes native Bedrock structured output a viable option for a dedicated machine-readable route in this project.

When using Bedrock native structured output, keep the schema small, named, versioned, and compatible with Bedrock's supported JSON Schema subset. Validate the final text again with Pydantic or an equivalent application validator. Do not automatically pass `model_json_schema()` to Bedrock without checking that its keywords fit Bedrock's supported subset.

### Strands Structured Output

The custom agent uses `strands-agents==1.42.0`, `Agent`, and `BedrockModel`. Strands' Python structured-output API accepts a Pydantic model through `structured_output_model`. In the installed Strands implementation, the Pydantic type is converted into a structured-output tool. This is convenient when structured output follows tool work, but it is not proof that Bedrock's native `outputConfig.textFormat` grammar was used.

Choose deliberately:

| Route | Prefer it when |
|---|---|
| Bedrock native JSON Schema | The endpoint is AWS-specific, strict schema adherence matters, and the model/API combination supports it |
| Strands `structured_output_model` | The result follows an agent/tool loop and framework portability or Pydantic integration matters |
| DIY parser and validator | The provider lacks native support, a legacy contract must be maintained, or custom repair behavior is required |

In all three routes, keep deterministic business validation after the model result.

### Repository Fit

The custom path currently does this:

```text
Streamlit comparison app
  -> AgentCore Runtime payload containing a free-form prompt
  -> agents/custom/agent.py builds Strands Agent + BedrockModel
  -> model and tools emit text/tool/reasoning events
  -> runtime emits SSE events to the comparison UI
```

The current invocation does not pass a `structured_output_model`, and `_bedrock_model_kwargs()` does not configure Bedrock native final-output `outputConfig.textFormat`. The final user response is therefore streamed prose, not a documented structured-output contract.

This is appropriate for the current chat UI. Do not force every conversational response into JSON. Add structured output only where another program consumes the result, such as:

- an evaluation result artifact;
- a normalized weather record;
- a routing or classification decision;
- a workflow command proposed for later authorization;
- a machine-readable comparison report;
- tool arguments that must meet a strict contract.

`agents/custom/agent.py` patches the incoming Starlette request parser to use `json.loads(..., strict=False)` so raw control characters in free-form prompts do not fail before the handler runs. That is transport input parsing, not model-output validation.

For a new machine-readable route:

```text
1. Define a small versioned Pydantic model and a Bedrock-compatible JSON Schema.
2. Set extra fields to forbidden / additionalProperties to false.
3. Choose Bedrock native structured output for direct inference, or Strands structured output after an agent loop.
4. Buffer the result until the invocation completes.
5. Validate again with Pydantic and run domain/evidence checks.
6. Emit one typed success event or one typed failure event.
7. Keep side effects behind authorization, current-state checks, and idempotency.
8. Measure validation failures and retries by model and schema version.
```

Use an explicit SSE event type, such as `structured_result`, so consumers never guess whether `content` is prose or JSON.

Adoption call: helpful now for dedicated machine-to-machine boundaries; not helpful as a blanket replacement for the current chat response.



## Sources

- [Amazon Bedrock: Get validated JSON results from models](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
- [Amazon Bedrock API: OutputConfig](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_OutputConfig.html)
- [Amazon Bedrock API: ToolSpecification and strict tool use](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ToolSpecification.html)
- [Amazon Bedrock model card: Anthropic Claude Sonnet 4.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)
- [Amazon Bedrock: Advanced API features for imported models](https://docs.aws.amazon.com/bedrock/latest/userguide/custom-model-import-advanced-features.html)
- [Strands Agents: Structured Output](https://strandsagents.com/latest/documentation/docs/user-guide/concepts/agents/structured-output/)
- [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12)
- [Pydantic: Models](https://docs.pydantic.dev/latest/concepts/models/)
