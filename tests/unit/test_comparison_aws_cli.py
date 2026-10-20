"""Security and rendering guards for the comparison AWS CLI console."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services import aws_cli  # noqa: E402


def test_read_only_aws_command_is_accepted_and_pager_is_disabled() -> None:
    args = aws_cli.validated_aws_command(
        "aws logs filter-log-events --log-group-name usage --filter-pattern session-123"
    )

    assert args[:3] == ["aws", "logs", "filter-log-events"]
    assert args[-1] == "--no-cli-pager"


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'aws sts get-caller-identity'",
        "aws s3api get-object --bucket private --key key /tmp/out",
        "aws logs delete-log-group --log-group-name usage",
        "aws logs filter-log-events --endpoint-url http://127.0.0.1",
        "aws logs filter-log-events --cli-input-json file:///etc/passwd",
        "aws sts get-caller-identity --profile production",
        "aws sts get-session-token",
        "aws sts get-federation-token --name visitor",
        "aws sts get-caller-identity --debug",
    ],
)
def test_unsafe_or_out_of_scope_commands_are_rejected(command: str) -> None:
    with pytest.raises(aws_cli.AwsCliValidationError):
        aws_cli.validated_aws_command(command)


def test_command_runs_without_a_shell_and_parses_json(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps({"value": 42}), "")

    monkeypatch.setattr(aws_cli.shutil, "which", lambda name: "/usr/local/bin/aws")
    monkeypatch.setattr(aws_cli.subprocess, "run", fake_run)

    result = aws_cli.run_aws_command("aws sts get-caller-identity")

    assert result.ok is True
    assert result.json_output() == {"value": 42}
    assert calls[0][0][0] == "/usr/local/bin/aws"
    assert "shell" not in calls[0][1]


def test_sidebar_wires_in_the_console() -> None:
    app = (APP_ROOT / "app.py").read_text()
    component = (APP_ROOT / "components" / "aws_cli.py").read_text()

    assert "render_aws_cli_console()" in app
    assert "AWS read-only console" in component
    assert "Load example" in component
