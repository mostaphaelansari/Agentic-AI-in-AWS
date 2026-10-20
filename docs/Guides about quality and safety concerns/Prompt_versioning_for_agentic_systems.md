---
title: "Prompt Versioning For Agentic Systems"
date: 2026-06-22
as_of: 2026-06-22
last_verified: 2026-06-30
author: Mostapha EL ANSARI
tags:
  - agents
  - prompts
  - versioning
  - evaluation
  - deployment
  - observability
status: internal
---

# Prompt Versioning For Agentic Systems

> [!summary]
> Prompt versioning identifies controlled changes to the instruction layer and the prompt-assembly code that turns those instructions into model input. Agent versioning identifies the complete deployable behavior bundle: prompt, model, inference settings, tool descriptions and schemas, orchestration loop, retrieval and memory configuration, safety controls, output contract, application code, infrastructure, and evaluation suite. Use prompt versions for instruction identity, agent versions for production behavior identity, immutable hashes and Git commits for reproducibility, regression evaluations for approval, and per-run metadata for attribution.

## Definition

A prompt version identifies the controlled instruction layer and the code that turns those instructions into a model request. In a simple chatbot, that may be one system message.

In this document, prompt versioning usually covers:

```text
system instructions
  + developer instructions
  + prompt templates
  + examples or few-shot instructions
  + prompt-assembly code
```

An agentic system's effective model input is broader than the prompt version. It may include:

```text
System and developer instructions
  + tool names, descriptions, and schemas
  + agent-loop and framework-generated instructions
  + retrieved documents and memory
  + runtime metadata such as date, identity, tenant, and permissions
  + conversation history
  + current user input
  = effective model input
```

Only the controlled instruction layer and prompt-assembly behavior belong directly to prompt versioning. The other inputs still affect behavior, but they should be attributed as agent-version dependencies or per-run metadata instead of being collapsed into the prompt version.

## Prompt Version Versus Agent Version

Prompt versioning and agent versioning are related, but they are not the same control.

| Concept | Answers | Usually includes | Does not include |
|---|---|---|---|
| Prompt version | Which instruction set and prompt assembly produced the model request? | System and developer instructions, examples, prompt templates, prompt-assembly code, content hashes | Tool results, user conversation history, retrieved runtime data, full container or infrastructure identity |
| Agent version | Which complete deployed behavior produced the run? | Prompt version, model ID, inference settings, tools, tool schemas, agent loop, retrieval and memory policy, safety controls, output contract, runtime code, dependencies, container digest, infrastructure config, evaluation evidence | Per-run user input, live tool responses, timestamps, tenant-specific secrets |

A prompt version is therefore one important component of an agent version. If only the wording in the system prompt changes, that is a prompt-version change and usually an agent-version change because the deployed behavior changed. If the prompt stays identical but the model, tool schema, maximum iterations, parser, safety policy, or runtime code changes, that is still an agent-version change even though the prompt version did not change.

## Template Version Versus Rendered Prompt Version

A template and the message sent to the model are different artifacts.

```text
Template:
  Today is {current_date}. Use the supplied tools...

Rendered prompt:
  Today is 2026-06-22. Use the supplied tools...
```

Record both when dynamic rendering is used:

- **Template hash:** identifies the release-controlled instructions.
- **Renderer version:** identifies the code and rules that substitute variables.
- **Rendered hash:** identifies what was actually sent for this run.
- **Safe variable metadata:** records non-sensitive values needed for reproduction.

Never derive a new semantic release merely because the date, tenant, or user changed. Those are runtime variables. Increment the release when the template, renderer, variable contract, or default behavior changes.

## Recommended Prompt-Version Manifest

A machine-readable prompt-version manifest should connect the team's logical prompt version to immutable source evidence. For example:

```yaml
schema_version: 1

prompt:
  name: weather-agent-system-prompt
  app_prompt_version: 1.3.0
  template: agents/custom/prompts/system.md
  template_sha256: "a327866ea8f8a8d698dfc6c615ea678efe24f7752246d32c9c9e6d0f85d2a593"

source:
  git_commit: "<full-shared-repo-commit-sha>"

rendering:
  renderer: "agents/custom/agent.py:_load_system_prompt"
  # Include only when dynamic rendering changes the final model prompt.
  rendered_sha256: "<hash-if-dynamic-rendering-is-used>"
```

This is intentionally smaller than an agent-release manifest. It records the authored prompt identity, the exact prompt-file content, the shared source commit that produced it, and optional rendered-prompt identity. Model ID, tools, runtime code, container image, Gateway configuration, safety policy, and evaluation suite belong to agent versioning unless they are themselves part of the prompt text being versioned.

The manifest should be generated or verified in CI. Fields must come from source files and CI source identity rather than duplicated values that can silently drift. If AWS Prompt Management is used, AWS already displays the prompt ID, prompt ARN, AWS prompt version, and prompt-version ARN. Copy those into the Git manifest only if the team wants Git to be the lookup point for the AWS snapshot.

### Where Each Manifest Field Comes From

For this repository, treat the prompt-version manifest as an index over source-controlled prompt files, generated hashes, and the shared source commit:

| Manifest field | Source in this repo | How to fill it |
|---|---|---|
| `schema_version` | Manifest schema owned by the repo | Increment only when the manifest format changes. |
| `prompt.name` | Prompt release convention | Use a stable name for the prompt being versioned, such as `weather-agent-system-prompt`. |
| `prompt.app_prompt_version` | Prompt release decision | Assign manually when the instruction layer changes; do not derive it from the date, branch, or user input. |
| `prompt.template` | `agents/custom/prompts/system.md` for Custom Runtime; `agents/managed/system-prompt.md` for Managed Harness | Record the prompt file path used by the release. |
| `prompt.template_sha256` | Prompt file content | Generate from the exact prompt file after canonicalization, for example with `sha256sum agents/custom/prompts/system.md`. |
| `source.git_commit` | GitHub Actions `${{ github.sha }}` or `git rev-parse HEAD` | Record the shared repository commit that produced this prompt version. Do not use a developer's uncommitted local state. |
| `rendering.renderer` | Prompt-loading and prompt-assembly code | For Custom Runtime, this is `_load_system_prompt()` in `agents/custom/agent.py`; for Managed Harness, it is the prompt read and `system_prompts` assignment in `infrastructure/pulumi/managed_agent/__main__.py`. |
| `rendering.rendered_sha256` | Rendered prompt sent to the model | Record when dynamic rendering is used. Omit when the template is the entire prompt. |

When AWS Prompt Management is used, apply small tags such as `app_prompt_version`, `template_sha256`, and `git_commit` to the AWS prompt version. Then use the AWS console or API as the source for AWS-owned identifiers such as prompt ID, prompt ARN, AWS prompt version, and prompt-version ARN.

## Recommended Agent-Version Manifest

An agent-version manifest includes the prompt-version fields and adds the rest of the deployed behavior bundle. Use it when you need to identify, compare, approve, or roll back the complete AgentCore behavior that users interact with.

```yaml
schema_version: 1

agent:
  name: weather-agent
  app_agent_version: 1.4.0
  target: custom-runtime

prompt:
  name: weather-agent-system-prompt
  app_prompt_version: 1.3.0
  template: agents/custom/prompts/system.md
  template_sha256: "a327866ea8f8a8d698dfc6c615ea678efe24f7752246d32c9c9e6d0f85d2a593"

source:
  git_commit: "<full-shared-repo-commit-sha>"

model:
  provider: aws-bedrock
  model_id: "<resolved-bedrock-model-id>"
  inference_config_sha256: "<hash-of-model-and-inference-settings>"

tools:
  catalog_version: "<tool-catalog-version>"
  schema_sha256: "<hash-of-tool-names-descriptions-and-schemas>"

agent_loop:
  version: "<agent-loop-version>"
  max_iterations: 12

safety:
  policy_version: "<safety-policy-version>"
  configuration_sha256: "<hash-of-safety-configuration>"

output:
  event_schema_version: "<stream-or-response-contract-version>"

runtime:
  agentcore_resource_arn: "arn:aws:bedrock-agentcore:..."
  image_digest: "sha256:<resolved-container-image-digest>"
  infrastructure_config_sha256: "<hash-of-resolved-runtime-config>"

evaluation:
  suite_version: "<evaluation-suite-version>"
  dataset_sha256: "<hash-of-evaluation-dataset>"
  baseline_agent_version: "<previous-approved-agent-version>"
```

The `prompt` block is the same prompt-version identity described above. The remaining blocks explain why the same prompt can still behave differently after deployment. If only the prompt changes, update the prompt version and create a new agent version for the deployed behavior. If model, tools, runtime, policy, or parser changes while the prompt stays the same, keep the prompt version unchanged and create a new agent version.

## Operational Rules

- If the prompt text or prompt-assembly behavior changes, update the prompt version and prompt hash.
- If that changed prompt is deployed into AgentCore, also create a new agent version.
- If model, tools, runtime configuration, safety policy, or parser changes while the prompt stays the same, keep the prompt version and create a new agent version.
- In traces, record at least `prompt.app_prompt_version`, `prompt.template_sha256`, `source.git_sha`, `model.id`, and the deployed agent release/version.
- Do not log raw prompts just to prove identity. Log approved hashes and version labels instead.

## Prompt Versioning In This Repository

This repository has two agent deployment paths. They share a behavioral goal but package prompts differently.

### Managed AgentCore Harness

The managed system prompt is stored at:

```text
agents/managed/system-prompt.md
```

`infrastructure/pulumi/managed_agent/__main__.py` reads that file and supplies it as `system_prompts` together with the model, tools, maximum iterations, and timeout. Therefore, the meaningful managed release is at least:

```text
Git commit
prompt content hash
Pulumi stack and resolved configuration
model ID
tool configuration and Gateway target
max iterations and timeout
Harness resource ID
evaluation-suite version
```

> [!warning]
> When `import_harness_id` is configured, the current Pulumi resource ignores changes to `systemPrompts`, `model`, `tools`, `memory`, and `maxTokens`. This was useful for initially adopting the live Harness, but it means a changed prompt file may produce no deployed prompt change. Review and deliberately narrow or remove the relevant `ignore_changes` entries after adoption, then verify the live Harness configuration.

The managed workflow runs linting and tests, previews the Pulumi change, and applies the stack. A stronger prompt-release gate would add manifest verification, candidate-versus-baseline evaluation, and post-deployment identity verification before promotion.

### Custom AgentCore Runtime

The custom system prompt is stored at:

```text
agents/custom/prompts/system.md
```

The Dockerfile copies the entire `prompts` directory into the image, and `agents/custom/agent.py` loads the prompt at runtime. The deployment workflow tags the built image with `${github.sha}` and first checks whether that tag already exists. This gives a strong source-to-image relationship.

There are two important boundaries:

1. The image does not fully describe the runtime. Pulumi also supplies the model ID, Gateway URL, offload bucket, caching flag, and thinking flag as environment variables.
2. The ECR repository is configured with mutable tags. The workflow convention avoids rebuilding an existing Git-SHA tag, but the registry itself does not enforce tag immutability. Record and deploy the resolved image digest when exact artifact identity matters.

When `import_runtime_id` is configured, the custom Runtime currently ignores `environmentVariables`. As with the managed path, verify that adoption settings do not hide an intended release change.

### Shared Behavior And Comparison Fairness

At this checkout, the managed and custom system-prompt files have identical content. That is useful for an architecture comparison: differences are less likely to come from different written instructions.

Treat parity as an explicit policy rather than an accident. CI can either:

- Require the two files to remain byte-identical, or
- Allow divergence but require separate prompt versions, release notes, and evaluation baselines.

The comparison backends also prepend current-date metadata to user messages. That is runtime context, not a new prompt release. Record the rendered request hash and date separately from the template version.

The application-level prompt-disclosure precheck is another behavioral dependency. If that filter changes, the agent release can accept or reject different requests even when neither system-prompt file changes.

## Short Checklist

```text
[ ] Prompt version and prompt hash changed when instruction text changed
[ ] Agent version changed when deployed behavior changed
[ ] Git commit and source prompt path are recorded
[ ] Managed and custom prompts are intentionally equal or intentionally separate
[ ] AWS Prompt Management versions, if used, are tagged with app_prompt_version, template_sha256, and git_commit
[ ] Live deployment was verified after changes to prompt, model, tools, runtime, or policy
[ ] Raw prompts, secrets, and sensitive runtime data are not logged as release metadata
```

## Related Repository Documents

- `docs/14_Evaluation_and_quality_for_AI_agents.md` — datasets, golden traces, regression gates, and online evaluation
- `docs/13_Bedrock_Guardrails_hallucination_prevention.md` — Guardrails and the broader safety-control stack
- `docs/08_Caching_prompt_semantic_tool_resource.md` — prompt, semantic, tool, and resource caching boundaries
- `docs/12_Caching_walkthrough_in_this_repo.md` — current custom-agent prompt-caching implementation
- `docs/cicd.md` — deployment workflows and infrastructure lifecycle
