
# Caching: Prompt, Semantic, and Tool-Resource

> [!summary]
> Caching in agentic systems is not one feature. Prompt caching reuses model-side work for identical prompt prefixes. Semantic caching reuses a prior answer for a similar-enough request. Tool-resource caching reuses downstream tool results. Each layer saves a different bill line and creates a different correctness risk.

## Definition

Caching stores reusable work so a later request can avoid recomputing it.

In this project, the useful caching layers are:

| Cache layer | Cached object | Primary savings | Main risk |
|---|---|---|---|
| Prompt caching | Stable prompt prefixes such as system prompts, tool schemas, static instructions, and repeated context | Lower input-token cost and latency for repeated prefixes | Prefix churn causing silent misses, or misreading cache metrics as normal input |
| Semantic caching | A meaning-aware lookup from normalized query/embedding to a prior final answer | Avoided model calls, retrieval, tool execution, and agent runtime duration | False-positive answer, tenant leakage, stale answer |
| Tool-resource caching | Tool outputs, API responses, retrieved docs, browser snapshots, files, or resource reads | Fewer Gateway/tool/downstream calls and shorter sessions | Serving stale external data or caching side effects |

The layers compose, but they do not replace each other:

```text
user question
   |
   v
semantic cache
   | hit: return stored answer, skip agent
   v miss
agent loop
   prompt cache per model invocation
   tool-resource cache per tool call
   downstream API / Lambda / MCP server on miss
```

Prompt caching helps even when every user question is new, because fixed instructions and tool schemas are reused. Semantic caching helps when users ask equivalent questions. Tool-resource caching helps when the same external resource is fetched repeatedly.

## Why It Matters

Agents amplify repeated work. A single user prompt can trigger multiple model turns, repeated tool schemas, Gateway calls, browser actions, retrieval calls, and context reloads. Without caching, the same instructions and resources are paid for again and again.

Caching can reduce:

- Bedrock input-token cost
- AgentCore Runtime duration
- Gateway operation count
- Browser and code-interpreter work
- External API calls and quotas
- Retrieval load and vector database reads
- User-visible latency

Caching also matters because a wrong cache hit is worse than a cache miss. A cached answer can bypass the reasoning, retrieval, permission check, or freshness check that would have caught an error.

## Where It Bites First

| Use case | Cache layer that matters first | Why |
|---|---|---|
| Agent with large system prompts and many tools | Prompt caching | Tool schemas and fixed instructions are repeatedly sent to the model. |
| Multi-step AgentCore Runtime loops | Prompt caching plus tool-resource caching | Each step reuses the prompt prefix, and tools may refetch the same resource. |
| FAQ, support, policy, and documentation Q&A | Semantic caching | Many users ask the same intent with different wording. |
| Weather, pricing, inventory, and status tools | Tool-resource caching with short TTL | External resources are expensive or slow but time-sensitive. |
| Browser agents | Tool-resource caching | The same pages, screenshots, and extracted text may be revisited. |
| RAG over stable documents | Prompt caching plus retrieval/result caching | Retrieved context may be stable across related questions. |
| Benchmarks and architecture comparisons | Disable semantic caching | A semantic hit measures the cache, not the stack under test. |

## AWS-Managed Option vs Do-It-Yourself

| Dimension | AWS-managed option | Do-it-yourself option |
|---|---|---|
| Prompt caching | Amazon Bedrock prompt caching for supported models. Strands can add cache points automatically for supported providers. | Manually shape prompts for stable prefixes, or self-host models with prefix/KV caching if you already own inference. |
| Semantic caching | No single turnkey AgentCore semantic-cache switch. AWS provides managed substrates and guidance, especially ElastiCache for Valkey vector search, MemoryDB, OpenSearch, and Bedrock embeddings. | Implement embed, search, threshold, serve/store, TTL, metadata filters, and invalidation yourself or through a framework/library. |
| Tool-resource caching | Gateway centralizes tool access and stores/parses/indexes tool definitions, but result caching is usually application/tool specific. API Gateway or CloudFront can help for HTTP-style tools. | Cache tool responses in ElastiCache/Redis, DynamoDB with TTL, S3, local memoization, or inside the Lambda/tool wrapper. |
| Invalidation | Provider prompt-cache TTL is managed by Bedrock/provider. | You own TTL, freshness, version keys, permission scoping, and invalidation events. |
| Observability | Bedrock emits cache token metrics when supported. | You must emit cache hit/miss, stale-hit, bypass, and invalidation metrics. |
| Safety | Managed prompt cache is prefix-based and does not return old final answers directly. | Semantic/tool caches can return stale or unauthorized business content unless guarded. |

Managed prompt caching is the safest layer because it only reuses model-internal work for repeated prefixes. Semantic and tool-resource caches are more powerful, but they require explicit correctness rules.

## Cache Models

### Prompt Caching

Prompt caching is provider-side reuse of stable prompt prefixes. In Bedrock, cache checkpoints mark contiguous sections of the prompt that can be reused if the prefix matches on later calls.

Typical cacheable prefix:

```text
system prompt
tool definitions
JSON schemas
static instructions
few-shot examples
stable documentation excerpts
```

What it saves:

```text
input processing cost
input latency
repeated schema/instruction processing
```

What it does not do:

```text
It does not skip the model call.
It does not return a previous final answer.
It does not cache arbitrary tool results.
```

Important mechanics:

| Mechanic | Practical note |
|---|---|
| Prefix match | Static content must be placed before dynamic content. Timestamps, random IDs, reordered schemas, or changing instructions before the checkpoint can cause misses. |
| Minimum prefix size | Model-specific. Claude Sonnet 4.6 is currently listed at 1,024 tokens per checkpoint, while some Claude 4.5 models are listed at 4,096. Check the Bedrock table for the exact model. |
| TTL | Most supported models have 5-minute TTL. Some Claude 4.5 models support an extended 1-hour TTL. Hits refresh the TTL window. |
| Checkpoints | Claude models support up to 4 checkpoints. Bedrock also documents simplified cache management for Claude models with an automatic lookback from the static/dynamic boundary. |
| Pricing | Cache reads are discounted; cache writes may cost more than standard uncached input. The read/write multiplier depends on model and TTL. |

In this project, the Custom agent enables prompt caching through Strands/Bedrock configuration. The UI reports `cacheReadInputTokens` and `cacheWriteInputTokens` separately, which matters because `inputTokens` excludes cached tokens.

Cost honesty: prompt caching only pays back when the cached prefix is reused before expiry. A rough break-even such as "two reuses within TTL" or "around 30% hit ratio" is useful as practitioner guidance, not an AWS guarantee. Measure the actual hit ratio.

### Semantic Caching

Semantic caching stores an answer under a meaning-aware key rather than only an exact string. "Hash of meaning" is a useful phrase, but technically this is usually nearest-neighbor vector search with a threshold.

Common flow:

```text
user request
normalize / classify / embed
lookup similar prior request
if similarity high and metadata compatible:
    return cached answer
else:
    run agent
    store answer with metadata
```

What it can save:

```text
LLM inference
retrieval
tool execution
Gateway operations
Runtime duration
latency
```

What it adds:

```text
embedding call
vector store lookup
storage
threshold tuning
invalidation risk
```

Metadata is not optional. A safe semantic cache key usually includes:

- Tenant or user permission scope
- Prompt intent
- Language or locale
- Data-source version
- Tool/resource versions
- Freshness TTL
- Model/prompt version
- Safety and compliance flags

Semantic caching is highest leverage because a hit can skip the whole agent loop. It is also the riskiest because a false positive can return a confident wrong answer. Use an allowlist of cacheable intents, not a vague blocklist of dangerous ones.

AWS framing: there is no single "turn on semantic cache" AgentCore control. AWS has managed building blocks and official guidance, including ElastiCache for Valkey vector search with Bedrock/AgentCore examples. The cache logic remains yours: embed, search, threshold, filter, serve/store, and invalidate.

### Tool-Resource Caching

Tool-resource caching stores expensive external results so the agent does not call the same resource repeatedly.

Examples:

- Weather API response for a location and timestamp bucket
- Geocoding lookup such as `searchLocation("Marseille")`
- Web page HTML, markdown extraction, or screenshot
- Retrieved document chunks
- OpenAPI response from a business system
- Large tool output offloaded to S3
- Code-interpreter artifact metadata
- Tool catalog or schema fetches where applicable

A good tool cache key includes:

```text
tool name
normalized input
caller permission scope
resource version or ETag
TTL/freshness policy
schema version
```

Tool-resource caching is often the best way to reduce Gateway operations and runtime duration, but it is also where stale data hurts fastest.

AgentCore Gateway note: Gateway bills MCP operations such as `ListTools`, `CallTool`, and `Ping`, search queries, and tool indexing. Gateway stores/parses/indexes tool definitions, but there is no documented first-class cache for tool results. If a Lambda-backed Gateway tool should cache Open-Meteo responses, the result cache belongs inside the Lambda/tool implementation or in front of the downstream API.

Runtime cost nuance: tool caching shortens I/O wait and therefore shortens the session. CPU is charged only for active processing time, but memory is still charged over session time based on memory consumed up to each second. Shorter sessions can reduce the memory portion of AgentCore Runtime cost.

## How The Layers Compose

The strongest design usually composes all three carefully:

1. Semantic cache outside the agent for safe, redundant, final-answer workloads.
2. Prompt cache inside every model invocation for stable prefixes.
3. Tool-resource cache inside or behind tools for idempotent resource reads.

Invalidation flows outward:

```text
tool/resource data changed
  -> invalidate tool cache
  -> invalidate dependent semantic answers

prompt/tool schema changed
  -> prompt cache misses automatically by prefix mismatch
  -> semantic cache may need a prompt/model version bump
```

## Cost Honesty

- Prompt caching is not free on miss. Prefix changes or TTL expiry can turn every call into a cache write.
- Semantic caching adds per-request cost even on miss: embedding call, vector lookup, storage, and operations.
- Tool-resource caching can reduce Gateway and downstream calls, but managed caches such as API Gateway cache or ElastiCache may have standing costs.
- Caches can make demos look faster than the architecture really is. Benchmarks should be explicit about warm vs cold cache state.
- A cache that cannot be disabled per run is painful for debugging and invalidates many comparisons.

## Validation Questions

Use these in the project health-check before enabling or presenting caching:

1. **Which cache layer are we using: prompt, semantic, tool-resource, or more than one?**
   The team should be able to say what is cached and what is not cached.

2. **Can we explain cache metrics separately from normal token metrics?**
   Prompt cache reads, cache writes, fresh input, and output tokens should be reported separately.

3. **Is the prompt-cache prefix stable and large enough for the selected model?**
   Check static-before-dynamic ordering, deterministic tool schema serialization, model-specific minimum token count, checkpoint count, and TTL.

4. **What is the semantic-cache threshold and how was it chosen?**
   Use a labeled sample or evaluation set. A copied default such as `0.8` is not a validation strategy.

5. **Is the semantic cache scoped by tenant/user and data version?**
   Any answer containing non-public or user-specific data must be filtered or keyed by permission scope.

6. **Is there an explicit idempotent-tools allowlist?**
   Side-effecting tools such as create order, send message, update ticket, or submit payment should be structurally unable to hit a result cache.

7. **What is the invalidation strategy?**
   Name the TTL, event trigger, version bump, manual purge path, or reason the data is safe to keep.

8. **Can every cache be disabled per run?**
   Benchmarks, debugging, and architecture comparisons need a clean way to run cold or bypass specific cache layers.

9. **Do we track hit rate, miss rate, stale-hit incidents, and cost savings?**
   A cache without observability becomes a correctness risk rather than a FinOps tool.

10. **What happens on a cache miss or cache corruption?**
    The fallback should be a normal fresh agent run, not a user-visible failure.

## Project Notes

- Prompt caching is already visible in the app through cache read/write token metrics.
- Semantic caching is not currently implemented in this repository and should stay out of the benchmark path.
- Tool-resource caching is not currently implemented; the app estimates Gateway cost from observed tool calls plus Gateway operation assumptions.
- `searchLocation` is the best first tool-resource cache candidate because geocoding is stable and repeated.
- `getForecast` can be cached only with a deliberately short TTL and a timestamp bucket in the cache key.
- Any future semantic cache should be scoped by tenant/user permissions, data version, and prompt/model version before it is used in a demo.

## Sources

- [Amazon Bedrock prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
- [Amazon Bedrock prompt caching model support](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html#prompt-caching-model-support)
- [Effectively use prompt caching on Amazon Bedrock](https://aws.amazon.com/blogs/machine-learning/effectively-use-prompt-caching-on-amazon-bedrock/)
- [Amazon Bedrock AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Announcing vector search for Amazon ElastiCache](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-elasticache-vector-search/)
- [Lower cost and latency for AI using ElastiCache as a semantic cache with Amazon Bedrock](https://aws.amazon.com/blogs/database/lower-cost-and-latency-for-ai-using-amazon-elasticache-as-a-semantic-cache-with-amazon-bedrock/)
- [AgentCore Gateway vs. Self-Hosted MCP](gateway-vs-self-hosted-mcp.md)
