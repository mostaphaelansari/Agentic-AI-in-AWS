---
title: "AI Agent Ownership And Responsibility"
date: 2026-06-24
as_of: 2026-06-30
last_verified: 2026-06-30
author: Mostapha EL ANSARI
tags:
  - aws
  - bedrock
  - agentcore
  - ownership
  - governance
  - operations
  - security
status: internal
---

# AI Agent Ownership And Responsibility

> [!summary]
> Agent ownership is the explicit answer to: who decides what the agent is allowed to do, who builds and operates it, and who answers if the result is wrong. AWS can own managed infrastructure and service mechanics. The customer still owns configuration, data, tools, policies, quality thresholds, release decisions, and business risk.

## Definition

Ownership is not one job title. It separates four questions:

| Question | Meaning | Example |
|---|---|---|
| Who decides? | Decision authority | Product owner defines which user goals the agent may pursue |
| Who implements? | Engineering responsibility | Agent engineer changes the prompt, tool loop, and output contract |
| Who operates? | Day-to-day operational duty | Platform team deploys, monitors, rolls back, and restores service |
| Who is accountable? | Final responsibility for the outcome | Business owner accepts or rejects the risk of automated refunds |

These roles can belong to one person in a small project, but they are not the same thing.

```text
Control = what a component or team can configure or change.
Responsibility = work a person or team is expected to perform.
Accountability = who answers for the result and accepts the residual risk.
```

AWS can control and operate a managed service. The customer still owns how that service is configured and used: what data is supplied, which tools are exposed, which permissions are granted, which policies are enforced, and whether the agent is suitable for the business process.

An agent can take delegated actions, but it is never accountable for:

- its own permissions;
- the correctness of its instructions;
- the truth of external data;
- the business policy it applies;
- the acceptable level of risk;
- incident response;
- legal, operational, or business consequences.

Short version:

```text
AWS can own the platform.
Your team owns the agent's behavior and business risk.
```

## Why It Matters

Agents are not only chat interfaces. They combine prompts, models, tools, data, identity, policies, runtime infrastructure, traces, and evaluations. A failure often crosses several boundaries:

```text
Prompt change
  -> different tool choice
  -> valid but over-privileged API call
  -> incorrect business action
  -> incomplete trace
  -> alert with no responder
```

Each component can be working as designed while the end-to-end result is still unsafe, incorrect, or impossible to explain.

Ownership matters because it gives the team:

- a named person or team who can approve risky changes;
- a clear boundary for what the agent is allowed to do;
- review paths for prompts, tools, models, policies, and schemas;
- traceability from a bad output back to a deployed version;
- separation between model instructions and real authorization;
- evaluation thresholds with someone empowered to act on failures;
- a retirement path for old agents, tools, credentials, memory, and logs.

The useful test is:

```text
If this signal turns red at 02:00, who is expected to decide and what can they do?
```

## When It Bites

Ownership problems usually appear late: during release approval, incidents, audits, or production drift. Common failure modes are:

### Managed Service Is Mistaken For Managed Outcome

AgentCore Runtime can manage hosting, scaling, session isolation, and runtime infrastructure. AgentCore Harness can manage the orchestration loop. Neither service defines the application's correct business outcome.

```text
Managed infrastructure can run an unsafe configuration perfectly.
Managed observability can show an incident nobody owns.
Managed policy can enforce an overly broad customer-authored rule.
```

### Everyone Owns The Agent, So Nobody Owns It

Shared contribution is healthy. Shared accountability is usually ambiguous.

Warning signs:

- a Slack channel is listed as the owner;
- every alert routes to a large distribution list;
- product defines success but cannot stop a release;
- security owns policy but not policy tests;
- operations can roll back infrastructure but cannot identify the prompt/model version;
- a vendor is named as owner of a customer business rule.

### Loop Ownership And Outcome Ownership Are Confused

The component that advances the agent loop is not necessarily accountable for the final result.

```text
Harness or Strands owns loop mechanics.
The application team owns configured behavior.
The domain owner owns business-rule meaning.
The product owner owns whether automation is acceptable.
```

This matters when comparing Managed Harness and Custom Runtime. More loop control moves into project code in Custom Runtime, but business accountability still has to be assigned either way.

### Tool Ownership Stops At The Schema

A tool can have a valid schema and still be operationally unsafe. Production tools need owners for:

- side effects;
- breaking response changes;
- stale credentials;
- timeouts and rate limits;
- retries and idempotency;
- whether the action is read-only, reversible, approval-gated, or irreversible.

### Identity Ownership Is Replaced By A Generic Agent Identity

The runtime execution role answers what the workload can access. It does not always answer which end user requested the action or whether that user is allowed to perform it.

Keep these identities separate:

| Identity | Question it answers |
|---|---|
| Human/end-user identity | Who requested the outcome? |
| Workload identity | Which deployed agent is calling? |
| Tool/resource identity | Which target is being accessed? |
| Deployment identity | Who or what changed production? |
| Approval identity | Who accepted a high-risk action or exception? |

Collapsing these into one role weakens attribution and can turn a least-privilege problem into a tenant-isolation incident.

### Behavior Changes Have No Artifact Owner

Prompts, model IDs, tool descriptions, schemas, inference settings, and policy files all change behavior. If they are treated as informal configuration, the team may not know:

- which prompt produced a trace;
- who approved a model change;
- whether evaluation used the deployed configuration;
- whether a live override changed behavior;
- which owner must approve rollback.

### Quality Metrics Have No Decision Owner

An evaluation score is only useful if someone owns the threshold and the response. Missing ownership looks like:

- nobody defines the minimum acceptable score;
- averages hide a failed critical case;
- a failing release can proceed without a named waiver approver;
- the dataset is stale but has no curator;
- alerts do not map to a runbook or responder.

### Live State Drifts From Declared State

Infrastructure as code can describe intended state while the live service contains imported, ignored, or manually changed fields. Ownership requires both a source of truth and a reconciliation process.

In this repo, review especially:

- `infrastructure/pulumi/managed_agent/__main__.py` because imported Harness fields can be ignored during adoption;
- `infrastructure/pulumi/managed_agent/__main__.py` because broad `allowed_tools=["*"]` needs an explicit owner before production;
- `infrastructure/pulumi/gateway/__main__.py` because broad Gateway permissions need security ownership;
- `apps/comparison/config.py` because stale ARNs or regions can make the UI call the wrong live resource.

## AWS Option Vs Do-It-Yourself

AWS services can reduce how much infrastructure and glue code you operate. They do not remove the need to assign owners.

| Area | AWS option | Do-it-yourself option | What the customer still owns |
|---|---|---|---|
| Hosting | AgentCore Runtime manages runtime infrastructure and isolated sessions | ECS, EKS, EC2, Lambda, or another host | Agent code, dependencies, configuration, release decision |
| Agent loop | AgentCore Harness manages the configured orchestration loop | Strands/custom loop in Runtime or another orchestrator | Prompt, model, tools, limits, acceptable outcomes |
| Tools | AgentCore Gateway exposes APIs, Lambda functions, or MCP tools | Custom MCP server, API gateway, or direct clients | Tool selection, schema, side-effect policy, permissions |
| Authorization | AgentCore Policy can enforce Cedar policies for Gateway tool calls | Custom policy middleware or service | Business policy meaning, policy review, boundary tests |
| Identity | IAM, JWT authorizers, AgentCore Identity | Custom identity broker and token handling | Claim trust, role mapping, least privilege, credential lifecycle |
| Memory | AgentCore Memory stores events and extracted records | Database, vector store, object storage | Allowed content, namespaces, retention, deletion, retrieval safety |
| Observability | AgentCore emits logs, metrics, and traces to AWS observability surfaces | Custom OpenTelemetry/logging backend | Instrumentation, retention, alerts, thresholds, responders |
| Evaluation | AgentCore Evaluations provides managed evaluation surfaces | Custom datasets, assertions, judges, runners | Dataset truth, thresholds, critical cases, waivers, release action |
| Registry | AWS Agent Registry catalogs agents, tools, skills, and related resources | Internal catalog or CMDB | Record accuracy, owner fields, approval status, deprecation |
| Deployment | AWS APIs, Pulumi, GitHub Actions | Custom scripts or console operations | Source of truth, approval, credentials, rollback, drift reconciliation |

```text
Managed service reduces implementation and operations work.
It does not transfer customer decision rights or accountability.
```

For a lightweight do-it-yourself ownership model, keep a small registry in the repository or service catalog:

```yaml
agent:
  name: comparison-custom-agent
  product_owner: team-or-person
  technical_owner: team-or-person
  operations_owner: on-call-service
  security_owner: team-or-person
  data_owner: team-or-person
  repository: DataSentics/Aws-agentic-blueprint
  runtime: agentcore-runtime
  environment: dev
  risk_tier: read-only-demo
  tools:
    - name: open-meteo
      owner: team-or-person
      side_effect_class: read-only
  runbook: URL-or-repository-path
  evaluation_gate: evaluations/dataset.json
  review_by: YYYY-MM-DD
```

Do not put secrets or personal data in this metadata. Prefer stable team and on-call identities, but make sure they resolve to real humans during incidents.

## Repository Mapping

| Surface | Current source of truth | AWS/mechanism owner | Project owner still required |
|---|---|---|---|
| Managed agent loop | `infrastructure/pulumi/managed_agent/__main__.py` plus `agents/managed/system-prompt.md` | AgentCore Harness | Prompt, model, tools, limits, invocation overrides, quality |
| Custom agent loop | `agents/custom/agent.py` and its container dependencies | Project code running on AgentCore Runtime | All loop behavior, callbacks, limits, tools, errors, quality |
| Custom prompt | `agents/custom/prompts/system.md` | Project | Instruction content, review, regression, release |
| Shared weather tools | `infrastructure/pulumi/gateway/__main__.py` and `infrastructure/gateway/open_meteo_lambda.py` | Gateway/Lambda plus project configuration | Schema, API behavior, IAM, availability, compatibility |
| Managed invocation adapter | `apps/comparison/backends/harness.py` | Project | Overrides, stream normalization, identity/session correlation |
| Custom invocation adapter | `apps/comparison/backends/runtime.py` | Project | Payload contract, stream parsing, failures, session correlation |
| UI configuration | `apps/comparison/config.py` and environment variables | Project | Correct live ARNs/regions/model assumptions and config ownership |
| Infrastructure state | Three Pulumi stacks under `infrastructure/pulumi/` | Pulumi executes; project declares | Service readiness review, imports, drift, permissions, state access |
| Deployment identity | GitHub Actions OIDC roles per deployment unit | GitHub/AWS mechanisms | Role scope, environment protection, approvals, audit |
| Quality gate | `evaluations/` and Custom Runtime workflow evaluation | AgentCore Evaluations plus project runner | Cases, assertions, threshold, failure action, coverage gaps |
| Telemetry | AgentCore/CloudWatch and optional MLflow trace mirror | AWS/project instrumentation | Retention, access, correlation, alert thresholds, response |

Managed versus Custom in this repo:

```text
Managed Harness:
  AWS owns the internal loop implementation.
  This repo owns the Harness declaration and invocation behavior.

Custom Runtime:
  AWS owns the managed runtime platform.
  This repo owns the Python/Strands loop and container behavior.

Both:
  This project owns the user promise, exposed tools, IAM configuration,
  data handling, evaluation evidence, release decision, and incident response.
```

This is why "Managed" should mean lower implementation and operational surface, not lower customer accountability.

## Validation Questions

### Definition And Accountability

1. Who is the single accountable owner for the agent's business outcome?
2. Can that owner approve, limit, pause, or retire the agent?
3. Who owns the meaning of the domain rules the agent applies?
4. Is “AWS,” “the model,” “the vendor,” or a group channel incorrectly named as the accountable owner?
5. Are decision authority, implementation responsibility, operational duty, and accountability separated?

### AWS Option Vs DIY

6. Which parts are AWS-managed: Runtime, Harness, Gateway, Policy, Memory, Observability, Evaluations, Registry?
7. Which parts are project-owned: prompt, model config, tool scope, data handling, release decision, incident response?
8. If using Managed Harness, who owns the configured behavior?
9. If using Custom Runtime, who owns the custom loop code and container behavior?
10. If doing it yourself, who owns the extra hosting, orchestration, policy, telemetry, and evaluation code?

### Prompts, Models, Tools, And Schemas

11. Who reviews and approves prompt, model, inference-setting, skill, and tool-description changes?
12. Can every production trace be mapped to the deployed behavioral artifacts?
13. Does every tool have an owner, schema, side-effect class, timeout, retry, and idempotency contract?
14. Who approves adding a tool or expanding `allowed_tools`?
15. Who owns schema versioning and consumer compatibility?

### Identity And Security

16. Are end-user, workload, deployment, tool, and approval identities distinguishable?
17. Who owns least-privilege review for every execution and deployment role?
18. Are business permissions enforced outside model instructions?
19. Who reviews Cedar or other authorization policies and their boundary tests?
20. Can the security owner disable access without waiting for an application release?

### Data And State

21. Who owns each class of prompt, trace, memory, file, tool result, and evaluation data?
22. Are purpose, residency, retention, deletion, and inspection rights documented?
23. Who handles user deletion, legal hold, incident evidence, and tenant-isolation failures?
24. Are session IDs and memory namespaces bound to authenticated users by application logic?
25. Is the owner of encryption keys and key-rotation decisions named?

### Quality, Release, And Operations

26. Who owns the evaluation dataset and keeps it current?
27. Who sets release thresholds and can approve a time-bounded waiver?
28. Are critical cases gated individually instead of hidden inside averages?
29. Does the evaluation evidence correspond to the exact deployed candidate?
30. Do Managed and Custom paths have intentionally comparable or intentionally different gates?
31. Does every alert have an owner, severity, and prescribed action?
32. Can on-call identify the deployed prompt, model, image, tools, policies, and configuration?
33. Who owns reconciliation between Pulumi/source state and live AWS state?
34. Can the operator roll back or disable the agent and its tool access independently?
35. Is ownership reviewed when autonomy, data, tools, regions, or risk tier changes?

## Sources

- [Security in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security.html)
- [Data protection in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/data-protection.html)
- [How AgentCore Runtime works](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)
- [AgentCore Runtime IAM permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [AgentCore Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)
- [AgentCore harness is now generally available](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-agentcore-harness-generally-available/)
- [Configure AgentCore Harness agents and models](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-config-and-models.html)
- [Connect AgentCore Harness to tools](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html)
- [Policy in Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Understanding Cedar policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html)
- [AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)
- [AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)
- [AWS Agent Registry](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html)
- [Log AgentCore Gateway API calls with CloudTrail](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-cloudtrail.html)
