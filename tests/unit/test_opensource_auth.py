from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "opensource" / "agents" / "custom"))
import service  # noqa: E402


def _load_auth_module():
    path = ROOT / "opensource" / "ui" / "auth.py"
    spec = importlib.util.spec_from_file_location("opensource_ui_auth", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_invocations_requires_api_key_when_set(monkeypatch):
    monkeypatch.setattr(service, "API_KEY", "secret-key")
    client = TestClient(service.app)

    assert client.post("/invocations", json={"prompt": "hi"}).status_code == 401
    # /ping stays open for container health checks.
    assert client.get("/ping").status_code == 200

    # Correct key passes auth (empty prompt short-circuits before the model).
    ok = client.post("/invocations", json={}, headers={"X-API-Key": "secret-key"})
    assert ok.status_code == 200
    bearer = client.post("/invocations", json={}, headers={"Authorization": "Bearer secret-key"})
    assert bearer.status_code == 200


def test_invocations_open_when_no_api_key(monkeypatch):
    monkeypatch.setattr(service, "API_KEY", "")
    client = TestClient(service.app)
    assert client.post("/invocations", json={}).status_code == 200


def test_password_hash_roundtrip_and_users_file(tmp_path):
    auth = _load_auth_module()

    stored = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("hunter2", "garbage")

    users_file = tmp_path / "users.json"
    assert not auth.auth_enabled(users_file)
    auth._save_users({"alice": stored}, users_file)
    assert auth.auth_enabled(users_file)
    assert auth.authenticate("alice", "hunter2", users_file)
    assert not auth.authenticate("alice", "wrong", users_file)
    assert not auth.authenticate("bob", "hunter2", users_file)
