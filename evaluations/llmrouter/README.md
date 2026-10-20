# Travel and protein model-routing corpus

This directory contains a reproducible cold-start dataset for routing agent
turns between Claude Haiku (`cheap`) and Claude Sonnet (`strong`) with
[ulab-uiuc/LLMRouter](https://github.com/ulab-uiuc/LLMRouter). It targets the
repository's Travel Agent and Protein Analysis workloads and includes a smaller
general boundary set so unrelated deep-analysis prompts do not automatically
fall into the economical tier.

## What is included

- 7,212 unique queries and 14,424 paired candidate records.
- 3,205 travel prompts, 3,204 protein prompts, and 803 general boundary prompts.
- 3,604 Haiku winners and 3,608 Sonnet winners.
- 36 task families with cheap/strong minimal pairs.
- Separate train (5,772), validation (720), and test (720) concrete prompts;
  intent templates recur across splits because KNN must have local examples.
- Explicit risk, rationale, provenance, expected model, and stable embedding ID
  on every example.
- Critical production seeds, including `Explain in depth the concept of value
  maxxing.`, labelled `strong`.

`generate_dataset.py` owns the scenario catalog. Regeneration validates exact
split isolation, class balance, candidate pairing, unique IDs, and the winner
encoded by every pair:

```bash
uv run python -m evaluations.llmrouter.generate_dataset
```

The generated manifest and hashes are in `data/manifest.json`.

## Honest data boundary

The checked-in corpus is a **policy bootstrap**, not an empirical claim that
Haiku or Sonnet produced a particular quality score. Its `performance` values
are synthetic decision utilities:

- For a cheap-labelled task, Haiku receives `1.0` and Sonnet `0.985`. This says
  both are expected to satisfy the task, so the cheaper acceptable model wins.
- For a strong-labelled task, Sonnet receives `1.0`; Haiku receives a lower
  utility based on risk.
- `measured=false`, `quality_score=null`, and
  `score_type=policy_bootstrap_utility` prevent these values from being
  mistaken for benchmark observations.

This is more expressive than the former keyword function, but it still learns
human policy. Production promotion requires repeated executions of both models
and quality evaluation. `promote_measurements.py` converts those evaluations
into paired LLMRouter records. High and critical-risk cases retain a Sonnet
policy floor; lower-risk cases choose Haiku only when its 95% lower confidence
bound clears the configured acceptance threshold.

Each measurement JSONL row must contain:

```json
{"task_id":"travel-weather-cheap-train-000","model_name":"cheap","quality_score":0.94,"cost_usd":0.0021,"input_tokens":500,"output_tokens":120,"response_time":1.4,"response":"..."}
```

Collect at least three independent rows for each task/model pair, then promote
one split at a time:

```bash
uv run python -m evaluations.llmrouter.promote_measurements \
  --gold evaluations/llmrouter/data/query_data_train.jsonl \
  --measurements path/to/repeated_train_measurements.jsonl \
  --output evaluations/llmrouter/data/routing_data_train.measured.jsonl
```

Review and version measured data before replacing the bootstrap routing file.
Do not mix a partially measured file with synthetic rows without recording that
provenance explicitly.

## Build and train LLMRouter

LLMRouter KNN uses Longformer embeddings at both training and inference. Build
them with LLMRouter's own embedding function so the vector spaces match:

```bash
uv sync --dev --extra llmrouter
uv run --extra llmrouter python -m evaluations.llmrouter.build_artifacts
```

The builder defaults to one query per batch and two PyTorch CPU threads so it
remains responsive on an 8 GiB WSL host. It saves a resumable checkpoint every
100 queries. If WSL or the terminal disconnects, run the same command again;
the builder reports `Resuming from checkpoint` and continues. Transformers may
report unused Longformer language-model-head weights during startup. That is an
expected warning because routing uses the base model's embeddings.

This downloads the upstream embedding model on first use and writes ignored,
machine-local files under `evaluations/llmrouter/artifacts/`:

- `query_embeddings_longformer.pt`
- `knnrouter.yaml` with absolute paths
- `knnrouter.pkl` after training

Absolute paths are intentional. LLMRouter 0.3.1 resolves relative dataset
paths against its installed package directory rather than the YAML directory.

Train KNN and verify route-only inference:

```bash
uv run --extra llmrouter llmrouter train \
  --router knnrouter \
  --config evaluations/llmrouter/artifacts/knnrouter.yaml

uv run --extra llmrouter llmrouter infer \
  --router knnrouter \
  --config evaluations/llmrouter/artifacts/knnrouter.yaml \
  --query "Explain in depth the concept of value maxxing." \
  --route-only
```

Then configure the comparison application:

```dotenv
MODEL_ROUTING_MODE=auto
LLM_ROUTER_NAME=knnrouter
LLM_ROUTER_CONFIG_PATH=/absolute/path/to/evaluations/llmrouter/artifacts/knnrouter.yaml
LLM_ROUTER_MODEL_MAP={"cheap":"cheap","strong":"strong"}
```

Restart or redeploy the **comparison application** so it loads the new router
configuration. Managed Harness resources do not need redeployment: each
invocation receives the selected Haiku/Sonnet model override.

## Release gate

Do not enable automatic Haiku routing merely because KNN fits the training
corpus. The included validation/test splits detect exact-record leakage and
regressions, but they reuse the same intent families and are not evidence of
generalization to unknown workloads. Add real production-like prompts and
evaluate the untouched test split, prioritizing:

1. strong-task recall, especially high/critical-risk recall;
2. Haiku precision (how often a Haiku route really remains acceptable);
3. task success and policy compliance for both complete agent/tool loops;
4. quality delta versus always-Sonnet;
5. cost and latency savings only after the quality constraints pass.

Run the router in shadow mode first: log its decision while continuing to invoke
Sonnet. Promote it only after the measured test set satisfies the agreed quality
budget.
