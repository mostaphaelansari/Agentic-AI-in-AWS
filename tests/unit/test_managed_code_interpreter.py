"""Guard the managed Harness Code Interpreter configuration."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_managed_harness_declares_code_interpreter_and_permissions() -> None:
    program = (ROOT / "infrastructure" / "pulumi" / "managed_agent" / "__main__.py").read_text()

    assert '"type": "agentcore_code_interpreter"' in program
    assert '"agent_core_code_interpreter"' in program
    assert '"name": "code_interpreter"' in program
    assert "CodeInterpreterCustom" in program
    assert 'name=f"{harness_name}_public"' in program
    assert 'network_configuration={"network_mode": "PUBLIC"}' in program
    assert '"code_interpreter_arn": code_interpreter.code_interpreter_arn' in program
    assert "bedrock-agentcore:StartCodeInterpreterSession" in program
    assert "bedrock-agentcore:InvokeCodeInterpreter" in program
    assert "bedrock-agentcore:GetCodeInterpreterSession" in program
    assert "bedrock-agentcore:StopCodeInterpreterSession" in program
    assert '"code-interpreter-custom/*"' in program


def test_code_interpreter_procedure_lives_only_in_its_skill() -> None:
    prompt = (ROOT / "agents" / "managed" / "system-prompt.md").read_text()
    skill = (
        ROOT / "opensource" / "agents" / "custom" / "skills" / "code-interpreter" / "SKILL.md"
    ).read_text()

    assert "code_interpreter" not in prompt
    assert "Call `code_interpreter`" in skill
