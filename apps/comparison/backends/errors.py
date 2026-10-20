"""User-safe classification of AWS backend failures."""

from __future__ import annotations

from botocore.exceptions import (
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

AUTH_EXCEPTIONS = (
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

AWS_AUTH_MESSAGE = (
    "AWS credentials are unavailable or expired. Run `aws sso login`, then retry the comparison."
)
MAX_TOKENS_MESSAGE = (
    "The agent response reached its output-token limit. Retry with a narrower request or fewer "
    "requested details."
)
MAX_ITERATIONS_MESSAGE = (
    "The agent reached its tool/reasoning iteration limit. Retry with fewer requested tasks."
)


class AWSAuthenticationError(RuntimeError):
    """Raised when AWS credentials cannot be loaded or refreshed."""


def is_authentication_error(exc: Exception) -> bool:
    if isinstance(exc, AUTH_EXCEPTIONS):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "token has expired",
            "sso token",
            "unable to locate credentials",
            "credentials were refreshed",
        )
    )


def public_error_message(exc: Exception) -> str:
    if isinstance(exc, AWSAuthenticationError) or is_authentication_error(exc):
        return AWS_AUTH_MESSAGE
    message = str(exc).lower()
    if "max_tokens" in message or "max tokens" in message or "token limit" in message:
        return MAX_TOKENS_MESSAGE
    if "max_iterations" in message or "max iterations" in message:
        return MAX_ITERATIONS_MESSAGE
    return "The AgentCore request failed. Check the application logs for details."
