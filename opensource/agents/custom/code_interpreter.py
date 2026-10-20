"""Open Terminal-backed Python interpreter for the open-source agent.

The model receives a Python-only tool. Open Terminal's broader shell and file
APIs remain private implementation details and are not exposed as agent tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import PurePath
from typing import Any

from strands import tool

OPEN_TERMINAL_URL = os.environ.get("OPEN_TERMINAL_URL", "").rstrip("/")
OPEN_TERMINAL_API_KEY = os.environ.get("OPEN_TERMINAL_API_KEY", "")
CODE_INTERPRETER_TIMEOUT = min(
    max(float(os.environ.get("CODE_INTERPRETER_TIMEOUT", "15")), 1.0), 60.0
)
CODE_INTERPRETER_MAX_CODE_CHARS = int(os.environ.get("CODE_INTERPRETER_MAX_CODE_CHARS", "20000"))
CODE_INTERPRETER_MAX_OUTPUT_CHARS = int(
    os.environ.get("CODE_INTERPRETER_MAX_OUTPUT_CHARS", "12000")
)
MAX_ATTACHMENT_BYTES = int(os.environ.get("CODE_INTERPRETER_MAX_ATTACHMENT_BYTES", "1048576"))
ATTACHMENT_CHUNK_BYTES = 48 * 1024
# Open Terminal echoes the base64 command in its JSON envelope. One attachment
# chunk therefore needs substantially more transport room than the output we
# ultimately expose to the model. The model-visible result remains capped by
# CODE_INTERPRETER_MAX_OUTPUT_CHARS below.
MAX_TERMINAL_RESPONSE_BYTES = max(256 * 1024, CODE_INTERPRETER_MAX_OUTPUT_CHARS * 4)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
SUPPORTED_ATTACHMENT_FORMATS = {
    ".csv": {"kind": "csv", "content_type": "text/csv"},
    ".cif": {"kind": "mmcif", "content_type": "chemical/x-cif"},
    ".mcif": {"kind": "mmcif", "content_type": "chemical/x-cif"},
    ".mmcif": {"kind": "mmcif", "content_type": "chemical/x-cif"},
}


def _request(
    method: str,
    path: str,
    *,
    session_id: str,
    body: dict[str, Any] | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "X-Session-ID": session_id,
    }
    if OPEN_TERMINAL_API_KEY:
        headers["Authorization"] = f"Bearer {OPEN_TERMINAL_API_KEY}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{OPEN_TERMINAL_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=CODE_INTERPRETER_TIMEOUT + 5) as response:
            payload = response.read(MAX_TERMINAL_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Open Terminal returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Open Terminal is unavailable: {exc.reason}") from exc
    if len(payload) > MAX_TERMINAL_RESPONSE_BYTES:
        raise RuntimeError("Open Terminal returned an oversized response envelope.")
    return json.loads(payload or b"null")


def _python_command(code: str) -> str:
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    # Base64 contains no shell metacharacters. The generated source is decoded
    # inside the interpreter container instead of interpolated into the shell.
    return f"python -c \"import base64;exec(base64.b64decode('{encoded}'))\""


def attachment_workspace(session_id: str) -> str:
    """Return an opaque workspace path that cannot expose a client session id."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"/tmp/agent_uploads/{digest}"


def safe_attachment_name(filename: str) -> str:
    """Reduce a browser-supplied filename to one safe basename."""
    basename = PurePath(str(filename).replace("\\", "/")).name
    cleaned = _SAFE_FILENAME.sub("_", basename).strip("._")
    cleaned = cleaned or "upload"
    suffix = PurePath(cleaned).suffix.lower()
    if suffix in SUPPORTED_ATTACHMENT_FORMATS:
        return f"{cleaned[: -len(suffix)][: 120 - len(suffix)]}{suffix}"
    return cleaned[:120]


def decode_attachments(raw_attachments: Any) -> list[dict[str, Any]]:
    """Validate and decode the bounded attachment payload accepted by the API."""
    if raw_attachments in (None, []):
        return []
    if not isinstance(raw_attachments, list) or len(raw_attachments) != 1:
        raise ValueError("Upload exactly one data file at a time.")

    item = raw_attachments[0]
    if not isinstance(item, dict):
        raise ValueError("The attachment payload is invalid.")
    name = safe_attachment_name(item.get("name", "upload"))
    suffix = PurePath(name).suffix.lower()
    attachment_format = SUPPORTED_ATTACHMENT_FORMATS.get(suffix)
    if attachment_format is None:
        raise ValueError("Only .csv, .cif, .mcif, and .mmcif attachments are supported.")
    encoded = item.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("The attachment is empty.")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The attachment is not valid base64 data.") from exc
    if not content:
        raise ValueError("The attachment is empty.")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"The attachment exceeds the {MAX_ATTACHMENT_BYTES // 1024:,} KB upload limit."
        )
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The attachment must be UTF-8 encoded text.") from exc
    return [{"name": name, "content": content, **attachment_format}]


def decode_csv_attachments(raw_attachments: Any) -> list[dict[str, Any]]:
    """Compatibility wrapper for the original CSV-only helper."""
    return decode_attachments(raw_attachments)


def attachment_manifest(
    attachments: list[dict[str, Any]], *, session_id: str
) -> list[dict[str, Any]]:
    """Describe validated attachments without placing their contents in a prompt."""
    workspace = attachment_workspace(session_id)
    return [
        {
            "name": item["name"],
            "path": f"{workspace}/{item['name']}",
            "size": len(item["content"]),
            "kind": item["kind"],
        }
        for item in attachments
    ]


def _execute_setup(code: str, *, session_id: str) -> None:
    """Run a short setup program and require a successful terminal result."""
    query = urllib.parse.urlencode({"wait": CODE_INTERPRETER_TIMEOUT, "tail": 20})
    result = _request(
        "POST",
        f"/execute?{query}",
        session_id=session_id,
        body={"command": _python_command(code)},
    )
    if not isinstance(result, dict) or result.get("status") == "running":
        raise RuntimeError("The code-interpreter workspace did not finish preparing the upload.")
    if result.get("exit_code") not in (None, 0):
        raise RuntimeError("The code-interpreter workspace could not store the uploaded file.")


def stage_attachments(
    attachments: list[dict[str, Any]], *, session_id: str
) -> list[dict[str, Any]]:
    """Write validated uploads into the isolated interpreter filesystem.

    Chunks keep every generated ``python -c`` command below typical OS
    argument-size limits while still supporting useful CSV and mmCIF files.
    """
    if not attachments:
        return []
    manifest = attachment_manifest(attachments, session_id=session_id)
    for item, metadata in zip(attachments, manifest):
        path_literal = repr(metadata["path"])
        content = item["content"]
        for offset in range(0, len(content), ATTACHMENT_CHUNK_BYTES):
            chunk = content[offset : offset + ATTACHMENT_CHUNK_BYTES]
            encoded = base64.b64encode(chunk).decode("ascii")
            mode = "wb" if offset == 0 else "ab"
            setup = (
                "import base64,pathlib;"
                f"p=pathlib.Path({path_literal});p.parent.mkdir(parents=True,exist_ok=True);"
                f"p.open({mode!r}).write(base64.b64decode({encoded!r}))"
            )
            _execute_setup(setup, session_id=session_id)
    return manifest


def execute_python(code: str, *, session_id: str) -> str:
    """Execute Python through Open Terminal and return bounded JSON output."""
    if not OPEN_TERMINAL_URL:
        return "Code interpreter is disabled because OPEN_TERMINAL_URL is not configured."
    if not code.strip():
        return "No Python code supplied."
    if len(code) > CODE_INTERPRETER_MAX_CODE_CHARS:
        return (
            "Python code exceeds the configured limit of "
            f"{CODE_INTERPRETER_MAX_CODE_CHARS} characters."
        )

    query = urllib.parse.urlencode({"wait": CODE_INTERPRETER_TIMEOUT, "tail": 200})
    result = _request(
        "POST",
        f"/execute?{query}",
        session_id=session_id,
        body={"command": _python_command(code)},
    )
    if isinstance(result, dict) and result.get("status") == "running":
        process_id = result.get("id")
        if process_id:
            try:
                _request(
                    "DELETE",
                    f"/execute/{urllib.parse.quote(str(process_id), safe='')}?force=true",
                    session_id=session_id,
                )
            except RuntimeError:
                pass
        result["status"] = "timed_out"
        result["error"] = (
            f"Execution exceeded {CODE_INTERPRETER_TIMEOUT:g} seconds and was terminated."
        )

    rendered = json.dumps(result, ensure_ascii=False, default=str)
    if len(rendered) > CODE_INTERPRETER_MAX_OUTPUT_CHARS:
        rendered = (
            rendered[:CODE_INTERPRETER_MAX_OUTPUT_CHARS] + "...[code interpreter output truncated]"
        )
    return rendered


def build_code_interpreter_tool(session_id: str):
    """Create a Strands tool bound to one conversation/session workspace."""

    @tool(name="code_interpreter")
    def code_interpreter(code: str) -> str:
        """Execute Python for arithmetic, statistics, data transformation, or verification.

        Args:
            code: Complete Python program. Print the values needed for the answer.
        """
        return execute_python(code, session_id=session_id)

    return code_interpreter
