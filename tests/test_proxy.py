import os
import logging
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-session-secret"

from fastapi.testclient import TestClient

import proxy


def test_application_version_comes_from_version_file() -> None:
    assert proxy.APP_VERSION == proxy.VERSION_FILE.read_text(encoding="utf-8").strip()
    assert proxy.app.version == proxy.APP_VERSION


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
        "default_upstream_name": "本地 Ollama",
        "api_key": "secret",
        "timeout_seconds": 42,
        "clean_patterns": [r"(?is)<think>.*?</think>", r"<tag>"],
    }
    response = client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    loaded = client.get("/api/settings").json()
    assert loaded["default_upstream_name"] == settings["default_upstream_name"]
    assert loaded["clean_patterns"] == settings["clean_patterns"]
    assert proxy.clean_content("A<think>hidden</think>B<tag>", loaded) == "AB"
    assert not list(tmp_path.glob("settings-*.tmp"))


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
    saved = proxy.database_tokens()[0]
    assert not proxy.SETTINGS_FILE.exists() or token not in proxy.SETTINGS_FILE.read_text(encoding="utf-8")
    assert saved["hash"] == proxy.token_digest(token)
    listed = client.get("/api/tokens").json()["data"][0]
    assert "hash" not in listed
    assert listed["status"] == "active"
    assert listed["expires_at"] is None
    assert listed["total_tokens"] == 0
    assert client.post("/v1/chat/completions", json={}).status_code == 401
    assert client.post("/v1/chat/completions", content="not-json", headers={"Authorization": f"Bearer {token}", "content-type": "application/json"}).status_code == 400


def test_expired_api_token_is_listed_but_cannot_authenticate(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    token = client.post("/api/tokens", json={"name": "过期客户端"}).json()["token"]
    saved_token = next(item for item in proxy.database_tokens() if item["hash"] == proxy.token_digest(token))
    with proxy.database_connection() as database:
        database.execute(
            "UPDATE api_tokens SET expires_at = ?, total_tokens = ? WHERE id = ?",
            (int(time.time()) - 1, 2_500_000, saved_token["id"]),
        )

    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["status"] == "expired"
    assert listed["total_tokens"] == 2_500_000
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


def test_api_token_total_tracks_usage_updates(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    client.post("/api/tokens", json={"name": "累计客户端"})
    token_id = proxy.database_tokens()[0]["id"]
    with proxy.database_connection() as database:
        database.execute("UPDATE api_tokens SET total_tokens = 4 WHERE id = ?", (token_id,))
        database.execute(
            """INSERT INTO api_usage
               (id, token_id, token_name, at, path, method, model, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("usage-1", token_id, "累计客户端", int(time.time()), "/v1/responses", "POST", "test", 4),
        )

    proxy.update_usage_record("usage-1", total_tokens=10)

    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["total_tokens"] == 10


def test_clear_usage_logs_preserves_statistics_and_token_total(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    client.post("/api/tokens", json={"name": "统计客户端"})
    token_id = proxy.database_tokens()[0]["id"]
    now = int(time.time())
    with proxy.database_connection() as database:
        database.execute("UPDATE api_tokens SET total_tokens = 1234 WHERE id = ?", (token_id,))
        database.execute(
            """INSERT INTO api_usage
               (id, token_id, token_name, at, path, method, model, input_tokens, output_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("clear-me", token_id, "统计客户端", now, "/v1/responses", "POST", "test", 1000, 234, 1234),
        )

    before = client.get("/api/usage").json()
    assert before["today"] == 1
    assert before["tokens_today"] == 1234
    assert len(before["logs"]) == 1
    assert client.delete("/api/usage/logs").status_code == 200

    after = client.get("/api/usage").json()
    assert after["today"] == before["today"]
    assert after["tokens_today"] == before["tokens_today"]
    assert after["logs"] == []
    assert proxy.database_tokens()[0]["total_tokens"] == 1234


def test_usage_log_clear_requires_admin(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.delete("/api/usage/logs").status_code == 401


def test_usage_rows_older_than_400_days_are_pruned_on_new_request(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    token = client.post("/api/tokens", json={"name": "保留策略客户端"}).json()["token"]
    with proxy.database_connection() as database:
        database.execute(
            """INSERT INTO api_usage
               (id, token_id, token_name, at, path, method, model, total_tokens)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
            ("expired-usage", "旧客户端", int(time.time()) - 401 * 86400, "/v1/responses", "POST", "old", 1),
        )

    response = client.post(
        "/v1/chat/completions",
        content="not-json",
        headers={"Authorization": f"Bearer {token}", "content-type": "application/json"},
    )
    assert response.status_code == 400
    assert all(item["id"] != "expired-usage" for item in proxy.database_usage())


def test_existing_usage_schema_adds_visible_column(tmp_path: Path) -> None:
    database_path = tmp_path / "cleanllm.db"
    with sqlite3.connect(database_path) as database:
        database.execute(
            """CREATE TABLE api_usage (
                id TEXT PRIMARY KEY, token_id TEXT, token_name TEXT NOT NULL,
                at INTEGER NOT NULL, path TEXT NOT NULL, method TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', latency_ms REAL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )"""
        )
    client_for(tmp_path)
    with proxy.database_connection() as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(api_usage)")}
    assert "visible" in columns


def test_api_token_can_expire_and_be_disabled(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    expires_at = int(time.time()) + 3600
    created = client.post("/api/tokens", json={"name": "限时客户端", "expires_at": expires_at, "allowed_models": ["gpt-*"]})
    token_id = created.json()["id"]
    token = created.json()["token"]
    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["expires_at"] == expires_at
    assert listed["status"] == "active"
    assert listed["allowed_models"] == ["gpt-*"]
    denied_model = client.post(
        "/v1/chat/completions",
        json={"model": "claude-test", "messages": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied_model.status_code == 403

    changed_expiration = expires_at + 3600
    updated = client.patch(
        f"/api/tokens/{token_id}/policy",
        json={"expires_at": changed_expiration, "allowed_models": ["claude-*", "embedding-model"]},
    )
    assert updated.status_code == 200
    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["expires_at"] == changed_expiration
    assert listed["allowed_models"] == ["claude-*", "embedding-model"]

    response = client.patch(f"/api/tokens/{token_id}/status", json={"enabled": False})
    assert response.status_code == 200
    assert client.get("/api/tokens").json()["data"][0]["status"] == "disabled"
    denied = client.post(
        "/v1/chat/completions",
        json={"model": "test", "messages": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 401
    assert client.post("/api/tokens", json={"name": "无效", "expires_at": int(time.time()) - 1}).status_code == 422


def test_api_token_filters_public_model_list(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    token = client.post(
        "/api/tokens", json={"name": "模型白名单", "allowed_models": ["gpt-*", "embedding-model"]}
    ).json()["token"]

    async def fake_models(refresh=False, _=None):
        return {"data": [
            {"id": "gpt-test", "created": 0, "owned_by": "upstream"},
            {"id": "embedding-model", "created": 0, "owned_by": "upstream"},
            {"id": "claude-test", "created": 0, "owned_by": "upstream"},
        ]}

    monkeypatch.setattr(proxy, "get_models", fake_models)
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["gpt-test", "embedding-model"]


def test_native_responses_stream_preserves_split_completed_event(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    chunks = [
        b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n',
        b'event: response.completed\ndata: {"type":"response.com',
        b'pleted","response":{"id":"resp_test","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
    ]

    class FakeResponse:
        status_code = 200
        is_closed = False

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk
            self.is_closed = True

        async def aclose(self):
            self.is_closed = True

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/responses", json={"model": "test", "input": "hello", "stream": True})
    assert response.status_code == 200
    assert response.text.count("event: response.completed") == 1
    assert '"id":"resp_test"' in response.text


def test_native_responses_stream_reports_failure_when_upstream_omits_terminal_event(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)

    class FakeResponse:
        status_code = 200
        is_closed = False

        async def aiter_bytes(self):
            yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"answer"}\n\n'
            self.is_closed = True

        async def aclose(self):
            self.is_closed = True

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/responses", json={"model": "test", "input": "hello", "stream": True})
    assert response.status_code == 200
    assert "event: response.completed" not in response.text
    assert response.text.count("event: response.failed") == 1


def test_stream_cleaning_preserves_delta_whitespace() -> None:
    line = 'data: {"choices":[{"delta":{"content":" hello"}}]}'
    cleaned = proxy.clean_stream_line(line, {"clean_patterns": []})
    assert json.loads(cleaned[6:])["choices"][0]["delta"]["content"] == " hello"


def test_settings_reject_invalid_upstream_shapes() -> None:
    base = {"target_api_url": "http://primary/v1"}
    for upstream in (
        {"name": "broken", "url": "not-a-url"},
        {"name": "broken", "url": "http://backup/v1", "timeout": "bad"},
    ):
        try:
            proxy.SettingsUpdate.model_validate({**base, "upstreams": [upstream]})
        except ValueError:
            pass
        else:
            raise AssertionError("invalid upstream must be rejected")


def test_settings_reject_duplicate_upstream_names_and_external_background() -> None:
    invalid_values = (
        {"target_api_url": "http://primary/v1", "default_upstream_name": "same", "upstreams": [{"name": "same", "url": "http://backup/v1"}]},
        {"target_api_url": "http://primary/v1", "appearance_background": "javascript:alert(1)"},
    )
    for value in invalid_values:
        try:
            proxy.SettingsUpdate.model_validate(value)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe settings must be rejected")


def test_usage_token_fields_are_normalized() -> None:
    assert proxy.usage_values({"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}) == {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
    }
    assert proxy.usage_values({"input_tokens": 8, "output_tokens": 3}) == {
        "input_tokens": 8,
        "output_tokens": 3,
        "total_tokens": 11,
    }


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
    assert proxy.chat_url_for_target("http://ollama:11434") == "http://ollama:11434/v1/chat/completions"
    assert proxy.chat_url_for_target("http://ollama:11434/v1") == "http://ollama:11434/v1/chat/completions"
    assert proxy.endpoint_url_for_target("https://api.example.com/v1/chat/completions", "embeddings") == "https://api.example.com/v1/embeddings"
    assert proxy.endpoint_url_for_target("https://api.example.com/v1", "audio/speech") == "https://api.example.com/v1/audio/speech"


def test_connectivity_metrics_include_natural_day_counts() -> None:
    checked_at = datetime(2026, 9, 3, 12, 0).astimezone().timestamp()
    today = datetime.fromtimestamp(checked_at).astimezone().replace(hour=8).timestamp()
    yesterday = datetime.fromtimestamp(checked_at).astimezone().replace(day=2, hour=23).timestamp()
    history = [
        {"ts": yesterday, "results": [{"name": "上游 A", "ok": True}]},
        {"ts": today, "results": [{"name": "上游 A", "ok": False}]},
        {"ts": checked_at, "results": [{"name": "上游 A", "ok": True}]},
    ]
    metrics = proxy.connectivity_metrics(history, "上游 A", checked_at)
    assert metrics["checks_today"] == 2
    assert metrics["failures_today"] == 1
    assert metrics["availability_7d"] == 66.7


def test_runtime_history_uses_sqlite_and_is_removed_from_settings(tmp_path: Path) -> None:
    client_for(tmp_path)
    proxy.SETTINGS_FILE.write_text(
        json.dumps({
            "target_api_url": "http://upstream/v1/chat/completions",
            "api_tokens": [{"id": "old"}],
            "api_usage": [{"id": "old"}],
            "connectivity_history": [{"ts": 1, "results": []}],
            "export_history": [{"id": "old"}],
        }),
        encoding="utf-8",
    )

    loaded = proxy.load_settings()
    persisted = json.loads(proxy.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert loaded["target_api_url"] == "http://upstream/v1/chat/completions"
    assert not proxy.RUNTIME_SETTINGS_KEYS.intersection(persisted)

    proxy.database_add_connectivity(10.0, [{"name": "上游", "ok": True}])
    proxy.database_add_export({"id": "export-1", "model": "test", "filename": "test.tar.gz", "created_at": 10, "size": 42})
    assert proxy.database_connectivity_history()[-1]["results"][0]["ok"] is True
    assert proxy.database_export_history()[0]["size"] == 42


def test_embedded_background_is_moved_out_of_settings(tmp_path: Path) -> None:
    client_for(tmp_path)
    image = "data:image/png;base64,iVBORw0KGgo="
    settings = proxy.load_settings()
    settings["appearance_background"] = image
    settings["appearance_backgrounds"] = [image]

    proxy.save_settings(settings)

    persisted = json.loads(proxy.SETTINGS_FILE.read_text(encoding="utf-8"))
    selected = persisted["appearance_background"]
    assert selected.startswith("/api/appearance/background/")
    assert image not in proxy.SETTINGS_FILE.read_text(encoding="utf-8")
    assert (tmp_path / "backgrounds" / Path(selected).name).read_bytes() == b"\x89PNG\r\n\x1a\n"


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


def test_model_discovery_aggregates_upstreams(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = client.get("/api/settings").json()
    settings["upstreams"] = [{
        "name": "备用",
        "url": "http://backup:8000/v1/chat/completions",
        "models_url": "http://backup:8000/v1/models",
    }]
    assert client.put("/api/settings", json=settings).status_code == 200

    class FakeResponse:
        def __init__(self, url): self.url = url
        def raise_for_status(self): return None
        def json(self):
            return {"data": [{"id": "shared" if "backup" in self.url else "primary"}]}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs): return FakeResponse(url)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    result = client.get("/api/models?refresh=true")
    assert result.status_code == 200, result.text
    assert [(item["id"], item["upstream"]) for item in result.json()["data"]] == [
        ("primary", "默认上游"), ("shared", "备用")
    ]


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
