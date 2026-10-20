"""Constrained AWS CLI execution for the comparison UI.

This is intentionally not a general shell.  Streamlit runs with AWS credentials,
so exposing arbitrary commands would turn the web UI into remote code execution.
Only read-only operations for the services used by the cost dashboard are allowed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

READ_ONLY_OPERATIONS = {
    "bedrock-agentcore-control": {
        "get-agent-runtime",
        "get-harness",
        "list-agent-runtimes",
        "list-harnesses",
        "list-tags-for-resource",
    },
    "cloudwatch": {
        "describe-alarms",
        "get-dashboard",
        "get-insight-rule-report",
        "get-metric-data",
        "get-metric-statistics",
        "list-dashboards",
        "list-metrics",
        "list-tags-for-resource",
    },
    "logs": {
        "describe-log-groups",
        "describe-log-streams",
        "filter-log-events",
        "get-log-events",
        "get-log-group-fields",
        "get-log-record",
        "list-anomalies",
        "list-log-anomaly-detectors",
        "list-tags-for-resource",
    },
    # Do not allow get-session-token or get-federation-token: both mint credentials.
    "sts": {"get-caller-identity"},
}
BLOCKED_OPTIONS = {
    "--ca-bundle",
    "--cli-input-json",
    "--cli-input-yaml",
    "--debug",
    "--endpoint-url",
    "--generate-cli-skeleton",
    "--no-sign-request",
    "--profile",
}
MAX_OUTPUT_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0


class AwsCliValidationError(ValueError):
    """The submitted command falls outside the console's read-only boundary."""


@dataclass(frozen=True)
class AwsCliResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json_output(self) -> Any | None:
        if not self.stdout.strip():
            return None
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError:
            return None


def _option_name(token: str) -> str:
    return token.split("=", 1)[0]


def validated_aws_command(command: str) -> list[str]:
    """Parse and validate one read-only AWS CLI command."""
    try:
        args = shlex.split(command)
    except ValueError as exc:
        raise AwsCliValidationError(f"Could not parse command: {exc}") from exc

    if len(args) < 3 or args[0] != "aws":
        raise AwsCliValidationError("Command must start with: aws <service> <operation>")

    service, operation = args[1:3]
    if service not in READ_ONLY_OPERATIONS:
        allowed = ", ".join(sorted(READ_ONLY_OPERATIONS))
        raise AwsCliValidationError(f"Service '{service}' is not allowed. Allowed: {allowed}")
    if operation not in READ_ONLY_OPERATIONS[service]:
        raise AwsCliValidationError(f"Operation '{service} {operation}' is not allowed")

    for token in args[3:]:
        if _option_name(token) in BLOCKED_OPTIONS:
            raise AwsCliValidationError(f"Option '{_option_name(token)}' is not allowed")
        if "file://" in token.lower() or "fileb://" in token.lower():
            raise AwsCliValidationError("Local file inputs are not allowed")

    if "--no-cli-pager" not in args:
        args.append("--no-cli-pager")
    return args


def run_aws_command(command: str) -> AwsCliResult:
    """Run a validated AWS CLI command without invoking a shell."""
    args = validated_aws_command(command)
    executable = shutil.which("aws")
    if not executable:
        raise RuntimeError("AWS CLI is not installed in the Streamlit environment")
    args[0] = executable
    timeout = float(os.environ.get("AWS_CLI_CONSOLE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=max(1.0, min(timeout, 60.0)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"AWS CLI command exceeded the {timeout:g}s limit") from exc

    stdout = completed.stdout
    stderr = completed.stderr
    truncated = len(stdout.encode()) + len(stderr.encode()) > MAX_OUTPUT_BYTES
    if truncated:
        remaining = MAX_OUTPUT_BYTES
        stdout_bytes = stdout.encode()[:remaining]
        remaining -= len(stdout_bytes)
        stderr_bytes = stderr.encode()[:remaining]
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
    return AwsCliResult(
        command=command,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
    )
