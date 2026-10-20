"""Guardrails for the CI/CD workflow split."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_deployment_workflows_do_not_host_streamlit():
    for path in WORKFLOWS.glob("deploy-*.yml"):
        workflow = path.read_text()
        assert "streamlit" not in workflow.lower()
        assert "apps/comparison" not in workflow


def test_production_deployments_are_manual_and_protected():
    for path in WORKFLOWS.glob("deploy-*.yml"):
        workflow = path.read_text()
        assert "workflow_dispatch:" in workflow
        assert "environment: dev" in workflow
        assert "id-token: write" in workflow


def test_ci_smoke_tests_streamlit_without_deploying_it():
    workflow = (WORKFLOWS / "ci.yml").read_text()
    smoke_test = (ROOT / "infrastructure" / "scripts" / "check_streamlit.py").read_text()
    assert "infrastructure/scripts/check_streamlit.py" in workflow
    assert '"--server.fileWatcherType=none"' in smoke_test


def test_agentcore_evaluation_runs_in_ci_and_after_custom_deployment():
    evaluation_command = "uv run python evaluations/run_agentcore_evaluation.py"
    ci = (WORKFLOWS / "ci.yml").read_text()
    custom_deployment = (WORKFLOWS / "deploy-custom-agent.yml").read_text()

    assert evaluation_command in ci
    assert "github.ref == 'refs/heads/main'" in ci
    assert "EVALUATION_SOURCE_REVISION: ${{ github.sha }}" in ci
    assert evaluation_command in custom_deployment
    assert custom_deployment.index(evaluation_command) > custom_deployment.index(
        "Deploy custom agent"
    )
    assert "AGENTCORE_EVALUATION_EXPECTED_IMAGE_TAG: ${{ github.sha }}" in custom_deployment
