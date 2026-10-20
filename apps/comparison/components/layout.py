"""Top-level Streamlit layout elements."""

from __future__ import annotations

import streamlit as st
from config import ARCHITECTURES, BEDROCK_CHEAP_TOKEN_PRICING, BEDROCK_TOKEN_PRICING


def render_header(*, custom_only: bool = False) -> None:
    title = "Custom agent" if custom_only else "Managed agent vs Custom agent"
    subtitle = (
        "Chat directly with the repository-owned agent running on AgentCore Runtime."
        if custom_only
        else "Compare latency, tool use, token usage, and answer quality for one prompt."
    )
    legend = (
        "<span>Custom agent</span><span>Real-time stream</span>"
        if custom_only
        else "<span>Managed agent</span><span>Custom agent</span><span>Real-time streams</span>"
    )
    st.markdown(
        f"""
        <div class="compare-hero">
          <div>
            <h1 class="compare-title">{title}</h1>
            <p class="compare-subtitle">{subtitle}</p>
          </div>
          <div class="compare-legend">{legend}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_comparison_summary() -> None:
    managed = ARCHITECTURES["Managed agent"]
    custom = ARCHITECTURES["Custom agent"]
    rates = BEDROCK_TOKEN_PRICING
    cheap_rates = BEDROCK_CHEAP_TOKEN_PRICING
    with st.expander("How the two agents differ", expanded=False):
        st.markdown(
            f"""
| | Managed agent | Custom agent |
|---|---|---|
| **AWS call** | `invoke_harness` | `invoke_agent_runtime` (SSE) |
| **Region** | `{managed["region"]}` | `{custom["region"]}` |
| **Agent loop** | Hosted by AgentCore Harness | Repository-owned Strands agent |
| **Reasoning** | Harness model configuration | Bedrock additional request fields |
| **Prompt caching** | Provider-level system checkpoint (experimental) | Explicit runtime configuration |
| **Model routing** | Per-invocation model override | Per-invocation Strands model selection |

Token formulas:

```text
Prompt = all input-side tokens reported by the backend
Total tokens = Prompt + Out
```

Token cost measurement:

| Routed tier | Input / 1M | Output / 1M | Cache read / 1M | Cache write / 1M |
|---|---:|---:|---:|---:|
| **Economy (Haiku)** | `${cheap_rates["input_per_mtok"]:.2f}` | `${cheap_rates["output_per_mtok"]:.2f}` | `${cheap_rates["cache_read_per_mtok"]:.2f}` | `${cheap_rates["cache_write_per_mtok"]:.2f}` |
| **Quality (Sonnet)** | `${rates["input_per_mtok"]:.2f}` | `${rates["output_per_mtok"]:.2f}` | `${rates["cache_read_per_mtok"]:.2f}` | `${rates["cache_write_per_mtok"]:.2f}` |

| Case | Measured as |
|---|---|
| **Caching enabled** | Fresh input, cache reads/writes, and output × the selected model's rates above |
| **Without caching** | All prompt/input-side tokens × selected input rate + output × selected output rate |
| **Difference** | Cached token cost − without-cache token cost. Negative means caching saved money; positive means caching cost extra. |
"""
        )
