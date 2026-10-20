"""Local username/password auth for the open-source UI.

Users live in a plain JSON file ({username: password_hash}); passwords are
stored as salted PBKDF2-SHA256 — stdlib only, no external identity provider.

Manage the user file from the repo root:

    uv run python opensource/ui/auth.py add alice
    uv run python opensource/ui/auth.py remove alice
    uv run python opensource/ui/auth.py list
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

DEFAULT_USERS_FILE = Path(__file__).parent / "auth" / "users.json"
_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def users_file() -> Path:
    return Path(os.environ.get("UI_USERS_FILE") or DEFAULT_USERS_FILE)


def load_users(path: Path | None = None) -> dict[str, str]:
    target = path or users_file()
    try:
        with open(target) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def authenticate(username: str, password: str, path: Path | None = None) -> bool:
    stored = load_users(path).get(username)
    return bool(stored) and verify_password(password, stored)


def auth_enabled(path: Path | None = None) -> bool:
    return bool(load_users(path))


def _save_users(users: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(users, f, indent=2)
        f.write("\n")


def main() -> None:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["add", "remove", "list"])
    parser.add_argument("username", nargs="?")
    parser.add_argument(
        "--file", type=Path, default=None, help=f"users file (default {DEFAULT_USERS_FILE})"
    )
    args = parser.parse_args()

    path = args.file or users_file()
    users = load_users(path)

    if args.action == "list":
        print("\n".join(users) if users else f"no users in {path}")
        return
    if not args.username:
        parser.error(f"{args.action} requires a username")
    if args.action == "add":
        password = getpass.getpass(f"Password for {args.username}: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("passwords do not match")
        users[args.username] = hash_password(password)
        _save_users(users, path)
        print(f"user {args.username!r} saved to {path}")
    else:
        if users.pop(args.username, None) is None:
            raise SystemExit(f"no such user {args.username!r} in {path}")
        _save_users(users, path)
        print(f"user {args.username!r} removed from {path}")


if __name__ == "__main__":
    main()
