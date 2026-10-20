
# Practical LLM Model Router Methods

> [!summary]
> A model router selects the model that should handle each request. Practical
> methods include manual or policy selection, deterministic rules, task tables,
> semantic similarity, supervised NLP classifiers, LLM-based classifiers,
> managed provider routing, and cheap-first cascades. The right choice depends
> on available data, routing latency, model cost, and whether answer quality can
> be verified. This guide also maps the broader open-source router ecosystem and
> gives measured latency references for learned routing methods.

## 1. Purpose of a model router

Applications often have access to several models with different cost,
capability, context, and latency characteristics:

```text
small model  -> fast and inexpensive
medium model -> balanced
large model  -> more capable and expensive
```

A router chooses a model for the current request:

```text
Request + context
       │
       ▼
     Router
       │
       ├── small model
       ├── medium model
       └── large model
```

The most useful objective is generally not “always choose the cheapest model” or
“always choose the strongest model.” It is:

> Choose the least expensive model expected to meet the required quality level.

For a catalog ordered by expected cost, the router can select the first model
whose predicted success probability exceeds the quality threshold:

```text
select first model m where:

P(acceptable response | request, m) >= required threshold
```

## 2. What information can a router use?

Depending on the method, a router can use:

- current user prompt;
- relevant conversation history;
- prompt and context length;
- task or workflow name;
- language;
- attachment type and size;
- requested output format;
- available tools;
- latency or cost target;
- previous labeled requests;
- model responses and evaluation results.

A router decision should be represented explicitly:

```json
{
  "selected_tier": "small",
  "selected_model_id": "provider.model-small-v1",
  "router_type": "supervised-classifier",
  "router_version": "v3",
  "score": 0.94,
  "threshold": 0.90,
  "reason": "small model predicted to satisfy required quality"
}
```

## 3. Method 1 — Manual or policy-based selection

The caller explicitly selects a tier:

```python
if requested_mode == "economy":
    return SMALL_MODEL

if requested_mode == "quality":
    return LARGE_MODEL
```

The selection can come from:

- a user-interface option;
- an API parameter;
- a tenant or subscription plan;
- a latency service level;
- a batch versus interactive execution mode;
- an administrator override.

### Advantages

- negligible latency;
- no training data;
- no additional inference cost;
- predictable and easy to explain.

### Limitations

- the caller must understand the models;
- users may always choose the strongest model;
- it does not automatically assess request difficulty.

### Best use

Manual selection is useful as an override, an experiment control, or a product
tier. It is usually combined with another automatic router.

## 4. Method 2 — Deterministic rules

A rule router checks request properties in a fixed order:

```python
if long_context:
    return LARGE_MODEL

if has_complex_attachment:
    return LARGE_MODEL

if asks_for_simple_extraction:
    return SMALL_MODEL

if COMPLEX_TASK_REGEX.search(prompt):
    return LARGE_MODEL

return SMALL_MODEL
```

Possible rules include:

- prompt length thresholds;
- file presence or file type;
- known languages or modalities;
- keywords and regular expressions;
- requested output format;
- use of reasoning features;
- required tools;
- application-defined request categories.

### Advantages

- extremely low latency;
- no model invocation or training dataset;
- deterministic and easy to test;
- clear routing reasons;
- simple operational deployment.

### Limitations

- rules only approximate model capability;
- regex does not understand semantic meaning;
- rules become harder to maintain as traffic diversifies;
- a difficult request may contain no known signal;
- a simple request may match a misleading keyword.

### Best use

Rules are appropriate for prototypes, small bounded domains, explicit product
constraints, and a fallback when learned routing is unavailable.

## 5. Method 3 — Task or workflow routing table

When the application already knows the operation being executed, it can assign
a model directly:

```python
TASK_MODEL = {
    "intent_detection": SMALL_MODEL,
    "field_extraction": SMALL_MODEL,
    "query_rewrite": SMALL_MODEL,
    "document_synthesis": MEDIUM_MODEL,
    "complex_code_review": LARGE_MODEL,
}
```

This differs from a keyword router. The task is supplied by the workflow rather
than inferred from user text.

Example workflow:

```text
classify intent      -> small model
extract search terms -> small model
analyze documents    -> large model
format response      -> small model
```

### Advantages

- negligible routing overhead;
- highly predictable;
- easier to evaluate per workflow step;
- no need to infer information the application already knows.

### Limitations

- requires a stable task taxonomy;
- less useful for open-ended chat;
- each task still needs a measured model choice;
- new workflow nodes require configuration.

### Best use

This is often the best router for structured pipelines and agents whose steps
are defined by application code or a workflow graph.

## 6. Method 4 — Semantic similarity routing

Semantic routing converts a request into an embedding and compares it with
known examples or task prototypes:

```text
New request
    │
    ▼
Embedding model
    │
    ▼
Nearest labeled examples
    │
    ▼
Model selected from similar historical cases
```

Example:

```python
vector = embed(request.text)
neighbors = index.search(vector, k=20)

small_success_rate = weighted_success_rate(neighbors)

if small_success_rate >= threshold:
    return SMALL_MODEL

return LARGE_MODEL
```

The router can also compare the request against descriptions such as:

```text
simple lookup
structured extraction
code debugging
document analysis
```

### Advantages

- understands paraphrases better than regex;
- can reuse an existing embedding and vector-search stack;
- decisions can be explained with similar examples;
- adding labeled examples can improve coverage without rewriting rules.

### Limitations

- requires an embedding model and index;
- remote embedding calls add latency and cost;
- nearest examples may still be too dissimilar;
- similarity of topics does not always imply similar difficulty;
- the index must be updated as traffic changes.

### Best use

Semantic routing is practical when an application already has embeddings and a
moderate set of representative examples, but not enough labels to train a
reliable supervised classifier.

The router should detect out-of-distribution requests. If even the closest
examples are distant, it should avoid treating the neighbor vote as confident.

## 7. Method 5 — Supervised NLP classifier

A supervised classifier learns from historical request outcomes.

For two models, the preferred target is:

```text
P(small model produces an acceptable response | request and context)
```

### 7.1 Create labels

Run representative requests against the candidate models:

```text
Request
   ├── small-model response
   └── large-model response
          │
          ▼
Evaluation
          │
          ▼
small_model_acceptable = true or false
```

The evaluation can use expected answers, schema validation, tool outcomes,
model-based evaluation, or human review depending on the task.

The label must answer whether the small model is good enough—not merely whether
the large model's response is slightly preferred.

### 7.2 Extract features

A practical first implementation can combine:

- TF-IDF word and character features;
- prompt and conversation token counts;
- known task identifier;
- language;
- attachment metadata;
- code-like text signals;
- requested output format;
- available tool categories.

### 7.3 Train and route

A useful starting model is calibrated logistic regression:

```text
TF-IDF + structured features
              │
              ▼
Calibrated logistic regression
              │
              ▼
P(small model acceptable)
```

Runtime decision:

```python
features = extract_features(request)
probability = classifier.predict_proba(features)[0, 1]

if probability >= quality_threshold:
    return SMALL_MODEL

return LARGE_MODEL
```

### Advantages

- millisecond-level local inference is achievable;
- no additional LLM call;
- learns from the application's actual domain;
- probability thresholds expose the cost/quality trade-off;
- the classifier artifact can be versioned and rolled back.

### Limitations

- requires representative and accurate labels;
- must be recalibrated after important model or prompt changes;
- unfamiliar traffic needs conservative handling;
- dataset leakage can produce misleading evaluation results.

### Best use

This is the recommended production approach when sufficient application-specific
data exists and routing latency must remain low.

Start with a linear classifier. Use a small sentence encoder only if evaluation
shows a meaningful gain on paraphrases or semantic generalization.

## 8. Method 6 — LLM-based classifier

A small or fast LLM receives a routing prompt before the answer model runs:

```text
User request
    │
    ▼
Classifier LLM
    │
    ├── small -> small answer model
    └── large -> large answer model
```

Example routing prompt:

```text
Choose the least expensive model that can reliably complete the request.

SMALL:
- factual questions
- summaries and transformations
- field extraction
- straightforward tool lookups

LARGE:
- difficult reasoning
- debugging and implementation
- architecture
- long or multi-step analysis

Return JSON only:
{
  "tier": "small" or "large",
  "confidence": 0.0 to 1.0,
  "reason": "brief explanation"
}
```

Example result:

```json
{
  "tier": "large",
  "confidence": 0.93,
  "reason": "requires multi-step diagnosis and code modification"
}
```

### Advantages

- strong semantic understanding;
- works before a large labeled dataset exists;
- fast to prototype and modify;
- useful for shadow-mode experiments and initial labeling.

### Limitations

- adds a serial model call before generation;
- adds token cost to every classified request;
- introduces another throttling and failure path;
- reported confidence is not automatically calibrated;
- output parsing and variability must be handled.

### Best use

Use an LLM classifier to bootstrap or benchmark a routing policy. Once sufficient
labels exist, compare it with a local classifier and retain it only if its quality
advantage justifies the added latency and cost.

## 9. Method 7 — Managed provider routing

Some model platforms provide a managed router endpoint. The application invokes
the router instead of selecting the final model itself:

```text
Request
    │
    ▼
Managed router endpoint
    │
    ├── model A
    └── model B
```

Amazon Bedrock Intelligent Prompt Routing is one example. It predicts response
quality and routes between two supported models in the same family.

[AWS Intelligent Prompt Routing documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html)

### Advantages

- no custom classifier deployment;
- routing and model invocation use one managed endpoint;
- infrastructure and model selection are handled by the provider;
- useful for teams without a domain-specific routing dataset.

### Limitations

- supported model pairs and regions are restricted;
- the application has less control over the classifier;
- provider routing may not learn from application-specific outcomes;
- language or model-family limitations can apply;
- portability across providers is lower.

### Best use

Use managed routing when the desired model pair is supported and benchmarks show
that its quality/cost curve meets the application's requirements.

## 10. Method 8 — Cheap-first cascade

A cascade is not strictly a pre-generation router, but it is a practical method
for selecting model effort dynamically.

```text
Request
    │
    ▼
Small model generates
    │
    ▼
Acceptance gate
    ├── pass -> return small-model response
    └── fail -> invoke large model
```

Conceptual implementation:

```python
candidate = invoke_small_model(request)

if acceptance_gate(candidate, request):
    return candidate

return invoke_large_model(request)
```

Practical acceptance gates include:

- JSON schema validation;
- required fields present;
- deterministic calculation check;
- expected answer or assertion;
- successful tool execution;
- citations belonging to the retrieved source set.

### Advantages

- easy requests use only the small model;
- the decision uses the actual response, not only prompt prediction;
- deterministic validation can give strong evidence of acceptability.

### Limitations

- escalated requests pay both model costs;
- escalated requests incur both latencies serially;
- open-ended quality is difficult to validate cheaply;
- retrying a complete agent turn may repeat tool calls or side effects.

### Best use

Use cascades for structured or objectively verifiable outputs. Use a
pre-generation router for open-ended tasks where no reliable, inexpensive gate
exists.

## 11. Open-source LLM router taxonomy

Open-source LLM routers are easiest to classify by the signal they use to make
the routing decision. This is different from [LLM0](https://www.llm0.ai/), which
describes itself as a spend firewall rather than a model-selection router.

| Class | How routing works | Representative OSS projects or algorithms | Best use |
|---|---|---|---|
| **Static or policy routers** | Apply explicit rules for model name, tenant, budget, region, or availability | LiteLLM, Portkey Gateway, Bifrost, Helicone | Production reliability and governance |
| **Semantic or intent routers** | Use embeddings or classification to determine a domain or intent | Aurelio Semantic Router, vLLM Semantic Router | Domain-to-specialist routing, such as coding to a coder model |
| **Difficulty or quality routers** | Predict whether a cheaper model is capable enough | RouteLLM, HybridLLM, causal-LLM routers | Cost reduction with a controlled quality loss |
| **Preference or performance predictors** | Learn expected model performance from historical evaluations or preferences | Matrix factorization, Elo, RouterDC, KNN, SVM, MLP, GraphRouter | Selecting among heterogeneous models |
| **Cascade or verifier routers** | Run a cheap model first and escalate when confidence or validation is insufficient | AutoMix, FrugalGPT | Aggressive cost optimization for verifiable outputs |
| **LLM-as-router** | Ask an LLM to choose the model that should answer | RouteLLM causal-LLM, LLMRouter `LLMMultiRoundRouter`, NVIDIA auto-router | Complex decisions that exceed a small classifier |
| **Multi-turn or agentic routers** | Use conversation or trajectory state rather than only the current prompt | Router-R1, `KNNMultiRoundRouter`, `LLMMultiRoundRouter` | Coding agents, research agents, and long workflows |
| **Personalized routers** | Condition model selection on user identity, preferences, or history | GMTRouter, PersonalizedRouter | User-specific cost, quality, and style preferences |
| **Multimodal routers** | Select by text, image, audio, time-series modality, or model capability | TSRouter, NVIDIA LLM Router, vLLM Semantic Router | Mixed LLM and VLM workloads |
| **Infrastructure or load routers** | Select an endpoint, GPU, region, or provider rather than the intellectually best model | LiteLLM, Bifrost, Portkey | Availability, throughput, latency, and rate limits |

The first and last classes are often called LLM routers, but they are primarily
gateways, policy engines, or load balancers. They complement learned model
selection rather than replacing it.

### Major open-source projects

| Project | Primary role | Model scope | Practical position |
|---|---|---|---|
| [LLMRouter — UIUC](https://github.com/ulab-uiuc/LLMRouter) | Research and deployment framework | Multiple models and routing families | Unified comparison of many routing algorithms |
| [vLLM Semantic Router](https://github.com/vllm-project/semantic-router) | Programmable Mixture-of-Models router | Multi-model and multimodal | Advanced production semantic and policy routing |
| [RouteLLM — LMSYS](https://github.com/lm-sys/RouteLLM) | Learned cost-quality router | Primarily strong-versus-weak | Focused learned routing with preference data |
| [Semantic Router — Aurelio](https://github.com/aurelio-labs/semantic-router) | Embedding and intent router | Arbitrary routes | Fast semantic intent decisions |
| [NVIDIA LLM Router](https://github.com/NVIDIA-AI-Blueprints/llm-router) | Intent and learned auto-router | Multi-model and multimodal | Deprecated; succeeded by [NeMo Switchyard](https://github.com/NVIDIA-NeMo/Switchyard) |
| [AutoMix](https://github.com/automix-llm/automix) | Confidence and verifier cascade | Small to large | Response-based escalation |
| [FrugalGPT](https://github.com/stanford-futuredata/FrugalGPT) | Budget-aware cascade | Multiple models | Cost-constrained model-service strategies |
| [LiteLLM](https://github.com/BerriAI/litellm) | AI gateway and load router | Many providers | Provider abstraction, fallback, and governance |
| [Portkey Gateway](https://github.com/Portkey-AI/gateway) | Policy and gateway router | Many providers | Conditional routing and production controls |
| [Bifrost](https://github.com/maximhq/bifrost) | High-performance gateway | Multiple providers | Unified API, failover, and load balancing |
| [Helicone](https://github.com/Helicone/helicone) | Gateway and observability | Multiple providers | Routing, fallbacks, and request telemetry |
| [TensorZero](https://github.com/tensorzero/tensorzero) | Gateway and evaluation platform | Multiple models | Inference, evaluation, and optimization |

The [UIUC LLMRouter paper](https://arxiv.org/abs/2608.06867) formulates routing
using context encoders, model encoders, scoring functions, decision rules, and
learning signals. Its library includes more than 16 implementations across
single-turn, multi-turn, personalized, multimodal, and agentic settings. The
single-turn implementations include KNN, SVM, MLP, matrix factorization, Elo,
RouterDC, AutoMix, HybridLLM, GraphRouter, and CausalLM. This makes LLMRouter a
useful infrastructure for researching and benchmarking router algorithms.

RouteLLM is narrower. It learns a strong-versus-weak decision from preference
data and provides matrix-factorization, similarity-weighted ranking, BERT, and
causal-LLM routers. A threshold controls the quality-cost trade-off:

```text
P(strong model wins | prompt) >= threshold -> strong model
otherwise                                  -> weak model
```

Aurelio Semantic Router answers a different question: *what kind of request is
this?* It embeds a request and compares it with route examples. RouteLLM instead
asks: *how capable a model does this request need?* The vLLM Semantic Router
extends the semantic approach into a broader Mixture-of-Models system that can
consider request signals, user preferences, policies, cost, latency, privacy,
safety, location, and model capabilities.

AutoMix and FrugalGPT make response-based decisions. AutoMix generates with a
cheaper model and verifies the candidate before escalating; FrugalGPT builds
budget-aware cascades over multiple model services. In contrast, RouteLLM is
primarily a pre-generation router.

Gateway projects such as LiteLLM, Portkey, Bifrost, and Helicone provide unified
provider APIs, load balancing, retries, fallbacks, spend controls, guardrails,
and observability. They are best understood as **traffic routers**, while
LLMRouter, RouteLLM, and semantic classifiers are **model-intelligence
routers**. A production system can use both layers.

Another useful classification is the point at which the decision is made:

```text
request -> model       RouteLLM / Semantic Router
request -> model path  vLLM Semantic Router
response -> escalation AutoMix / FrugalGPT
turn -> model          Router-R1
agent step -> model    agentic routers
user -> policy         personalized routers
request -> endpoint    LiteLLM / Bifrost / Portkey
```

For stack selection, use LLMRouter to research and benchmark algorithms,
RouteLLM for straightforward cheap-versus-expensive learned routing, Aurelio
Semantic Router for intent routing, vLLM Semantic Router for advanced
Mixture-of-Models policies, and a gateway such as LiteLLM, Portkey, or Bifrost
underneath when provider routing and failover are also required.

## 12. Learned-router latency and production budget

For LLMRouter-style learned routing, extra latency can range from single-digit
milliseconds to hundreds of milliseconds depending on the algorithm. The UIUC
LLMRouter paper presents a unified framework containing many router types, not
one universal router-latency figure.

A measured reference is RouteLLM's routing-overhead benchmark. Converting its
reported processing rates to approximate per-request processing time gives:

| Router | Reported throughput | Approximate routing time[^throughput-latency] |
|---|---:|---:|
| **Matrix factorization** | 155.16 requests/s | **~6.4 ms** |
| **BERT classifier** | 69.62 requests/s | **~14.4 ms** |
| **Causal-LLM classifier** | 42.46 requests/s | **~23.6 ms** |
| **Similarity-weighted ranking** | 2.9 requests/s | **~345 ms** |

[^throughput-latency]: Calculated as `1000 / requests_per_second`. This is a
    rough latency proxy, not a directly measured p50 request latency. RouteLLM
    used a Google Cloud `g2-standard-4` VM with one NVIDIA L4 for matrix
    factorization, BERT, and causal-LLM routers. Similarity-weighted ranking ran
    on a CPU-only `n2-standard-8` VM and included embedding-generation cost.

The practical fast-path budget is therefore:

```text
Request -> lightweight learned router (~5-25 ms) -> selected LLM
```

If the selected LLM has a 500-1500 ms time to first token (TTFT), a 10-20 ms
router adds roughly 1-4% to TTFT. That overhead is usually small compared with
the model call.

More elaborate routers change the budget. A remote embedding-based semantic
router adds network latency, while an LLM used as the router adds another serial
generation. The vLLM Semantic Router optimization paper reports a complex
classifier path reduced from 4,918 ms to 50 ms; it reports 108 ms for a
16K-token routing input.

Use these ranges as initial architecture budgets, not universal benchmarks:

```text
Rules / heuristics                  <1 ms
Small local classifier / MF         ~5-25 ms
Optimized semantic classifier       ~20-100 ms
Remote embedding routing            ~50-300+ ms
Heavy similarity search             ~100-400+ ms
Small LLM-as-router                 ~100-1000+ ms
Frontier LLM-as-router              ~500 ms-several seconds
```

For production difficulty-based model selection, approximately 10 ms is an
achievable target with a small local classifier. Avoid an LLM-as-router when
the primary objective is lower latency. Measure router p50, p95, and p99 in the
deployment environment because throughput-derived estimates do not capture
queueing, batching, cold starts, or network calls.

## 13. Practical comparison

| Method | Added latency | Extra model call | Data required | Best fit |
|---|---:|---:|---:|---|
| Manual/policy | Negligible | No | None | Overrides and product tiers |
| Rules | Negligible | No | None | Baseline and bounded domains |
| Task table | Negligible | No | Task taxonomy | Structured workflows |
| Semantic similarity | Low | Embedding may be remote | Examples | Reusing similar historical cases |
| Supervised classifier | Very low locally | No | Labeled outcomes | Production domain routing |
| LLM classifier | Serial LLM latency | Yes | None initially | Bootstrap and prototype |
| Managed provider router | Provider-managed | No separate classifier call | None locally | Supported managed model pairs |
| Cascade | One or two generations | On escalation | Acceptance gate | Verifiable outputs |

## 14. How to choose a practical method

```text
Does the application already know the task?
    yes -> task routing table
    no  -> continue

Is a labeled application dataset available?
    yes -> supervised classifier
    no  -> continue

Are representative examples and embeddings available?
    yes -> semantic similarity router
    no  -> continue

Is a supported managed router available?
    yes -> benchmark managed routing
    no  -> rules or an LLM classifier for bootstrap

Can the generated result be validated cheaply?
    yes -> consider a cascade
```

These methods can be combined. A common progression is:

```text
rules baseline
    -> collect outcomes
    -> train local classifier
    -> keep manual override
    -> add cascade only for verifiable tasks
```

## 15. Evaluation criteria

Every routing method should be compared with at least:

```text
always smallest model
always largest model
random routing with the same small-model share
current production router
```

Important metrics include:

- small-route precision: acceptable small-model routes / all small-model routes;
- small-model coverage: small-model routes / all requests;
- quality difference from always using the strongest model;
- cost per accepted response;
- router latency and end-to-end latency;
- probability calibration for learned classifiers;
- cascade escalation rate;
- results by language, task and context length.

Accuracy alone is insufficient. A router that always selects the largest model
may have excellent quality but no savings. A router that always selects the
smallest model may save tokens but fail the required quality level.

## 16. General implementation pattern

Keep model selection separate from model invocation:

```python
decision = router.route(request, context)
response = model_clients[decision.model_id].invoke(request)
```

Recommended decision interface:

```python
@dataclass(frozen=True)
class RouteDecision:
    model_id: str
    tier: str
    reason: str
    router_type: str
    router_version: str
    score: float | None = None
    threshold: float | None = None
```

This keeps the router independent from Strands, AgentCore, LangGraph, or another
agent framework. Each integration adapter only translates `model_id` into the
provider's invocation contract.

## 17. Example: AgentCore and Strands

In this repository, one shared route is sent to two agent implementations.

### Managed AgentCore Harness

The existing Harness receives a per-prompt model override:

```python
client.invoke_harness(
    harnessArn=harness_arn,
    runtimeSessionId=session_id,
    messages=messages,
    model={
        "bedrockModelConfig": {
            "modelId": decision.model_id,
        }
    },
)
```

### Strands on AgentCore Runtime

The application sends a logical tier:

```json
{
  "prompt": "...",
  "model_tier": "small"
}
```

The Runtime maps it to an allowed model and constructs the provider:

```python
model_id = MODEL_CATALOG[model_tier]
agent = Agent(model=BedrockModel(model_id=model_id), tools=tools)
```

The model may change between prompts in the same session. A single prompt's
agent/tool loop normally keeps its selected model fixed.

## 18. Recommended adoption sequence

1. Start with deterministic rules or a task table.
2. Record request, route, model, tokens, latency and evaluated outcome.
3. Create paired examples from small and strong models.
4. Train and calibrate a local supervised classifier.
5. Run it in shadow mode before applying decisions.
6. Activate it progressively and compare with the strong-model baseline.
7. Add semantic routing or cascades only where they solve a measured gap.
8. Benchmark a managed router when a suitable provider/model pair is available.

## 19. Final recommendation

For a general application:

```text
Known structured tasks -> task routing table
General traffic with labels -> local supervised classifier
General traffic without labels -> rules or temporary LLM classifier
Existing embeddings/examples -> semantic similarity router
Supported managed pair -> benchmark provider router
Verifiable response -> consider a cascade
```

These approaches cover the practical model-routing needs of most applications
without introducing research-heavy online learning or complex optimization
systems.

## References

- [AWS — Intelligent Prompt Routing](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html)
- [AWS — AgentCore Harness model overrides](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-models.html)
- [Strands — Amazon Bedrock model provider](https://strandsagents.com/docs/user-guide/concepts/model-providers/amazon-bedrock/)
- [LLMRouter — Unified Infrastructure for Developing, Evaluating, and Deploying LLM Routers](https://arxiv.org/abs/2608.06867)
- [RouteLLM — Learning to Route LLMs with Preference Data](https://arxiv.org/html/2406.18665v4)
- [vLLM Semantic Router — routing latency optimization](https://arxiv.org/abs/2603.12646)

## Related repository documents

- [Value maxxing and dynamic Foundation Model routing](Value_maxxing_et_routage_dynamique_des_foundation_models.md)
- [Dynamic Foundation Model assignment](Assignation_dynamique_Foundation_Model_agent_existant.md)
- [AgentCore router implementation](AgentCore_cost_aware_model_router_implementation.md)
