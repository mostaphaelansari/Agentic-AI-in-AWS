"""Sidebar AWS CLI console with a deliberately read-only command surface."""

from __future__ import annotations

import os

import streamlit as st
from config import ARCHITECTURES
from services.aws_cli import AwsCliResult, AwsCliValidationError, run_aws_command

RESULT_KEY = "comparison_aws_cli_result"
COMMAND_KEY = "comparison_aws_cli_command"
PRESET_KEY = "comparison_aws_cli_preset"


def _resource_id(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


def aws_cli_presets() -> dict[str, str]:
    managed = ARCHITECTURES["Managed agent"]
    custom = ARCHITECTURES["Custom agent"]
    custom_log_group = custom.get("usage_log_group", "")
    commands = {
        "Caller identity": f"aws sts get-caller-identity --region {managed['region']}",
        "Harness metadata": (
            "aws bedrock-agentcore-control get-harness "
            f"--harness-id {_resource_id(managed['arn'])} --region {managed['region']}"
        ),
        "Custom Runtime metadata": (
            "aws bedrock-agentcore-control get-agent-runtime "
            f"--agent-runtime-id {_resource_id(custom['arn'])} --region {custom['region']}"
        ),
    }
    if custom_log_group:
        commands["Latest Runtime usage streams"] = (
            "aws logs describe-log-streams "
            f"--log-group-name {custom_log_group} --order-by LastEventTime "
            f"--descending --limit 5 --region {custom['region']}"
        )
    return commands


def _render_result(result: AwsCliResult) -> None:
    if result.ok:
        st.success("Command completed")
    else:
        st.error(f"AWS CLI exited with status {result.returncode}")
    if (payload := result.json_output()) is not None:
        st.json(payload, expanded=False)
    elif result.stdout.strip():
        st.code(result.stdout, language="text", wrap_lines=True)
    if result.stderr.strip():
        st.code(result.stderr, language="text", wrap_lines=True)
    if result.truncated:
        st.caption("Output was truncated at 64 KiB.")


def render_aws_cli_console() -> None:
    """Render a bounded AWS CLI form and its most recent result."""
    if os.environ.get("ENABLE_AWS_CLI_CONSOLE", "true").lower() not in {"1", "true", "yes"}:
        return

    presets = aws_cli_presets()
    with st.expander("AWS read-only console", expanded=False):
        st.caption(
            "Runs with the UI backend's AWS identity. Only read-only AgentCore, "
            "CloudWatch, Logs, and STS commands are accepted."
        )
        preset = st.selectbox("Example", list(presets), key=PRESET_KEY)
        if COMMAND_KEY not in st.session_state:
            st.session_state[COMMAND_KEY] = presets[preset]
        if st.button("Load example", key="load_aws_cli_example", width="stretch"):
            st.session_state[COMMAND_KEY] = presets[preset]
        with st.form("comparison_aws_cli_form"):
            command = st.text_area(
                "Command",
                key=COMMAND_KEY,
                height=116,
                help="Required form: aws <allowed-service> <read-only-operation> [arguments]",
            )
            submitted = st.form_submit_button("Run command", width="stretch")
        if submitted:
            try:
                with st.spinner("Running AWS CLI…"):
                    st.session_state[RESULT_KEY] = run_aws_command(command)
            except (AwsCliValidationError, RuntimeError) as exc:
                st.error(str(exc))
        result = st.session_state.get(RESULT_KEY)
        if isinstance(result, AwsCliResult):
            _render_result(result)
