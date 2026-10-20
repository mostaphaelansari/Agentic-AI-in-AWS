"""Primary Streamlit app for comparing Managed and Custom agents."""

from __future__ import annotations

import logging
import re

import streamlit as st
from components.aws_cli import render_aws_cli_console
from components.comparison import (
    render_current_comparison,
    render_live_comparison,
    render_saved_comparison,
    response_markdown,
    run_comparison,
)
from components.docs import render_docs_selector, render_docs_view
from components.history import (
    consume_prompt_resubmit,
    current_comparison_entry,
    current_prompt_key,
    history_entries,
    pending_prompt_resubmit,
    render_history_selector,
    start_new_chat,
)
from components.layout import render_comparison_summary, render_header
from components.session_cost import mount_session_cost_widget
from components.styles import inject_styles
from config import BEDROCK_STRONG_MODEL_ID
from molecule_viewer import (
    MOLSTAR_VERSION,
    agent_structure_view,
    atom_record_count,
    view_specs_for_state,
)
from molstar_component import (
    render_molstar_component,
    selection_description,
    selection_prompt_context,
    structure_fingerprint,
)
from services.charts import chart_specs_for_state
from services.conversation import conversation_prompts, routing_messages, submission_branch
from services.model_router import start_router_warmup
from services.run_controller import ACTIVE_RUN_KEY, get_run
from services.security import prompt_security_message
from services.uploads import (
    delete_staged_upload,
    prepare_upload,
    prompt_with_mmcif_analysis,
    prompt_with_upload,
    stage_upload,
)

PROMPT_VIEW_AGENT = "Managed agent"
CUSTOM_AGENT = "Custom agent"
logger = logging.getLogger("unified-ui.app")
_CUSTOM_UPLOAD_SEED = "Analyze the exact uploaded mmCIF with Python in Code Interpreter."
_APPLICATION_SUMMARY_BLOCK = re.compile(
    r"\nThe application parsed this bounded metadata directly from the exact upload\..*?"
    r"</application_mmcif_summary>\n",
    re.DOTALL,
)


def active_run_key(page_scope: str = "comparison") -> str:
    """Keep page-specific keys independent of Streamlit's module reload order."""
    return ACTIVE_RUN_KEY if page_scope == "comparison" else f"{ACTIVE_RUN_KEY}:{page_scope}"


def interpreter_upload_prompt(prompt: str, staged: dict) -> str:
    """Build a tool-first upload prompt across hot-reload module versions."""
    try:
        return prompt_with_upload(
            prompt,
            staged,
            use_application_summary=False,
        )
    except TypeError as exc:
        # Streamlit reloads the entrypoint while imported modules remain in
        # sys.modules. During that one-process transition, uploads.py may still
        # expose the previous two-argument signature. Build its URL-bearing
        # form with a neutral seed, then remove the inline summary that caused
        # the Custom Runtime's oversized tool call.
        if "use_application_summary" not in str(exc):
            raise
        augmented = prompt_with_upload(_CUSTOM_UPLOAD_SEED, staged)
        augmented = augmented.replace(_CUSTOM_UPLOAD_SEED, prompt, 1)
        return _APPLICATION_SUMMARY_BLOCK.sub("\n", augmented)


def managed_upload_prompt(prompt: str, staged: dict) -> str:
    """Build the Managed-agent prompt for the staged upload type."""
    if staged.get("kind") == "mmcif":
        return prompt_with_mmcif_analysis(prompt, staged)
    return prompt_with_upload(prompt, staged)


def prompt_controlled_view_slots(
    states: dict[str, dict],
) -> list[tuple[str, dict]]:
    """Return the Managed-agent view only.

    The Custom agent still returns its normal response, but its Mol* selection
    is not displayed in the comparison UI.
    """
    specs = view_specs_for_state(states.get(PROMPT_VIEW_AGENT, {}))
    return [(PROMPT_VIEW_AGENT, specs[0])] if specs else []


def render_agent_structure_dashboard(upload: dict, states: dict[str, dict]) -> None:
    """Show Managed-agent analysis beside its validated Mol* structure view."""
    state = states.get(PROMPT_VIEW_AGENT)
    if state is None:
        return

    view = agent_structure_view(upload["content"], state)
    answer = state.get("answer", "").strip() or state.get("pending_text", "").strip()
    st.markdown("## Agent analysis + 3D view")
    st.caption("Review the Managed agent's analysis beside the molecular view it requested.")
    with st.container(border=True):
        analysis_column, structure_column = st.columns([0.9, 1.1], gap="large")
        with analysis_column:
            st.markdown("### Analysis")
            st.caption(PROMPT_VIEW_AGENT)
            with st.container(border=True, height=560):
                st.markdown(response_markdown(answer, "No analysis was returned."))
                for chart_spec in chart_specs_for_state(state):
                    st.vega_lite_chart(chart_spec, width="stretch")
        with structure_column:
            st.markdown("### 3D view")
            st.caption(view["caption"])
            if view["warning"]:
                st.warning(view["warning"])
            render_molstar_component(
                view["structure"],
                key=f"managed_dashboard_{structure_fingerprint(view['structure'])}",
                height=560,
                show_controls=False,
                show_sequence=True,
                color_theme=view["color_theme"],
            )


def render_agent_page(*, custom_only: bool) -> None:
    """Render either the two-agent comparison or the Custom-only experience."""
    page_scope = "custom" if custom_only else "comparison"
    architectures = (CUSTOM_AGENT,) if custom_only else (PROMPT_VIEW_AGENT, CUSTOM_AGENT)
    run_label = "Run Custom agent" if custom_only else "Run comparison"

    render_header(custom_only=custom_only)
    if not custom_only:
        render_comparison_summary()

    run_key = active_run_key(page_scope)
    active_run_id = st.session_state.get(run_key)
    active_controller = get_run(active_run_id)
    if active_run_id and active_controller is None:
        st.session_state.pop(run_key, None)
        active_run_id = None

    with st.sidebar:
        if st.button(
            "New chat",
            type="primary",
            width="stretch",
            disabled=active_controller is not None,
            help="Stop or finish the active run before starting a new chat.",
            key=f"new_chat_{page_scope}",
        ):
            start_new_chat(page_scope)
            st.rerun()

        st.divider()
        if custom_only:
            mount_session_cost_widget(page_scope=page_scope, architectures=architectures)
        else:
            mount_session_cost_widget()
        render_aws_cli_console()
        st.divider()
        st.header("Run settings")
        st.caption("Feature flags applied to each agent run.")
        routing_label = st.selectbox(
            "Model routing",
            ["Auto", "Economy", "Quality"],
            key=f"model_routing_{page_scope}",
            help=(
                "Auto uses the complexity classifier for ordinary prompts. "
                "Reasoning and attached-file runs always use the quality model, "
                "even when Economy is selected."
            ),
        )
        reasoning_efforts = ["Off", "Low", "Medium", "High"]
        if "claude-opus-4-6" in BEDROCK_STRONG_MODEL_ID.casefold():
            reasoning_efforts.append("Max")
        reasoning_effort_label = st.selectbox(
            "Reasoning effort",
            reasoning_efforts,
            key=f"reasoning_effort_{page_scope}",
            help=(
                "Use Bedrock adaptive thinking on the quality model. Low favors "
                "latency, medium balances depth and cost, and high favors deeper "
                "reasoning. Max is available only with Claude Opus 4.6."
            ),
        )
        enable_thinking = reasoning_effort_label != "Off"
        enable_caching = st.toggle(
            "Prompt caching",
            value=True,
            key=f"prompt_caching_{page_scope}",
            help=(
                "Enable Bedrock prompt caching for the selected agent runtime."
                if custom_only
                else "Enable Bedrock prompt caching for both agents."
            ),
        )
        st.divider()
        selected_history = render_history_selector(page_scope)

    resubmit_request = pending_prompt_resubmit(page_scope)
    with st.container(border=True, key=f"prompt_section_{page_scope}"):
        uploaded_file = st.file_uploader(
            "Upload data file",
            type=["csv", "cif"],
            accept_multiple_files=False,
            key=f"{current_prompt_key(page_scope)}_data",
            help=(
                "Upload one UTF-8 CSV or mmCIF (.cif) file up to 1 MB. The selected "
                "agent setup analyzes the bounded payload with Code Interpreter."
            ),
        )
        upload = None
        upload_error = None
        if uploaded_file is not None:
            try:
                upload = prepare_upload(uploaded_file.name, uploaded_file.getvalue())
            except ValueError as exc:
                upload_error = str(exc)
                st.error(upload_error)
            else:
                st.caption(
                    f"Ready: `{upload['name']}` ({upload['size'] / 1024:,.1f} KB). "
                    + (
                        "Ask for counts or say what Mol* should display."
                        if upload["kind"] == "mmcif"
                        else "Ask for metrics, trends, or a chart."
                    )
                )
        if upload and upload["kind"] == "mmcif":
            st.markdown("#### 3D structure preview")
            st.caption(
                f"Mol* {MOLSTAR_VERSION} · {atom_record_count(upload['content']):,} atom "
                "records · click to select residues, drag to rotate, scroll to zoom"
            )
            molstar_selection = render_molstar_component(
                upload["content"],
                key=(
                    f"{current_prompt_key(page_scope)}_structure_"
                    f"{structure_fingerprint(upload['content'])}"
                ),
                height=640,
            )
            if molstar_selection is not None:
                st.caption(f"Mol* selection: {selection_description(molstar_selection)}")
        else:
            molstar_selection = None
        prompt = st.text_input(
            "Prompt",
            placeholder=(
                (
                    "Try: show only protein chain A, residues 10–30."
                    if upload and upload["kind"] == "mmcif"
                    else "Try: summarize the numeric metrics and plot the most useful comparison."
                )
                if upload
                else (
                    "Ask the Custom agent a question."
                    if custom_only
                    else "Ask one question and compare Managed and Custom agents side by side."
                )
            ),
            key=current_prompt_key(page_scope),
        )
        source_entry = resubmit_request.get("source_entry") if resubmit_request else None
        revision_upload_name = (
            source_entry.get("options", {}).get("uploaded_file") if source_entry else None
        )
        missing_revision_upload = bool(
            revision_upload_name and (upload is None or upload.get("name") != revision_upload_name)
        )
        if missing_revision_upload:
            st.info(
                f"Reattach `{revision_upload_name}` to run this revision. The app does "
                "not retain uploaded file contents after a run."
            )
        run_clicked = st.button(
            run_label,
            key=f"run_{page_scope}",
            type="primary",
            disabled=(
                (not prompt.strip() and upload is None)
                or upload_error is not None
                or active_controller is not None
                or missing_revision_upload
            ),
        )
        run = run_clicked or bool(resubmit_request and not missing_revision_upload)

    result_slot = st.container()
    if run:
        resubmit_request = (
            consume_prompt_resubmit(page_scope) if resubmit_request else None
        )
        source_entry = resubmit_request.get("source_entry") if resubmit_request else None
        continuation_entry = selected_history or current_comparison_entry(page_scope)
        parent_id, branch_source_id = submission_branch(source_entry, continuation_entry)
        run_prompt = prompt.strip() or (
            "Analyze the mmCIF structure and report atom, chain, residue, and non-water "
            "ligand counts."
            if upload and upload["kind"] == "mmcif"
            else "Summarize the CSV's key numeric metrics and plot the most useful comparison."
        )
        security_message = prompt_security_message(run_prompt)
        if security_message:
            st.warning(security_message)
        else:
            options = {
                "enable_thinking": enable_thinking,
                "enable_caching": enable_caching,
                "_page_scope": page_scope,
                "model_routing": {
                    "Auto": "auto",
                    "Economy": "cheap",
                    "Quality": "strong",
                }[routing_label],
            }
            if enable_thinking:
                options["reasoning_effort"] = reasoning_effort_label.lower()
            if source_entry:
                options["resubmitted_from"] = source_entry["id"]
            agent_prompt = run_prompt
            history = history_entries(page_scope)
            agent_prompts = conversation_prompts(history, parent_id, run_prompt)
            classifier_messages = routing_messages(history, parent_id, run_prompt)
            if len(classifier_messages) > 1:
                options["_model_router_messages"] = classifier_messages
            if selection_context := (
                selection_prompt_context(molstar_selection) if molstar_selection else ""
            ):
                agent_prompt = f"{run_prompt}\n\n{selection_context}"
                if agent_prompts:
                    agent_prompts = {
                        arch_name: f"{arch_prompt}\n\n{selection_context}"
                        for arch_name, arch_prompt in agent_prompts.items()
                    }
            controller = None
            if upload:
                options["uploaded_file"] = upload["name"]
                try:
                    staged = stage_upload(upload)
                    # Both architectures should exercise Code Interpreter
                    # against the exact staged file. Keep each model prompt
                    # small and supply only the opaque download token; copying
                    # the large application summary into a tool call can exceed
                    # one model cycle's output-token limit.
                    staged_agent_prompts = {
                        CUSTOM_AGENT: interpreter_upload_prompt(
                            agent_prompts.get(CUSTOM_AGENT, agent_prompt),
                            staged,
                        ),
                    }
                    if not custom_only:
                        staged_agent_prompts[PROMPT_VIEW_AGENT] = managed_upload_prompt(
                            agent_prompts.get(PROMPT_VIEW_AGENT, agent_prompt),
                            staged,
                        )
                    controller = run_comparison(
                        prompt_with_upload(agent_prompt, staged),
                        options,
                        display_prompt=run_prompt,
                        agent_prompts=staged_agent_prompts,
                        parent_id=parent_id,
                        branch_source_id=branch_source_id,
                        cleanup=lambda: delete_staged_upload(staged),
                        architectures=architectures,
                        page_scope=page_scope,
                    )
                except Exception as exc:
                    logger.exception(
                        "comparison upload staging/start failed file=%s error_type=%s",
                        upload.get("name"),
                        type(exc).__name__,
                    )
                    st.error(
                        "The file could not be staged for analysis. Check the configured "
                        "upload bucket and AWS credentials, then retry. "
                        f"Technical error: {type(exc).__name__}."
                    )
            else:
                controller = run_comparison(
                    agent_prompt,
                    options,
                    display_prompt=run_prompt,
                    agent_prompts=agent_prompts,
                    parent_id=parent_id,
                    branch_source_id=branch_source_id,
                    architectures=architectures,
                    page_scope=page_scope,
                )
            if controller:
                with result_slot:
                    render_live_comparison(controller.run_id)
    elif active_controller is not None:
        with result_slot:
            render_live_comparison(active_controller.run_id)
    elif selected_history:
        with result_slot:
            render_saved_comparison(selected_history)
    elif current_entry := current_comparison_entry(page_scope):
        with result_slot:
            render_current_comparison(current_entry)
            if not custom_only and upload and upload["kind"] == "mmcif":
                render_agent_structure_dashboard(upload, current_entry["states"])


def render_comparison_page() -> None:
    render_agent_page(custom_only=False)


def render_custom_agent_page() -> None:
    render_agent_page(custom_only=True)


def render_docs_page() -> None:
    with st.sidebar:
        selected_doc = render_docs_selector()
    render_docs_view(selected_doc)


def main() -> None:
    st.set_page_config(page_title="AgentCore agents", layout="wide")
    start_router_warmup()
    inject_styles()
    navigation = st.navigation(
        [
            st.Page(render_comparison_page, title="Comparison", icon=":material/compare_arrows:"),
            st.Page(render_custom_agent_page, title="Custom agent", icon=":material/smart_toy:"),
            st.Page(render_docs_page, title="Docs", icon=":material/description:"),
        ]
    )
    navigation.run()


if __name__ == "__main__":
    main()
