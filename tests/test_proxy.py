import os
import logging
import json
from pathlib import Path

os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-session-secret"

from fastapi.testclient import TestClient

import proxy


def client_for(tmp_path: Path) -> TestClient:
    proxy.DATA_DIR = tmp_path
    proxy.SETTINGS_FILE = tmp_path / "settings.json"
    proxy.MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    return TestClient(proxy.app, raise_server_exceptions=False)


def login(client: TestClient) -> None:
    response = client.post(
        "/api/login", json={"username": "admin", "password": "test-password"}
    )
    assert response.status_code == 200
    assert response.cookies.get(proxy.SESSION_COOKIE)


def test_web_login_and_protected_settings(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.post(
        "/api/login", json={"username": "admin", "password": "wrong"}
    ).status_code == 401
    login(client)
    assert client.get("/api/settings").status_code == 200


def test_save_and_reload_regex_settings(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = {
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "api_key": "secret",
        "timeout_seconds": 42,
        "clean_patterns": [r"(?is)<think>.*?</think>", r"<tag>"],
    }
    response = client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    loaded = client.get("/api/settings").json()
    assert loaded["clean_patterns"] == settings["clean_patterns"]
    assert proxy.clean_content("A<think>hidden</think>B<tag>", loaded) == "AB"


def test_invalid_regex_returns_json_validation_error(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    response = client.put(
        "/api/settings",
        json={
            "target_api_url": "http://ollama:11434/v1/chat/completions",
            "timeout_seconds": 120,
            "clean_patterns": ["[invalid"],
        },
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


def test_account_change_hashes_password_and_invalidates_session(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    response = client.put(
        "/api/account",
        json={
            "current_password": "test-password",
            "username": "new-admin",
            "new_password": "new-secure-password",
        },
    )
    assert response.status_code == 200, response.text
    saved_text = proxy.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "new-secure-password" not in saved_text
    saved = proxy.load_settings()
    assert saved["admin_username"] == "new-admin"
    assert saved["admin_password_hash"].startswith("scrypt$")
    assert client.get("/api/settings").status_code == 401
    assert client.post(
        "/api/login",
        json={"username": "admin", "password": "test-password"},
    ).status_code == 401
    assert client.post(
        "/api/login",
        json={"username": "new-admin", "password": "new-secure-password"},
    ).status_code == 200


def test_proxy_settings_save_preserves_account_hash(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    assert client.put(
        "/api/account",
        json={
            "current_password": "test-password",
            "username": "owner",
            "new_password": "another-secure-password",
        },
    ).status_code == 200
    assert client.post(
        "/api/login",
        json={"username": "owner", "password": "another-secure-password"},
    ).status_code == 200
    original_hash = proxy.load_settings()["admin_password_hash"]
    response = client.put(
        "/api/settings",
        json={
            "target_api_url": "http://ollama:11434/v1/chat/completions",
            "timeout_seconds": 90,
            "clean_patterns": [],
        },
    )
    assert response.status_code == 200
    assert proxy.load_settings()["admin_password_hash"] == original_hash


def test_save_permission_error_is_json(tmp_path: Path) -> None:
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("file", encoding="utf-8")
    client = client_for(blocked_path)
    login(client)
    response = client.put(
        "/api/settings",
        json={
            "target_api_url": "http://ollama:11434/v1/chat/completions",
            "timeout_seconds": 120,
            "clean_patterns": [],
        },
    )
    assert response.status_code == 500
    assert "数据卷写入权限" in response.json()["detail"]


def test_health_and_proxy_json_validation_are_public(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post(
        "/v1/chat/completions", content="not-json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")


def test_api_tokens_are_hashed_and_protect_proxy(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    created = client.post("/api/tokens", json={"name": "测试客户端"})
    assert created.status_code == 200
    token = created.json()["token"]
    saved = proxy.load_settings()["api_tokens"][0]
    assert token not in proxy.SETTINGS_FILE.read_text(encoding="utf-8")
    assert saved["hash"] == proxy.token_digest(token)
    assert "hash" not in client.get("/api/tokens").text
    assert client.post("/v1/chat/completions", json={}).status_code == 401
    assert client.post("/v1/chat/completions", content="not-json", headers={"Authorization": f"Bearer {token}", "content-type": "application/json"}).status_code == 400


def test_models_url_is_derived_and_can_be_overridden() -> None:
    assert proxy.models_url(
        {"target_api_url": "http://ollama:11434/v1/chat/completions", "models_api_url": ""}
    ) == "http://ollama:11434/v1/models"
    assert proxy.models_url(
        {
            "target_api_url": "http://ollama:11434/v1/chat/completions",
            "models_api_url": "http://custom:9000/catalog",
        }
    ) == "http://custom:9000/catalog"


def test_log_api_reads_tail_and_reports_five_mb_limit(tmp_path: Path) -> None:
    assert proxy.ollama_base_url(
        {"target_api_url": "http://ollama:11434/v1/chat/completions", "ollama_api_url": ""}
    ) == "http://ollama:11434"
    assert proxy.ollama_base_url(
        {"target_api_url": "http://other/v1/chat/completions", "ollama_api_url": "http://ollama:11434/"}
    ) == "http://ollama:11434"
    client = client_for(tmp_path)
    proxy.LOG_FILE = tmp_path / "cleanllm.log"
    proxy.LOG_FILE.write_text("one\ntwo\nthree\n", encoding="utf-8")
    login(client)
    result = client.get("/api/logs?limit=2").json()
    assert result["lines"] == ["two", "three"]
    assert result["max_bytes"] == 5 * 1024 * 1024


def test_ollama_models_and_delete(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": "qwen3:8b", "size": 100, "modified_at": "2026-01-02T03:04:05Z"}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url, None))
            return FakeResponse()

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    result = client.get("/api/ollama/models")
    assert result.status_code == 200
    assert result.json()["data"][0]["id"] == "qwen3:8b"
    deleted = client.request("DELETE", "/api/ollama/models", json={"model": "qwen3:8b"})
    assert deleted.status_code == 200
    assert (
        "DELETE",
        "http://host.docker.internal:11434/api/delete",
        {"name": "qwen3:8b"},
    ) in calls


def test_ollama_archive_export_reads_manifest_and_blobs(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    root = tmp_path / "models"
    manifest = root / "manifests" / "registry.ollama.ai" / "library" / "demo" / "latest"
    blob = root / "blobs" / ("sha256-" + "a" * 64)
    manifest.parent.mkdir(parents=True)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"model-data")
    manifest.write_text(json.dumps({"config": {"digest": "sha256:" + "a" * 64}}), encoding="utf-8")
    original = proxy.OLLAMA_MODELS_DIR
    proxy.OLLAMA_MODELS_DIR = root
    try:
        response = client.get("/api/ollama/archive?model=demo:latest")
    finally:
        proxy.OLLAMA_MODELS_DIR = original
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gzip")
    assert len(response.content) > 50


def test_model_discovery_normalizes_openai_response(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "qwen3", "owned_by": "ollama", "created": 123},
                    {"id": "deepseek-r1"},
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    result = client.get("/api/models")
    assert result.status_code == 200, result.text
    assert [item["id"] for item in result.json()["data"]] == ["deepseek-r1", "qwen3"]


def test_capped_log_handler_never_keeps_an_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "capped.log"
    handler = proxy.CappedFileHandler(path, max_bytes=512)
    test_logger = logging.getLogger("cleanllm-cap-test")
    test_logger.handlers = [handler]
    test_logger.propagate = False
    test_logger.setLevel(logging.INFO)
    for index in range(80):
        test_logger.info("line %s %s", index, "x" * 30)
    handler.close()
    assert path.stat().st_size <= 512
