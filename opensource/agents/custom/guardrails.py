"""Deterministic safety guardrails for the open-source agent boundary.

These checks intentionally have no model or optional-package dependency. They
therefore run before every LLM request and cannot silently fail open because a
remote classifier or a heavyweight local model is unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    value: Any
    reason: str | None = None
    redactions: tuple[str, ...] = ()


_PROMPT_BLOCK_RULES = (
    (
        "instruction_override",
        re.compile(
            r"(?:^|[\n.!?]\s*)(?:please\s+)?"
            r"(?:ignore|disregard|override|bypass)\b[\s\S]{0,120}"
            r"\b(?:system|developer|previous|safety|guardrails?|policy|instructions?)\b",
            re.IGNORECASE,
        ),
        "The request attempts to override the agent's safety or system instructions.",
    ),
    (
        "prompt_exfiltration",
        re.compile(
            r"\b(?:reveal|show|print|repeat|dump|expose|return)\b[\s\S]{0,100}"
            r"\b(?:system|developer)\s+(?:prompt|message|instructions?)\b",
            re.IGNORECASE,
        ),
        "The request attempts to expose confidential system instructions.",
    ),
    (
        "malware_execution",
        re.compile(
            r"\b(?:build|create|deploy|execute|install|run|write)\b[\s\S]{0,100}"
            r"\b(?:botnet|credential\s+stealer|keylogger|malware|ransomware|reverse\s+shell)\b",
            re.IGNORECASE,
        ),
        "The request asks the agent to create or execute malware.",
    ),
)

_UNSAFE_CODE_RULES = (
    re.compile(r"\b(?:subprocess|socket|requests|httpx|urllib)\b", re.IGNORECASE),
    re.compile(r"\bos\.(?:popen|remove|rmdir|system|unlink)\s*\(", re.IGNORECASE),
    re.compile(r"\bshutil\.rmtree\s*\(", re.IGNORECASE),
    re.compile(r"\b(?:eval|exec)\s*\(", re.IGNORECASE),
    re.compile(
        r"\bopen\s*\(\s*['\"]/(?:etc|home|proc|root|sys)(?:/|['\"])",
        re.IGNORECASE,
    ),
)

_SENSITIVE_PATTERNS = (
    (
        "aws_access_key",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
        "[REDACTED_AWS_ACCESS_KEY]",
    ),
    (
        "api_key",
        re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9])"),
        "[REDACTED_API_KEY]",
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
        "Bearer [REDACTED_TOKEN]",
    ),
    (
        "email",
        re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
        "[REDACTED_EMAIL]",
    ),
    (
        "phone",
        re.compile(r"(?<!\w)\+(?:\d[ .()-]?){7,15}(?!\d)"),
        "[REDACTED_PHONE]",
    ),
)

_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_FORBIDDEN_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _passes_luhn(value: str) -> bool:
    digits = [int(char) for char in value if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def redact_sensitive_text(value: str) -> tuple[str, tuple[str, ...]]:
    """Replace common credentials and PII with stable, non-secret labels."""
    detected: list[str] = []
    redacted = value
    for label, pattern, replacement in _SENSITIVE_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        if count:
            detected.append(label)

    def replace_card(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if not _passes_luhn(candidate):
            return candidate
        detected.append("payment_card")
        return "[REDACTED_PAYMENT_CARD]"

    redacted = _CARD_CANDIDATE.sub(replace_card, redacted)
    return redacted, tuple(dict.fromkeys(detected))


def redact_sensitive_data(value: Any) -> tuple[Any, tuple[str, ...]]:
    """Recursively redact strings in JSON-like prompt, tool, and event data."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        detected: list[str] = []
        for key, item in value.items():
            safe_item, labels = redact_sensitive_data(item)
            output[key] = safe_item
            detected.extend(labels)
        return output, tuple(dict.fromkeys(detected))
    if isinstance(value, list):
        output = []
        detected = []
        for item in value:
            safe_item, labels = redact_sensitive_data(item)
            output.append(safe_item)
            detected.extend(labels)
        return output, tuple(dict.fromkeys(detected))
    if isinstance(value, tuple):
        safe_items, labels = redact_sensitive_data(list(value))
        return tuple(safe_items), labels
    return value, ()


def guard_prompt(prompt: Any, *, max_chars: int) -> GuardrailDecision:
    """Validate, policy-check, and redact one prompt before model invocation."""
    if not isinstance(prompt, str):
        return GuardrailDecision(False, "", "The prompt must be text.")
    if len(prompt) > max_chars:
        return GuardrailDecision(
            False,
            "",
            f"The prompt exceeds the {max_chars}-character safety limit.",
        )
    if _FORBIDDEN_CONTROLS.search(prompt):
        return GuardrailDecision(False, "", "The prompt contains invalid control characters.")
    for _rule_name, pattern, reason in _PROMPT_BLOCK_RULES:
        if pattern.search(prompt):
            return GuardrailDecision(False, "", reason)

    sanitized, redactions = redact_sensitive_text(prompt)
    return GuardrailDecision(True, sanitized, redactions=redactions)


def guard_tool_call(tool_name: str, tool_input: Any) -> GuardrailDecision:
    """Redact every tool input and block unsafe code-interpreter capabilities."""
    sanitized, redactions = redact_sensitive_data(tool_input)
    if tool_name == "code_interpreter" and isinstance(sanitized, dict):
        code = str(sanitized.get("code", ""))
        if any(pattern.search(code) for pattern in _UNSAFE_CODE_RULES):
            return GuardrailDecision(
                False,
                sanitized,
                "Code interpreter guardrail blocked network, process, or destructive access.",
                redactions,
            )
    return GuardrailDecision(True, sanitized, redactions=redactions)
