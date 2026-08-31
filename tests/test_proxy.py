import os
from pathlib import Path

os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-session-secret"

from fastapi.testclient import TestClient

import proxy


def client_for(tmp_path: Path) -> TestClient:
    proxy.DATA_DIR = tmp_path
    proxy.SETTINGS_FILE = tmp_path / "settings.json"
    return TestClient(proxy.app, raise_server_exceptions=False)


def login(client: TestClient) -> None:
    response = client.post("/api/login", json={"password": "test-password"})
    assert response.status_code == 200
    assert response.cookies.get(proxy.SESSION_COOKIE)


def test_web_login_and_protected_settings(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
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
