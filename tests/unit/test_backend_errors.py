"""Tests for AWS credential preflight and public error messages."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import TokenRetrievalError

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from backends.common import ensure_aws_credentials
from backends.errors import (
    AWS_AUTH_MESSAGE,
    MAX_ITERATIONS_MESSAGE,
    MAX_TOKENS_MESSAGE,
    AWSAuthenticationError,
    public_error_message,
)


class _ExpiredCredentials:
    def get_frozen_credentials(self):
        raise TokenRetrievalError(provider="sso", error_msg="Token has expired")


class _Session:
    def get_credentials(self):
        return _ExpiredCredentials()


class BackendErrorTests(unittest.TestCase):
    def test_expired_sso_token_has_actionable_message(self):
        error = TokenRetrievalError(provider="sso", error_msg="Token has expired")
        self.assertEqual(public_error_message(error), AWS_AUTH_MESSAGE)

    def test_preflight_wraps_expired_sso_token(self):
        with patch("backends.common.boto3.Session", return_value=_Session()):
            with self.assertRaises(AWSAuthenticationError):
                ensure_aws_credentials()

    def test_agent_limits_have_actionable_messages(self):
        self.assertEqual(
            public_error_message(RuntimeError("stopReason=max_tokens")),
            MAX_TOKENS_MESSAGE,
        )
        self.assertEqual(
            public_error_message(RuntimeError("Maximum max iterations reached")),
            MAX_ITERATIONS_MESSAGE,
        )


if __name__ == "__main__":
    unittest.main()
