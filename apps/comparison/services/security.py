"""Prompt-safety checks for the comparison demo."""

from __future__ import annotations

import re

PROMPT_DISCLOSURE_MESSAGE = (
    "I can't forward requests to reveal hidden instructions, system prompts, "
    "developer messages, internal policies, or tool configuration. Ask about the "
    "agents' public behavior or capabilities instead."
)

_SENSITIVE_PROMPT_PATTERNS = (
    re.compile(
        r"\b(system|developer|hidden|internal)\s+"
        r"(prompt|instruction|instructions|message|messages|policy|policies|rules?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(reveal|show|print|dump|repeat|disclose|share|expose|output)\b"
        r".{0,80}\b(prompt|instructions?|messages?|polic(?:y|ies)|rules?|configuration)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bignore\b.{0,80}\b(previous|above|prior|all)\b"
        r".{0,80}\b(instructions?|rules?|prompt|policy)\b",
        re.IGNORECASE,
    ),
)


def prompt_security_message(prompt: str) -> str | None:
    """Return a block message when the prompt asks for hidden instructions."""
    compact = " ".join(prompt.split())
    if any(pattern.search(compact) for pattern in _SENSITIVE_PROMPT_PATTERNS):
        return PROMPT_DISCLOSURE_MESSAGE
    return None
