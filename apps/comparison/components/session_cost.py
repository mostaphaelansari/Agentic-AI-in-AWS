"""Sidebar widget with the running cost of the whole chat session.

One card per architecture (Managed agent, Custom agent) so the comparison the
app exists for is visible continuously, not only in the per-run cost table.
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st
from services.session_cost import (
    grand_total,
    is_partial,
    refresh_pending_compute,
    session_totals,
    total_cost,
)

ARCH_CLASS = {"Managed agent": "managed", "Custom agent": "custom"}


def _money(amount: float) -> str:
    if amount and amount < 0.01:
        return f"${amount:.6f}"
    return f"${amount:,.4f}" if amount < 1 else f"${amount:,.2f}"


def _duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs:02d}s"


def _row(label: str, quantity: str, amount: str) -> str:
    return (
        '<div class="session-cost-row">'
        f"<span>{html.escape(label)}</span>"
        f'<span class="session-cost-qty">{html.escape(quantity)}</span>'
        f'<span class="session-cost-amount">{html.escape(amount)}</span>'
        "</div>"
    )


def _card_html(total: dict[str, Any]) -> str:
    arch_name = total["arch_name"]
    token_pending = total["token_pending_runs"]
    token_quantity = f"{total['total_tokens']:,} tok"
    token_amount = _money(total["token_cost"])
    if token_pending:
        token_quantity += f" · {token_pending} pending"
        token_amount = "pending" if not total["token_cost"] else f"{token_amount} + pending"
    rows = [
        _row(
            "Tokens",
            token_quantity,
            token_amount,
        ),
        _row(
            "Gateway",
            f"{total['operations']} ops",
            _money(total["gateway_cost"]),
        ),
    ]
    if token_pending:
        rows.append(
            '<p class="session-cost-note">'
            f"{token_pending} run(s) awaiting token usage. "
            "Pending usage is excluded from the subtotal.</p>"
        )
    if total["is_runtime"]:
        pending = total["runtime_pending_runs"]
        rows.append(
            _row(
                "Runtime",
                f"{total['vcpu_hours']:.6f} vCPU-h · {total['memory_gb_hours']:.6f} GB-h",
                "pending"
                if pending and not total["runtime_cost"]
                else _money(total["runtime_cost"]),
            )
        )
        if pending:
            rows.append(
                '<p class="session-cost-note">'
                f"{pending} run(s) awaiting vended Runtime usage — use "
                "<em>Refresh session cost</em> above.</p>"
            )
    else:
        rows.append(_row("Runtime", "not applicable", "—"))
    if total["builtin_tool_calls"]:
        builtin_pending = total["builtin_pending_runs"]
        quantity = f"{total['builtin_tool_calls']} calls"
        if not builtin_pending:
            quantity += (
                f" · {total['builtin_vcpu_hours']:.6f} vCPU-h"
                f" · {total['builtin_memory_gb_hours']:.6f} GB-h"
            )
        rows.append(
            _row(
                "Browser / code",
                quantity,
                "pending" if builtin_pending else _money(total["builtin_cost"]),
            )
        )
        if builtin_pending:
            rows.append(
                '<p class="session-cost-note">'
                f"{builtin_pending} run(s) awaiting Browser / Code Interpreter "
                "USAGE_LOGS.</p>"
            )
    savings = total["cache_savings"]
    if total["cache_read"] or total["cache_write"]:
        verdict = "saved" if savings >= 0 else "extra"
        rows.append(
            f'<p class="session-cost-note">Prompt caching {verdict} '
            f"{_money(abs(savings))} this session.</p>"
        )
    return f"""
    <div class="session-cost-card {ARCH_CLASS.get(arch_name, "")}">
      <div class="session-cost-card-head">
        <span class="session-cost-arch">{html.escape(arch_name)}</span>
        <span class="session-cost-card-total">{_money(total_cost(total))}</span>
      </div>
      {"".join(rows)}
    </div>
    """


def session_cost_html(
    live_states: dict[str, dict] | None = None,
    architectures: tuple[str, ...] | None = None,
) -> str:
    totals = session_totals(live_states)
    if architectures is not None:
        totals = {name: totals[name] for name in architectures if name in totals}
    running = bool(live_states) and any(
        not state.get("done") for state in (live_states or {}).values()
    )
    runs = max((total["runs"] for total in totals.values()), default=0)
    elapsed = max((total["elapsed"] for total in totals.values()), default=0.0)
    dot = '<span class="session-cost-live" title="updating"></span>' if running else ""
    note = ""
    if not totals:
        note = '<p class="session-cost-note">No runs in this chat yet.</p>'
    elif is_partial(totals):
        note = (
            '<p class="session-cost-note">Partial: delayed token or compute usage '
            "is not in the subtotal yet.</p>"
        )
    return f"""
    <div class="session-cost-widget">
      <div class="session-cost-head">
        <span class="session-cost-title">Session cost{dot}</span>
        <span class="session-cost-total">{_money(grand_total(totals))}</span>
      </div>
      <div class="session-cost-meta">
        <span><strong>{runs}</strong> runs</span>
        <span><strong>{_duration(elapsed)}</strong> agent time</span>
      </div>
      {"".join(_card_html(total) for total in totals.values())}
      {note}
    </div>
    """


def _render_session_cost_contents(
    page_scope: str = "comparison",
    architectures: tuple[str, ...] | None = None,
) -> None:
    from services.run_controller import ACTIVE_RUN_KEY, get_run

    run_key = (
        ACTIVE_RUN_KEY
        if page_scope == "comparison"
        else f"{ACTIVE_RUN_KEY}:{page_scope}"
    )
    controller = get_run(st.session_state.get(run_key))
    live_states = controller.snapshot() if controller is not None else None
    if st.button(
        "Refresh session cost",
        key=f"refresh_session_compute_costs_{page_scope}",
        help="Fetch delayed Runtime, Browser, and Code Interpreter USAGE_LOGS.",
        width="stretch",
    ):
        with st.spinner("Fetching usage logs…"):
            checked, resolved = refresh_pending_compute()
        if not checked:
            st.caption("No usage is pending for this chat.")
        elif resolved:
            st.caption(f"Resolved {resolved} of {checked} pending run(s).")
        else:
            st.caption("Usage is still pending in CloudWatch; try again shortly.")
    st.markdown(
        session_cost_html(live_states, architectures=architectures),
        unsafe_allow_html=True,
    )


_session_cost_fragment = None


def mount_session_cost_widget(
    *,
    page_scope: str = "comparison",
    architectures: tuple[str, ...] | None = None,
) -> None:
    """Render a short auto-refreshing sidebar fragment during active runs."""
    global _session_cost_fragment
    # Wrapped on first render rather than at import time, so the fragment binds
    # to whichever Streamlit module is in scope and no Streamlit call happens
    # while this module is merely being imported.
    if _session_cost_fragment is None:
        _session_cost_fragment = st.fragment(run_every=1.0)(_render_session_cost_contents)
    _session_cost_fragment(page_scope, architectures)


def render_session_cost_widget(live_states: dict[str, dict] | None = None) -> None:
    """Compatibility helper for callers that need a one-off cost snapshot."""
    st.markdown(session_cost_html(live_states), unsafe_allow_html=True)
