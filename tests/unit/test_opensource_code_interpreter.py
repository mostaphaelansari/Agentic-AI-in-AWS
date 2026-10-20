from __future__ import annotations

import base64
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "opensource" / "agents" / "custom" / "code_interpreter.py"
SPEC = importlib.util.spec_from_file_location("opensource_code_interpreter", MODULE_PATH)
code_interpreter = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(code_interpreter)


def test_python_command_base64_encodes_untrusted_source():
    source = 'print("hello; $(touch /tmp/nope)")'
    command = code_interpreter._python_command(source)

    encoded = command.split("b64decode('", 1)[1].split("')", 1)[0]
    assert base64.b64decode(encoded).decode() == source
    assert "touch /tmp/nope" not in command


def test_execute_python_passes_session_and_returns_result(monkeypatch):
    captured = {}
    monkeypatch.setattr(code_interpreter, "OPEN_TERMINAL_URL", "http://terminal:8000")

    def fake_request(method, path, *, session_id, body=None):
        captured.update(method=method, path=path, session_id=session_id, body=body)
        return {
            "id": "proc-1",
            "status": "done",
            "exit_code": 0,
            "output": [{"stream": "stdout", "data": "4\n"}],
        }

    monkeypatch.setattr(code_interpreter, "_request", fake_request)
    result = json.loads(code_interpreter.execute_python("print(2 + 2)", session_id="chat-7"))

    assert captured["method"] == "POST"
    assert captured["session_id"] == "chat-7"
    assert result["exit_code"] == 0


def test_execute_python_kills_timed_out_process(monkeypatch):
    calls = []
    monkeypatch.setattr(code_interpreter, "OPEN_TERMINAL_URL", "http://terminal:8000")

    def fake_request(method, path, *, session_id, body=None):
        calls.append((method, path, session_id))
        if method == "POST":
            return {"id": "proc-2", "status": "running", "output": []}
        return {"status": "killed"}

    monkeypatch.setattr(code_interpreter, "_request", fake_request)
    result = json.loads(code_interpreter.execute_python("while True: pass", session_id="chat-8"))

    assert result["status"] == "timed_out"
    assert calls[1][0] == "DELETE"
    assert "/execute/proc-2" in calls[1][1]


def test_disabled_interpreter_does_not_call_service(monkeypatch):
    monkeypatch.setattr(code_interpreter, "OPEN_TERMINAL_URL", "")
    assert "disabled" in code_interpreter.execute_python("print(1)", session_id="chat").lower()


def test_terminal_response_allows_echoed_attachment_chunk(monkeypatch):
    result = {"status": "done", "exit_code": 0, "command": "x" * 70_000, "output": []}
    encoded = json.dumps(result).encode()
    monkeypatch.setattr(code_interpreter, "OPEN_TERMINAL_URL", "http://terminal:8000")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            assert limit == code_interpreter.MAX_TERMINAL_RESPONSE_BYTES + 1
            return encoded

    monkeypatch.setattr(
        code_interpreter.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )

    response = code_interpreter._request(
        "POST",
        "/execute",
        session_id="chat-large-cif",
        body={"command": "ignored"},
    )

    assert response == result


def test_csv_attachment_is_validated_and_filename_is_sanitized():
    encoded = base64.b64encode(b"month,revenue\nJan,10\n").decode("ascii")

    attachments = code_interpreter.decode_csv_attachments(
        [{"name": "../../sales report.csv", "data_base64": encoded}]
    )

    assert attachments == [
        {
            "name": "sales_report.csv",
            "content": b"month,revenue\nJan,10\n",
            "kind": "csv",
            "content_type": "text/csv",
        }
    ]


def test_mmcif_attachment_extensions_are_accepted_and_classified():
    content = b"data_demo\nloop_\n_atom_site.group_PDB\n_atom_site.id\nATOM 1\n"
    encoded = base64.b64encode(content).decode("ascii")

    for extension in ("CIF", "MCIF", "MMCIF"):
        attachments = code_interpreter.decode_attachments(
            [{"name": f"../../protein structure.{extension}", "data_base64": encoded}]
        )

        assert attachments == [
            {
                "name": f"protein_structure.{extension.lower()}",
                "content": content,
                "kind": "mmcif",
                "content_type": "chemical/x-cif",
            }
        ]


def test_attachment_workspace_does_not_expose_session_id():
    workspace = code_interpreter.attachment_workspace("customer-secret-session")

    assert workspace.startswith("/tmp/agent_uploads/")
    assert "customer-secret-session" not in workspace


def test_stage_attachment_uses_bounded_chunks(monkeypatch):
    calls = []
    content = b"x" * (code_interpreter.ATTACHMENT_CHUNK_BYTES + 1)
    monkeypatch.setattr(
        code_interpreter,
        "_execute_setup",
        lambda source, *, session_id: calls.append((source, session_id)),
    )

    manifest = code_interpreter.stage_attachments(
        [{"name": "metrics.csv", "content": content, "kind": "csv"}], session_id="chat-9"
    )

    assert len(calls) == 2
    assert all(session_id == "chat-9" for _, session_id in calls)
    assert manifest[0]["name"] == "metrics.csv"
    assert manifest[0]["size"] == len(content)
    assert manifest[0]["kind"] == "csv"
    assert "chat-9" not in manifest[0]["path"]


def test_open_terminal_uses_an_internal_agent_only_network():
    compose = (ROOT / "opensource" / "compose" / "docker-compose.yml").read_text()
    open_terminal = re.search(r"^  open-terminal:\n(?P<body>.*?)^  agent:\n", compose, re.M | re.S)
    agent = re.search(r"^  agent:\n(?P<body>.*?)^  ui:\n", compose, re.M | re.S)

    assert open_terminal and "      - interpreter" in open_terminal.group("body")
    assert agent and "      - interpreter" in agent.group("body")
    assert re.search(r"^networks:\n  interpreter:\n    internal: true$", compose, re.M)
