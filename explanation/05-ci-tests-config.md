# 05 — CI/CD, Tests, and Project Config

How the repo is validated on every change and shipped to AWS on demand. New to
CI/CD, OIDC, uv, ruff, pytest? See
[00-concepts-for-beginners.md](00-concepts-for-beginners.md).

---

## `.github/workflows/` — four workflows, one strict rule

The rule: **CI validates; deploys are manual.** And that rule is itself enforced by
a unit test, so a workflow edit that breaks the model fails the test suite.

### `ci.yml` — runs on every PR and on pushes to `main`/`migration`

[.github/workflows/ci.yml](../.github/workflows/ci.yml) has two jobs:

**Job 1 — `validate`** (no AWS credentials at all):
`uv sync --frozen --dev` → `ruff format --check` → `ruff check` → `pytest` →
`compileall` over `apps agents evaluations infrastructure tests` → **Streamlit smoke
test** ([check_streamlit.py](../infrastructure/scripts/check_streamlit.py)) →
`pip-audit` (dependency CVEs) → `detect-secrets` over every tracked file.

**Job 2 — `agentcore-evaluation`** — runs **only on push to `main`**, after
`validate`. It assumes an AWS role via **OIDC** and runs the release-gating
evaluation against the Custom Runtime currently deployed in `dev`
(`uv run python evaluations/run_agentcore_evaluation.py`), then uploads the JSON
report as a build artifact. This is a *deployed-service smoke check* — see
[07-evaluations.md](07-evaluations.md).

### `deploy-gateway.yml`, `deploy-managed-agent.yml`, `deploy-custom-agent.yml`

All three share the same shape:

- `workflow_dispatch` only (**manual button**), `environment: dev` (so GitHub
  environment protection rules apply), `id-token: write` (**OIDC role assumption —
  no long-lived AWS keys stored in GitHub**), and a per-target **concurrency group**
  with `cancel-in-progress: false` so a second deploy *waits* instead of interrupting
  one in flight.
- Re-validate the commit (`ruff check` + `pytest`), assume the per-target deploy
  role from a repo variable, then `pulumi preview --diff` followed by `pulumi up` on
  the matching stack under `infrastructure/pulumi/`.

**`deploy-custom-agent.yml` does more** because the Custom agent is code:

1. **Version the system prompt.** Runs
   [services/prompt_version.py](../apps/comparison/services/prompt_version.py) to
   push `agents/custom/prompts/system.md` into Bedrock Prompt Management as an
   immutable version (with the Open-Meteo tool config and cache points), capturing
   the `prompt_id`, `prompt_version`, and `prompt_sha256`.
2. **Build the image.** ECR login → QEMU/Buildx → check whether `:$GITHUB_SHA`
   already exists in ECR (skip build if so — **immutable, SHA-addressed images**) →
   build `agents/custom/` for **linux/arm64** → push.
3. **Deploy.** `pulumi preview` then `pulumi up`, passing `IMAGE_URI` and the three
   `BEDROCK_PROMPT_*` values as env so the Runtime is pinned to *exactly* this
   image and this prompt version.
4. **Evaluate the deployed candidate.** Runs the evaluation with
   `AGENTCORE_EVALUATION_EXPECTED_IMAGE_TAG=${{ github.sha }}` — the runner refuses
   to pass unless the live Runtime is actually serving this commit's container. This
   is the real release gate; the `main` CI job is a lighter recurring check.

---

## `tests/unit/` — fast, offline, no AWS

Every test inserts `apps/comparison` (or the repo root) onto `sys.path` before
importing, because the app is run *from* that directory (its imports are top-level).

| Test | What it locks down |
|---|---|
| [test_workflows.py](../tests/unit/test_workflows.py) | **CI/CD guardrails as code.** Parses the workflow YAML as text and asserts: deploy workflows never mention Streamlit/`apps/comparison`; every deploy is `workflow_dispatch` + `environment: dev` + `id-token: write`; CI smoke-tests Streamlit; the evaluation runs on `main` *and* after custom deploy, *after* the deploy step, bound to the Git SHA. |
| [test_backend_errors.py](../tests/unit/test_backend_errors.py) | The credential preflight wraps an expired SSO token into `AWSAuthenticationError`, and `public_error_message` maps it to the actionable "run `aws sso login`" text. |
| [test_cost.py](../tests/unit/test_cost.py) | The measured-usage cost math: per-MTok list rates, cache savings netting read discount against write premium, `gateway_operations` excluding browser actions, per-1k Gateway pricing. |
| [test_security.py](../tests/unit/test_security.py) | `prompt_security_message` blocks system-prompt-exfiltration and injection prompts but allows a normal weather question. |
| [test_history_state.py](../tests/unit/test_history_state.py) | A completed run stays the "current" result (doesn't jump to a history entry); "New chat" clears it. Uses a fake Streamlit `session_state`. |
| [test_comparison_order.py](../tests/unit/test_comparison_order.py) | Managed always renders before Custom; unknown agents keep input order after known ones; the Altair chart serializes with that order. |
| [test_mlflow_tool_spans.py](../tests/unit/test_mlflow_tool_spans.py) | Loads `agents/custom/agent.py` directly and checks the MLflow span-annotation helpers (lenient JSON parsing + tool-span attributes) behave, using a recording fake span. |
| [test_agentcore_evaluation.py](../tests/unit/test_agentcore_evaluation.py) | The evaluation runner's pure parts: ARN→id/region parsing, SSE body parsing, the release-gate report logic, runtime-readiness container check. |

The synergy: the pure `services/`, `models/`, and evaluation-parsing functions were
written *without* Streamlit or AWS precisely so these tests can exercise real logic
with no network and no browser.

## `tests/manual/check_thinking_events.py`

A real-AWS diagnostic (not collected by CI). It invokes **both** backends with a
math prompt and thinking enabled, and reports whether `reasoning` events actually
arrive from each architecture, with per-type event counts. It's the tool you reach
for when debugging the thinking plumbing in
[backends/harness.py](../apps/comparison/backends/harness.py) (the botocore
injection) or the Custom agent's `additional_request_fields`. Requires valid SSO
credentials — that's why it's kept out of the offline suite.

---

## Project configuration

- **`pyproject.toml` + `uv.lock`** — the **repo-level** environment: Streamlit app,
  boto3, the Pulumi programs, and dev tools (pytest, ruff, pip-audit,
  detect-secrets, pulumi). CI always installs with `uv sync --frozen` so the
  lockfile is authoritative. Also holds the ruff config (line length 100, import
  sorting) and pytest config (tests live in `tests/unit`). Note the
  `per-file-ignores` for `E402` on `agents/custom/agent.py` — that file
  *intentionally* runs code (the monkey-patch and MLflow setup) before some imports.
- **`requirements.txt` (root)** — a convenience pip mirror for non-uv users.
- **`agents/custom/requirements.txt`** — the **separate, pinned** dependency set for
  the Docker image only. Bumping agent deps means rebuilding the image, not touching
  the repo lockfile. (See [03-custom-agent.md](03-custom-agent.md).)
- **`.env.example`** — documents every supported env var (ARNs, regions, pricing
  overrides, the Custom agent's `GATEWAY_URL`/offload/feature settings, and the
  optional MLflow endpoint). Copy to `.env` for local runs.

## `docs/` — prose wired into the app

Not code, but consumed by code:
[components/docs.py](../apps/comparison/components/docs.py) renders everything under
`docs/**/*.md` (plus the root README), Mermaid diagrams and all, inside the app's
**Docs** view. The contents are the architecture/FinOps/caching/quality write-ups
that back the design decisions this guide describes.

## `aws/gateway/deployed-state.json` and `.vscode/`

`deployed-state.json` is the record written by the legacy
[deploy.py](../infrastructure/gateway/deploy.py) — historical ids/ARNs of the
originally hand-provisioned resources, now owned by Pulumi via import. `.vscode/`
holds editor defaults and recommended extensions; no runtime effect.

---

### The release story, end to end

```text
open a PR ────────────────► ci.yml `validate`  (lint, tests, streamlit smoke, audit, secrets)
merge to main ────────────► ci.yml `agentcore-evaluation`  (smoke-eval the deployed dev Runtime)
click "Deploy custom agent"► version prompt → build+push SHA image → pulumi up → EVALUATE candidate
                                                                             │
                                              gate fails ⇒ red build, artifact uploaded for triage
```

Next: [06-services-and-finops.md](06-services-and-finops.md) — the measured cost
model, prompt versioning, and security services in detail.
