import os
import logging
import json
import sqlite3
import time
import io
import zipfile
import asyncio
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["SESSION_SECRET"] = "test-session-secret"

from fastapi.testclient import TestClient

import proxy


def test_application_version_comes_from_version_file() -> None:
    assert proxy.APP_VERSION == proxy.VERSION_FILE.read_text(encoding="utf-8").strip()
    assert proxy.app.version == proxy.APP_VERSION


def test_thinking_controls_are_model_scoped_in_admin_ui() -> None:
    features = (proxy.STATIC_DIR / "features.js").read_text(encoding="utf-8")
    app_script = (proxy.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    index = (proxy.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="ollama-disable-thinking"' not in features
    assert "ollama-thinking-select" not in features
    assert "model-thinking-select" in features
    assert "承上游" in app_script
    assert "ollama_disable_thinking" not in proxy.DEFAULT_SETTINGS
    assert "ollama_model_thinking" not in proxy.DEFAULT_SETTINGS
    editor_markup = features[features.index("editor.innerHTML="):features.index("const keyInput=", features.index("editor.innerHTML="))]
    assert editor_markup.index('id="upstream-tab-thinking"') < editor_markup.index('id="upstream-tab-timeout"')
    assert editor_markup.index('id="upstream-tab-thinking"') < editor_markup.index('<details class="wide">')
    assert 'id="upstream-tab-key-reveal"' in editor_markup
    assert "proxy-settings-actions" in features
    assert index.index('data-page="dashboard"') < index.index('data-page="chat"') < index.index('<p class="nav-label">代理管理</p>')
    assert 'chat:["工作台","对话测试"' in app_script


def test_admin_shell_restores_hash_before_deferred_scripts() -> None:
    index = (proxy.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    features = (proxy.STATIC_DIR / "features.js").read_text(encoding="utf-8")
    chat = (proxy.STATIC_DIR / "chat.js").read_text(encoding="utf-8")

    assert "root.dataset.initialPage" in index
    assert 'id="initial-page-placeholder"' in index
    for page in ("analytics", "api-tokens", "diagnostics", "changelog"):
        assert f'data-page="{page}"' in index
    assert index.index('data-page="diagnostics"') < index.index('data-page="security"')
    assert index.index('data-page="security"') < index.index('data-page="logs"')
    assert 'setAttribute("aria-busy", "false")' in features
    assert features.rfind('classList.add("js-ready")') > features.find('data-view="analytics"')
    assert "window.cleanllmModelsReady = loadModels()" in features
    assert "window.cleanllmModelsReady" in chat


def test_admin_history_panels_and_chat_toolbar_layout() -> None:
    index = (proxy.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_script = (proxy.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    features = (proxy.STATIC_DIR / "features.js").read_text(encoding="utf-8")
    chat = (proxy.STATIC_DIR / "chat.js").read_text(encoding="utf-8")
    overrides = (proxy.STATIC_DIR / "overrides.css").read_text(encoding="utf-8")

    assert "const TRACE_PAGE_SIZE=20" in features
    assert 'id="collapse-traces"' in features
    assert "trace-table-wrap" in features
    assert "const SNAPSHOT_PAGE_SIZE=6" in features
    assert "settingsPanel.append($(\"#snapshot-panel\"))" in features
    assert 'id="snapshot-more"' in features
    assert 'id="snapshot-less"' in features
    chat_toolbar = index[index.index('<div class="chat-toolbar panel-body">'):index.index('<div id="chat-messages"')]
    assert chat_toolbar.index('id="chat-model"') < chat_toolbar.index('id="chat-stream"')
    assert "chat-model-stack" in chat_toolbar
    assert ".chat-system-field textarea" in overrides
    assert "min-height: 90px" in overrides
    assert app_script.index('id="ollama-model-name"') < app_script.index('id="background-pull"') < app_script.index('id="ollama-tasks"') < app_script.index('id="ollama-models"')
    assert 'pullRow.insertAdjacentHTML("beforeend", \'<button id="check-ollama-updates"' in app_script
    assert "后台拉取当前模型" not in features
    assert "scheduleTaskRefresh" in features
    assert "event.target.closest(\".custom-select-menu\")" in features
    assert "copyTextReliably" in features
    assert "diagnosticsView.insertBefore(alertPanel,tracePanel)" in features
    assert "legacyCopyContentEditable" in features
    assert 'button.addEventListener("pointerup"' in features
    assert 'button.addEventListener("touchend"' in features
    assert 'if(!ok)throw new Error("copy failed")' in features
    assert '<span>备忘录</span>' in features
    assert '<h3>备忘录</h3>' in chat
    assert ':root[data-theme="light"]' in overrides
    assert "--bg: #eaf1f5" in overrides
    assert ':root[data-theme="light"] .button:not(.primary)' in overrides
    assert ':root[data-theme="light"] .button.primary' in overrides
    assert "-webkit-text-fill-color: #fff" in overrides
    assert "touch-action: pan-y" in overrides
    assert "-webkit-overflow-scrolling: touch" in overrides


def test_global_search_and_ollama_updates_are_wired_in_admin_ui() -> None:
    index = (proxy.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_script = (proxy.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    features = (proxy.STATIC_DIR / "features.js").read_text(encoding="utf-8")
    overrides = (proxy.STATIC_DIR / "overrides.css").read_text(encoding="utf-8")

    assert 'id="check-ollama-updates"' in app_script
    assert 'api("/api/ollama/models/updates")' in app_script
    assert "dataset.ollamaUpdateHeading" in app_script
    assert "row.lastElementChild?.before(cell)" in app_script
    assert "window.cleanllmRefreshOllamaTasks" in features
    assert "grid-template-columns: minmax(220px, 1fr) repeat(3, auto)" in overrides
    assert "if(!size)return '未声明'" in app_script
    assert 'model.context_length?`<span class="model-capability">' in app_script
    assert ".model-table td:nth-child(5)" in overrides
    assert "min-width: 130px" in overrides
    assert 'id="i-sidebar-collapse"' in index
    assert 'id="i-sidebar-expand"' in index
    assert 'id="menu-icon" href="#i-sidebar-collapse"' in index
    assert 'id="i-sidebar-collapse"' in features
    assert 'id="i-sidebar-expand"' in features
    assert 'menuIcon?.setAttribute("href",mobile?"#i-menu":collapsed?"#i-sidebar-expand":"#i-sidebar-collapse")' in features
    assert ".topbar .service-pill > span" in overrides
    assert "flex-basis: 34px" in overrides
    assert 'html body .page [data-search-hidden="1"]' in overrides

    assert "cleanllmSearchableText" in app_script
    assert '"tbody tr"' in app_script
    assert '".diagnostic-item"' in app_script
    assert '".release-note"' in app_script
    assert '".snapshot-list > article"' in app_script
    assert '".ollama-task-item"' in app_script
    assert ".observe(cleanllmContent, {childList: true, subtree: true})" in app_script
    assert 'input.placeholder = `搜索${meta[1]}`' in app_script
    assert '"Ctrl K"' in app_script


def client_for(tmp_path: Path) -> TestClient:
    proxy.DATA_DIR = tmp_path
    proxy.SETTINGS_FILE = tmp_path / "settings.json"
    proxy.MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    proxy.ROUTE_AFFINITY.clear()
    proxy.UPSTREAM_CIRCUITS.clear()
    proxy.NO_ROUTE_CACHE.clear()
    proxy.LOGIN_FAILURES.clear()
    return TestClient(proxy.app, raise_server_exceptions=False)


def configure_non_ollama_upstream() -> None:
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "https://provider.example/v1/chat/completions",
        "default_upstream_name": "provider",
    })
    proxy.save_settings(settings)


def test_virtual_model_resolves_target_and_prioritizes_upstreams() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
        "virtual_models": [{"alias": "coding-best", "target": "gpt-real", "upstreams": ["backup"]}],
    }).model_dump(mode="json")
    assert proxy.resolved_model(settings, "coding-best") == "gpt-real"
    assert [item["name"] for item in proxy.route_upstreams(settings, "coding-best")][:2] == ["backup", "primary"]


def test_unknown_model_prefers_non_ollama_when_discovery_is_partial() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "https://pipio.example/v1",
        "default_upstream_name": "pipio",
        "upstreams": [{"name": "Ollama", "url": "http://127.0.0.1:11434/v1"}],
    }).model_dump(mode="json")
    proxy.MODEL_CACHE.update({"data": [{"id": "local-model", "upstream": "Ollama"}], "at": time.time(), "source": ""})
    assert [item["name"] for item in proxy.route_upstreams(settings, "gpt-5.6-sol")][:2] == ["pipio", "Ollama"]
    assert proxy.model_metadata({}, "local-model", ollama=True)["interfaces"] == ["v1/chat/completions"]
    proxy.MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})


def test_native_responses_route_skips_ollama_but_chat_fallback_keeps_it() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "https://pipio.example/v1",
        "default_upstream_name": "pipio",
        "upstreams": [{"name": "Ollama", "url": "http://127.0.0.1:11434/v1"}],
    }).model_dump(mode="json")
    request = SimpleNamespace(
        state=SimpleNamespace(api_token=None, usage_id="request-responses"),
        url=SimpleNamespace(path="/v1/responses"),
    )
    native = proxy.route_upstreams(
        settings,
        "test-model",
        request,
        capability_path="/v1/responses",
        exclude_ollama_responses=True,
    )
    fallback = proxy.route_upstreams(
        settings,
        "test-model",
        request,
        capability_path="/v1/chat/completions",
    )
    assert [item["name"] for item in native] == ["pipio"]
    assert [item["name"] for item in fallback] == ["pipio", "Ollama"]


def test_responses_route_cache_does_not_hide_ollama_chat_fallback() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://127.0.0.1:11434/v1",
        "default_upstream_name": "Ollama",
    }).model_dump(mode="json")
    request = SimpleNamespace(
        state=SimpleNamespace(api_token=None, usage_id="request-ollama"),
        url=SimpleNamespace(path="/v1/responses"),
    )
    native = proxy.route_upstreams(
        settings,
        "test-model",
        request,
        capability_path="/v1/responses",
        exclude_ollama_responses=True,
    )
    fallback = proxy.route_upstreams(
        settings,
        "test-model",
        request,
        capability_path="/v1/chat/completions",
    )
    assert native == []
    assert [item["name"] for item in fallback] == ["Ollama"]


def test_discovered_ollama_only_model_skips_other_upstreams() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "https://pipio.example/v1",
        "default_upstream_name": "pipio",
        "upstreams": [{"name": "Ollama", "url": "http://127.0.0.1:11434/v1"}],
    }).model_dump(mode="json")
    proxy.MODEL_CACHE.update({
        "data": [{"id": "local-model", "upstream": "Ollama", "interfaces": ["v1/chat/completions"]}],
        "at": time.time(), "source": "",
    })
    request = SimpleNamespace(state=SimpleNamespace(api_token=None, usage_id="route-test"), url=SimpleNamespace(path="/v1/responses"))
    candidates = proxy.route_upstreams(settings, "local-model", request, capability_path="/v1/chat/completions")
    assert [item["name"] for item in candidates] == ["Ollama"]
    proxy.MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})


def test_non_stream_responses_uses_ollama_chat_compatibility_without_responses_probe(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "model": "test",
                "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/responses", json={"model": "test", "input": "hello"})
    assert response.status_code == 200
    assert calls and calls[0][0].endswith("/v1/chat/completions")
    assert "/responses" not in calls[0][0]
    assert "think" not in calls[0][1]


def test_ollama_model_thinking_policy_is_applied_only_to_ollama_requests() -> None:
    ollama = {"name": "Ollama", "url": "http://ollama:11434/v1/chat/completions"}
    proxied_ollama = {
        "url": "https://models.example/v1/chat/completions",
        "ollama_url": "https://models.example/ollama-api",
    }
    provider = {"name": "provider", "url": "https://provider.example/v1/chat/completions"}
    payload = {"model": "qwen3", "messages": []}
    settings = {"model_thinking_policies": [{"upstream": "Ollama", "model": "qwen3", "mode": "disabled"}]}
    assert proxy.ollama_request_payload(payload, ollama, settings)["think"] is False
    assert "think" not in proxy.ollama_request_payload(payload, provider, settings)
    assert "think" not in proxy.ollama_request_payload(payload, ollama, {})
    assert proxy.upstream_looks_ollama(proxied_ollama) is True


def test_ollama_model_thinking_policy_controls_native_request() -> None:
    ollama = {"name": "Ollama", "url": "http://ollama:11434/v1/chat/completions"}
    qwen = {"model": "qwen3:8b", "messages": []}
    deepseek = {"model": "deepseek-r1:8b", "messages": []}
    settings = {
        "model_thinking_policies": [
            {"upstream": "Ollama", "model": "qwen3:8b", "mode": "enabled"},
        ],
    }
    assert proxy.ollama_request_payload(qwen, ollama, settings)["think"] is True
    assert "think" not in proxy.ollama_request_payload(deepseek, ollama, settings)
    settings = {
        "model_thinking_policies": [
            {"upstream": "Ollama", "model": "qwen3:8b", "mode": "disabled"},
        ],
    }
    assert proxy.ollama_request_payload(qwen, ollama, settings)["think"] is False
    assert "think" not in proxy.ollama_request_payload(deepseek, ollama, settings)

    settings["model_thinking_policies"] = [
        {"upstream": "Ollama", "model": "gpt-oss:20b", "mode": "low"},
    ]
    gpt_oss = {"model": "gpt-oss:20b", "messages": []}
    assert proxy.ollama_request_payload(gpt_oss, ollama, settings)["think"] == "low"
    url, native_payload, native = proxy.prepare_chat_upstream_request(ollama, gpt_oss, settings)
    assert (url, native, native_payload["think"]) == ("http://ollama:11434/api/chat", True, "low")


def test_local_openai_thinking_policy_merges_template_arguments() -> None:
    upstream = {
        "name": "LM Studio",
        "url": "http://lmstudio:1234/v1/chat/completions",
        "thinking_protocol": "local_openai",
    }
    settings = {
        "model_thinking_policies": [
            {"upstream": "LM Studio", "model": "qwen3-8b", "mode": "disabled"},
        ],
    }
    payload = {
        "model": "qwen3-8b",
        "messages": [],
        "chat_template_kwargs": {"custom_flag": "kept"},
    }
    url, prepared, native = proxy.prepare_chat_upstream_request(upstream, payload, settings)
    assert url == upstream["url"]
    assert native is False
    assert prepared["reasoning_effort"] == "none"
    assert prepared["chat_template_kwargs"] == {
        "custom_flag": "kept", "enable_thinking": False,
    }
    assert payload["chat_template_kwargs"] == {"custom_flag": "kept"}


def test_thinking_policy_isolated_by_upstream_and_unknown_auto_is_unchanged() -> None:
    settings = {
        "model_thinking_policies": [
            {"upstream": "local-a", "model": "same-model", "mode": "disabled"},
            {"upstream": "local-b", "model": "same-model", "mode": "high"},
            {"upstream": "cloud", "model": "same-model", "mode": "disabled"},
        ],
    }
    payload = {"model": "same-model", "messages": []}
    local_a = {"name": "local-a", "url": "http://a/v1/chat/completions", "thinking_protocol": "local_openai"}
    local_b = {"name": "local-b", "url": "http://b/v1/chat/completions", "thinking_protocol": "local_openai"}
    cloud = {"name": "cloud", "url": "https://provider.example/v1/chat/completions", "thinking_protocol": "auto"}
    assert proxy.prepare_chat_upstream_request(local_a, payload, settings)[1]["reasoning_effort"] == "none"
    assert proxy.prepare_chat_upstream_request(local_b, payload, settings)[1]["reasoning_effort"] == "high"
    assert proxy.prepare_chat_upstream_request(cloud, payload, settings)[1] == payload


def test_responses_thinking_policy_preserves_existing_reasoning_fields() -> None:
    upstream = {"name": "vLLM", "thinking_protocol": "local_openai"}
    settings = {
        "model_thinking_policies": [
            {"upstream": "vLLM", "model": "qwen3", "mode": "low"},
        ],
    }
    payload = {"model": "qwen3", "input": "hello", "reasoning": {"summary": "auto"}}
    prepared = proxy.prepare_responses_upstream_payload(upstream, payload, settings)
    assert prepared["reasoning"] == {"summary": "auto", "effort": "low"}
    assert prepared["chat_template_kwargs"]["enable_thinking"] is True
    assert payload["reasoning"] == {"summary": "auto"}


def test_thinking_settings_validate_protocols_and_upstream_references() -> None:
    valid = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "LM Studio",
        "default_upstream_thinking_protocol": "local_openai",
        "model_thinking_policies": [
            {"upstream": "LM Studio", "model": "qwen3", "mode": "disabled"},
        ],
        "upstreams": [{
            "name": "Ollama", "url": "http://ollama:11434/v1",
            "thinking_protocol": "ollama",
        }],
    }).model_dump(mode="json")
    assert valid["default_upstream_thinking_protocol"] == "local_openai"
    assert valid["upstreams"][0]["thinking_protocol"] == "ollama"
    assert valid["model_thinking_policies"][0]["mode"] == "disabled"

    for changes in (
        {"default_upstream_thinking_protocol": "unknown"},
        {"upstreams": [{"name": "bad", "url": "http://bad/v1", "thinking_protocol": "unknown"}]},
        {"model_thinking_policies": [{"upstream": "missing", "model": "qwen3", "mode": "disabled"}]},
    ):
        try:
            proxy.SettingsUpdate.model_validate({"target_api_url": "http://primary/v1", **changes})
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected invalid thinking settings: {changes}")


def test_chat_request_applies_lm_studio_model_policy(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "http://lmstudio:1234/v1/chat/completions",
        "default_upstream_name": "LM Studio",
        "default_upstream_thinking_protocol": "local_openai",
        "model_thinking_policies": [
            {"upstream": "LM Studio", "model": "qwen3", "mode": "disabled"},
        ],
    })
    proxy.save_settings(settings)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/chat/completions", json={
        "model": "qwen3", "messages": [],
        "chat_template_kwargs": {"keep": True},
    })
    assert response.status_code == 200
    assert calls[0][0] == "http://lmstudio:1234/v1/chat/completions"
    assert calls[0][1]["reasoning_effort"] == "none"
    assert calls[0][1]["chat_template_kwargs"] == {"keep": True, "enable_thinking": False}


def test_ollama_native_url_preserves_reverse_proxy_prefix() -> None:
    upstream = {
        "url": "https://models.example/gateway/v1/chat/completions",
        "ollama_url": "https://models.example/ollama",
    }
    assert proxy.ollama_native_chat_url(upstream) == "https://models.example/ollama/api/chat"


def test_ollama_native_stream_is_converted_to_openai_sse() -> None:
    class FakeResponse:
        async def aiter_bytes(self):
            yield b'{"model":"qwen3:8b","message":{"role":"assistant","content":"he"},"done":false}\n'
            yield b'{"model":"qwen3:8b","message":{"role":"assistant","content":"llo"},"done":false}\n'
            yield b'{"model":"qwen3:8b","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","prompt_eval_count":4,"eval_count":2}\n'

    async def collect() -> list[bytes]:
        return [frame async for frame in proxy.iter_ollama_chat_sse_frames(FakeResponse(), "qwen3:8b") if frame]

    frames = asyncio.run(collect())
    assert any(b'"content":"he"' in frame for frame in frames)
    assert any(b'"content":"llo"' in frame for frame in frames)
    assert any(b'"finish_reason":"stop"' in frame and b'"total_tokens":6' in frame for frame in frames)
    assert frames[-1] == b"data: [DONE]\n\n"


def test_chat_stream_uses_native_ollama_and_keeps_openai_contract(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "default_upstream_name": "Ollama",
        "model_thinking_policies": [
            {"upstream": "Ollama", "model": "qwen3", "mode": "disabled"},
        ],
    })
    proxy.save_settings(settings)
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/x-ndjson"}

        async def aiter_bytes(self):
            yield b'{"model":"qwen3","message":{"role":"assistant","content":"hello"},"done":false}\n'
            yield b'{"model":"qwen3","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","prompt_eval_count":3,"eval_count":1}\n'

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, method, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return object()

        async def send(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/chat/completions", json={"model": "qwen3", "messages": [], "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert calls == [("http://ollama:11434/api/chat", {"model": "qwen3", "messages": [], "stream": True, "think": False})]
    assert '"content": "hello"' in response.text
    assert '"total_tokens": 4' in response.text
    assert response.text.count("data: [DONE]") == 1


def test_chat_request_forwards_ollama_thinking_policy(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "default_upstream_name": "Ollama",
        "model_thinking_policies": [
            {"upstream": "Ollama", "model": "qwen3", "mode": "disabled"},
        ],
    })
    proxy.save_settings(settings)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"model": "qwen3", "message": {"role": "assistant", "content": "ok"}, "done": True}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/chat/completions", json={"model": "qwen3", "messages": []})
    assert response.status_code == 200
    assert calls[0][0] == "http://ollama:11434/api/chat"
    assert calls[0][1]["think"] is False


def test_responses_non_stream_ollama_error_is_not_returned_as_success(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "default_upstream_name": "Ollama",
    })
    proxy.save_settings(settings)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"error": "unable to load model: missing blob"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/responses", json={"model": "qwen3", "input": "hello"})
    assert response.status_code == 502
    assert "unable to load model: missing blob" in response.json()["error"]["message"]


def test_responses_stream_ollama_error_event_is_reported_without_fake_completion(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "default_upstream_name": "Ollama",
    })
    proxy.save_settings(settings)

    class StreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield b'{"error":"unable to load model: missing blob"}\n'

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return StreamResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/responses", json={"model": "qwen3", "input": "hello", "stream": True})
    assert response.status_code == 200
    assert "unable to load model: missing blob" in response.text
    assert '"type": "response.failed"' in response.text
    assert '"status": "completed"' not in response.text


def test_circuit_breaker_skips_open_upstream_and_recovers() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
        "circuit_breaker_failures": 2,
        "circuit_breaker_cooldown_seconds": 60,
    }).model_dump(mode="json")
    proxy.record_upstream_failure(settings, "primary", "timeout")
    proxy.record_upstream_failure(settings, "primary", "timeout")
    assert proxy.circuit_is_open("primary")
    assert proxy.route_upstreams(settings, "model")[0]["name"] == "backup"
    proxy.record_upstream_success("primary")
    assert not proxy.circuit_is_open("primary")


def test_model_scoped_503_does_not_open_provider_wide_circuit() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "https://pipio.example/v1",
        "default_upstream_name": "pipio",
        "upstreams": [{"name": "backup", "url": "https://backup.example/v1"}],
        "circuit_breaker_failures": 1,
    }).model_dump(mode="json")

    proxy.record_upstream_http_result(
        settings, "pipio", 503,
        detail="分组 GPT 下模型 gpt-5.6-luna 无可用渠道（distributor）",
    )

    assert not proxy.circuit_is_open("pipio")
    assert [item["name"] for item in proxy.route_upstreams(settings, "gpt-5.6-sol")][:2] == [
        "pipio", "backup",
    ]

    proxy.record_upstream_stream_failure(
        settings, "pipio", "no available channel for model gpt-5.6-luna"
    )
    assert not proxy.circuit_is_open("pipio")

    proxy.record_upstream_http_result(settings, "pipio", 503, detail="Service unavailable")
    assert proxy.circuit_is_open("pipio")
    proxy.UPSTREAM_CIRCUITS.clear()


def test_circuit_breaker_allows_only_one_half_open_probe() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1", "default_upstream_name": "primary",
        "circuit_breaker_failures": 1, "circuit_breaker_cooldown_seconds": 5,
    }).model_dump(mode="json")
    proxy.record_upstream_failure(settings, "primary", "timeout")
    proxy.UPSTREAM_CIRCUITS["primary"]["open_until"] = time.time() - 1
    assert [item["name"] for item in proxy.route_upstreams(settings, "model")] == ["primary"]
    assert proxy.acquire_upstream("primary") is True
    assert proxy.acquire_upstream("primary") is False
    proxy.record_upstream_success("primary")
    assert [item["name"] for item in proxy.route_upstreams(settings, "model")] == ["primary"]


def test_virtual_model_conditional_route_uses_request_context() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1", "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
        "virtual_models": [{"alias": "coding-best", "target": "gpt-real", "routes": [{
            "upstream": "backup", "priority": 0, "weight": 2,
            "token_patterns": ["codex-*"], "path_patterns": ["/v1/responses"],
        }]}],
    }).model_dump(mode="json")
    request = SimpleNamespace(state=SimpleNamespace(api_token={"name": "codex-main"}, usage_id="request-1"), url=SimpleNamespace(path="/v1/responses"))
    assert proxy.route_upstreams(settings, "coding-best", request)[0]["name"] == "backup"


def test_virtual_model_is_rewritten_before_chat_forwarding(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    saved = client.put("/api/settings", json={
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
        "virtual_models": [{"alias": "coding-best", "target": "gpt-real", "upstreams": ["backup"]}],
    })
    assert saved.status_code == 200, saved.text
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 2}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/v1/chat/completions", json={"model": "coding-best", "messages": []})
    assert response.status_code == 200
    assert calls == [("http://backup/v1/chat/completions", {"model": "gpt-real", "messages": []})]
    trace = client.get("/api/traces").json()["data"][0]
    assert trace["model"] == "coding-best"
    assert trace["resolved_model"] == "gpt-real"
    assert trace["upstream"] == "backup"


def test_api_token_rpm_limit_and_request_trace(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    created = client.post("/api/tokens", json={"name": "limited", "rpm_limit": 1})
    assert created.status_code == 200
    token = created.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert client.post("/v1/chat/completions", content="not-json", headers=headers).status_code == 400
    limited = client.post("/v1/chat/completions", json={"model": "test"}, headers=headers)
    assert limited.status_code == 429
    assert "每分钟请求数" in limited.json()["error"]["message"]
    assert limited.json()["error"]["type"] == "rate_limit_error"
    trace = client.get("/api/traces").json()["data"][0]
    assert trace["token_name"] == "limited"
    assert trace["status_code"] == 400
    filtered = client.get("/api/traces", params={"request_id": trace["id"][:8], "token": "limited", "limit": 1}).json()
    assert filtered["total"] == 1
    assert filtered["has_more"] is False


def test_token_policy_limits_are_persisted(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    token_id = client.post("/api/tokens", json={"name": "policy"}).json()["id"]
    response = client.patch(f"/api/tokens/{token_id}/policy", json={
        "expires_at": None,
        "allowed_models": ["coding-*"],
        "rpm_limit": 20,
        "tpm_limit": 30000,
        "daily_token_limit": 500000,
        "monthly_token_limit": 5000000,
    })
    assert response.status_code == 200
    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["rpm_limit"] == 20
    assert listed["monthly_token_limit"] == 5000000


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
    proxy.MODEL_CACHE.update({"at": time.time(), "data": [{"id": "stale"}], "source": "old"})
    proxy.ROUTE_AFFINITY["stale"] = ("旧上游", time.time() + 900)
    settings = {
        "target_api_url": "http://ollama:11434/v1/chat/completions",
        "default_upstream_name": "本地 Ollama",
        "api_key": "secret",
        "timeout_seconds": 42,
        "default_upstream_thinking_protocol": "ollama",
        "model_thinking_policies": [
            {"upstream": "本地 Ollama", "model": "qwen3:8b", "mode": "high"},
        ],
        "clean_patterns": [r"(?is)<think>.*?</think>", r"<tag>"],
    }
    response = client.put("/api/settings", json=settings)
    assert response.status_code == 200, response.text
    loaded = client.get("/api/settings").json()
    assert loaded["default_upstream_name"] == settings["default_upstream_name"]
    assert loaded["clean_patterns"] == settings["clean_patterns"]
    assert loaded["default_upstream_thinking_protocol"] == "ollama"
    assert loaded["model_thinking_policies"] == settings["model_thinking_policies"]
    assert proxy.clean_content("A<think>hidden</think>B<tag>", loaded) == "AB"
    assert proxy.MODEL_CACHE["data"] is None
    assert proxy.ROUTE_AFFINITY == {}
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


def test_admin_note_requires_login_and_persists_without_logging_content(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/api/account/note").status_code == 401
    assert client.put("/api/account/note", json={"note": "private"}).status_code == 401

    login(client)
    assert client.put("/api/account/note", json={"note": "x" * 10_001}).status_code == 422
    note = "临时维护：晚间验证备用上游"
    saved = client.put("/api/account/note", json={"note": note})
    assert saved.status_code == 200
    assert client.get("/api/account/note").json() == {"note": note}
    assert proxy.load_settings()["admin_note"] == note

    current = client.get("/api/settings").json()
    assert client.put("/api/settings", json=current).status_code == 200
    assert client.get("/api/account/note").json()["note"] == note
    audit = client.get("/api/audit/logs").json()["data"]
    assert any(item["action"] == "更新管理员备忘录" for item in audit)
    assert note not in json.dumps(audit, ensure_ascii=False)


def test_admin_chat_uses_web_session_and_public_chat_routing(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    payload = {"model": "coding-best", "messages": [{"role": "user", "content": "hello"}]}
    assert client.post("/api/chat/completions", json=payload).status_code == 401
    login(client)
    saved = client.put("/api/settings", json={
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
        "virtual_models": [{
            "alias": "coding-best",
            "target": "gpt-real",
            "routes": [{
                "upstream": "backup",
                "priority": 0,
                "weight": 1,
                "path_patterns": ["/v1/chat/completions"],
            }],
        }],
    })
    assert saved.status_code == 200, saved.text
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/api/chat/completions", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    assert calls == [("http://backup/v1/chat/completions", {
        "model": "gpt-real",
        "messages": [{"role": "user", "content": "hello"}],
    })]
    request_id = response.headers.get("X-Request-ID")
    assert request_id
    trace = client.get("/api/traces", params={"request_id": request_id}).json()["data"][0]
    assert trace["token_name"] == "Web 对话"
    assert trace["path"] == "/api/chat/completions"
    assert trace["resolved_model"] == "gpt-real"
    assert trace["upstream"] == "backup"


def test_admin_chat_stream_preserves_done_and_records_first_byte(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = proxy.load_settings()
    settings.update({
        "target_api_url": "https://provider.example/v1/chat/completions",
        "default_upstream_name": "provider",
    })
    proxy.save_settings(settings)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/api/chat/completions", json={
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    })
    assert response.status_code == 200
    assert response.text.count("data: [DONE]") == 1
    assert '"content": "hello"' in response.text
    request_id = response.headers["X-Request-ID"]
    trace = client.get("/api/traces", params={"request_id": request_id}).json()["data"][0]
    assert trace["first_byte_ms"] is not None
    assert trace["termination_reason"] == "stream_completed"


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
    assert {"resolved_model", "upstream", "status_code", "attempts", "error", "termination_reason"}.issubset(columns)


def test_compatibility_test_checks_all_upstream_endpoints(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)
    calls = []

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            if calls[-1][1].endswith("/responses"):
                yield 'event: response.completed'
                yield 'data: {"type":"response.completed"}'
            else:
                yield 'data: [DONE]'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            return FakeResponse()

        def stream(self, method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post(
        "/api/compatibility/test",
        json={"upstream": "默认上游", "model": "test-model"},
    )
    assert response.status_code == 200
    assert len(response.json()["results"]) == 6
    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST", "POST", "POST"]
    assert all(item["ok"] for item in response.json()["results"])


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
    configure_non_ollama_upstream()
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


def test_native_responses_stream_sends_semantic_keepalive_and_preserves_identity(
    tmp_path: Path, monkeypatch
) -> None:
    client = client_for(tmp_path)
    configure_non_ollama_upstream()

    class FakeResponse:
        status_code = 200

        async def aiter_bytes(self):
            if False:
                yield b""

        async def aclose(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            return FakeResponse()

        async def aclose(self):
            pass

    async def fake_frames(_response):
        yield (
            b'event: response.created\n'
            b'data: {"type":"response.created","response":{"id":"resp_upstream",'
            b'"status":"in_progress"},"sequence_number":0}\n\n'
        )
        yield None
        yield (
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","response_id":"resp_upstream",'
            b'"delta":"ok","sequence_number":1}\n\n'
        )
        yield (
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"id":"resp_upstream",'
            b'"status":"completed"},"sequence_number":2}\n\n'
        )

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(proxy, "iter_sse_frames", fake_frames)
    response = client.post(
        "/v1/responses", json={"model": "test", "input": "hello", "stream": True}
    )
    assert response.status_code == 200
    assert response.text.startswith("event: response.created\n")
    assert response.text.count("event: response.created") == 1
    assert response.text.count("event: response.in_progress") == 1
    assert response.text.count("event: response.completed") == 1
    assert "resp_upstream" in response.text
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    sequences = [item["sequence_number"] for item in events]
    assert sequences == sorted(set(sequences))
    proxy_id = events[0]["response"]["id"]
    assert proxy_id == "resp_upstream"
    assert events[2]["response_id"] == proxy_id
    assert events[3]["response"]["id"] == proxy_id


def test_native_responses_stream_does_not_switch_identity_after_created(
    tmp_path: Path, monkeypatch
) -> None:
    client = client_for(tmp_path)
    configure_non_ollama_upstream()
    settings = proxy.load_settings()
    settings["upstreams"] = [{
        "name": "backup", "url": "https://backup.example/v1",
        "api_key": "", "enabled": True,
    }]
    proxy.save_settings(settings)
    calls = 0

    class FakeResponse:
        status_code = 200

        async def aiter_bytes(self):
            yield (
                b'event: response.created\n'
                b'data: {"type":"response.created","response":{"id":"resp_primary",'
                b'"status":"in_progress"}}\n\n'
            )
            yield (
                b'event: response.failed\n'
                b'data: {"type":"response.failed","response":{"id":"resp_primary",'
                b'"status":"failed","error":{"message":"generation failed"}}}\n\n'
            )

        async def aclose(self):
            pass

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post(
        "/v1/responses", json={"model": "test", "input": "hello", "stream": True}
    )
    assert response.status_code == 200
    assert calls == 1
    assert response.text.count("event: response.created") == 1
    assert response.text.count("event: response.failed") == 1
    assert "resp_primary" in response.text
    assert "response.completed" not in response.text
    with proxy.database_connection() as database:
        trace = database.execute(
            "SELECT termination_reason FROM api_usage ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert trace["termination_reason"] == "response.failed"


def test_native_responses_stream_translates_chat_sse_compatibility_response(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    configure_non_ollama_upstream()
    proxy.invalidate_proxy_runtime()
    chunks = [
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    class FakeResponse:
        status_code = 200

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk

        async def aclose(self):
            pass

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
    assert '"type":"response.output_text.delta"' in response.text
    assert response.text.count("event: response.completed") == 1
    assert "event: response.failed" not in response.text


def test_native_responses_stream_reports_failure_when_upstream_omits_terminal_event(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    configure_non_ollama_upstream()

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


def test_native_responses_stream_preserves_real_incomplete_terminal(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    configure_non_ollama_upstream()

    class FakeResponse:
        status_code = 200

        async def aiter_bytes(self):
            yield (
                b'event: response.incomplete\n'
                b'data: {"type":"response.incomplete","response":{"id":"resp_partial",'
                b'"status":"incomplete","usage":{"input_tokens":2,"output_tokens":3}}}\n\n'
            )

        async def aclose(self):
            pass

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
    assert response.text.count("event: response.incomplete") == 1
    assert "event: response.failed" not in response.text


def test_stream_cleaning_preserves_delta_whitespace() -> None:
    line = 'data: {"choices":[{"delta":{"content":" hello"}}]}'
    cleaned = proxy.clean_stream_line(line, {"clean_patterns": []})
    assert json.loads(cleaned[6:])["choices"][0]["delta"]["content"] == " hello"


def test_incremental_stream_cleaner_handles_split_think_tags() -> None:
    settings = {"clean_patterns": [r"(?is)<think>.*?</think>"]}
    cleaner = proxy.StreamTextCleaner(settings)
    assert cleaner.feed("before<th") == "before"
    assert cleaner.feed("ink>hidden") == ""
    assert cleaner.feed(" text</thi") == ""
    assert cleaner.feed("nk>after") == "after"
    assert cleaner.flush() == ""


def test_incremental_stream_cleaner_handles_cleanllm_channel_syntax() -> None:
    cleaner = proxy.StreamTextCleaner({"clean_patterns": proxy.DEFAULT_PATTERNS})
    assert cleaner.feed("before<*|chan") == "before"
    assert cleaner.feed("nel*>hidden") == ""
    assert cleaner.feed("<|/chan") == ""
    assert cleaner.feed("nel|>after") == "after"


def test_incremental_stream_cleaner_does_not_buffer_unrelated_angle_text() -> None:
    settings = {"clean_patterns": [r"(?is)<think>.*?</think>", r"(?i)<[a-z]+>"]}
    cleaner = proxy.StreamTextCleaner(settings)
    assert cleaner.feed("A<example>B") == "AB"


def test_incremental_stream_cleaner_handles_generic_tag_split() -> None:
    cleaner = proxy.StreamTextCleaner({"clean_patterns": [r"(?i)<[a-zA-Z0-9_]+>"]})
    assert cleaner.feed("A<exam") == "A"
    assert cleaner.feed("ple>B") == "B"


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
        "cached_tokens": 0,
        "total_tokens": 19,
    }
    assert proxy.usage_values({"input_tokens": 8, "output_tokens": 3}) == {
        "input_tokens": 8,
        "output_tokens": 3,
        "cached_tokens": 0,
        "total_tokens": 11,
    }


def test_usage_values_reads_cached_tokens() -> None:
    assert proxy.usage_values({
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 80},
    }) == {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 80, "total_tokens": 120}


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
        def __init__(self, *args, **kwargs):
            pass

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


def test_ollama_registry_targets_only_public_registry_models() -> None:
    assert proxy._ollama_registry_target("qwen3:8b") == (
        "registry.ollama.ai", "library/qwen3", "8b"
    )
    assert proxy._ollama_registry_target("rinex20/translategemma3:12b") == (
        "registry.ollama.ai", "rinex20/translategemma3", "12b"
    )
    assert proxy._ollama_registry_target("registry.example:5000/team/model:latest") is None
    assert proxy._ollama_registry_target("local-model@sha256:abc") is None


def test_ollama_digest_normalization_accepts_ollama_variants() -> None:
    digest = "A" * 64
    assert proxy._normalize_ollama_digest(digest) == f"sha256:{digest.lower()}"
    assert proxy._normalize_ollama_digest(f"SHA256:{digest}") == f"sha256:{digest.lower()}"
    assert proxy._normalize_ollama_digest("sha256:short") is None


def test_ollama_serialized_manifest_digest_matches_go_encoding() -> None:
    payload = {
        "layers": [
            {
                "size": 2,
                "digest": "sha256:layer",
                "mediaType": "application/vnd.ollama.image.model",
                "from": "base&model",
            }
        ],
        "config": {
            "size": 1,
            "digest": "sha256:config",
            "mediaType": "application/vnd.docker.container.image.v1+json",
        },
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "schemaVersion": 2,
        "ignoredByOllama": True,
    }
    go_encoded = (
        b'{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json",'
        b'"config":{"mediaType":"application/vnd.docker.container.image.v1+json",'
        b'"digest":"sha256:config","size":1},"layers":[{"mediaType":'
        b'"application/vnd.ollama.image.model","digest":"sha256:layer","size":2,'
        b'"from":"base\\u0026model"}]}\n'
    )
    assert proxy._ollama_serialized_manifest_digest(payload) == (
        f"sha256:{proxy.hashlib.sha256(go_encoded).hexdigest()}"
    )


def test_ollama_registry_bearer_challenge_is_parsed() -> None:
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "config", "digest": f"sha256:{'d' * 64}", "size": 1},
        "layers": [],
    }
    digest = proxy._ollama_serialized_manifest_digest(manifest)
    assert digest
    calls = []

    class FakeResponse:
        def __init__(self, *, status_code=200, headers=None, payload=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.payload = payload or {}

        def json(self):
            return self.payload

    class FakeClient:
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://auth.ollama.com/token":
                return FakeResponse(payload={"token": "registry-token"})
            if kwargs.get("headers", {}).get("Authorization") == "Bearer registry-token":
                return FakeResponse(
                    headers={"docker-content-digest": f"sha256:{'e' * 64}"},
                    payload=manifest,
                )
            return FakeResponse(
                status_code=401,
                headers={
                    "www-authenticate": (
                        'Bearer realm="https://auth.ollama.com/token",'
                        'service="registry.ollama.ai",'
                        'scope="repository:library/qwen3:pull"'
                    )
                },
            )

    remote_digest, detail = asyncio.run(
        proxy._ollama_remote_digest(FakeClient(), "qwen3:8b", digest)
    )

    assert remote_digest == digest
    assert detail == ""
    assert any(url == "https://auth.ollama.com/token" for url, _ in calls)


def test_ollama_model_update_check_compares_serialized_manifest_digests(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    assert client.get("/api/ollama/models/updates").status_code == 401
    login(client)
    calls = []
    same_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "config", "digest": f"sha256:{'a' * 64}", "size": 1},
        "layers": [],
    }
    old_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "config", "digest": f"sha256:{'b' * 64}", "size": 1},
        "layers": [],
    }
    new_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "config", "digest": f"sha256:{'c' * 64}", "size": 1},
        "layers": [],
    }
    same_digest = proxy._ollama_serialized_manifest_digest(same_manifest)
    old_digest = proxy._ollama_serialized_manifest_digest(old_manifest)
    new_digest = proxy._ollama_serialized_manifest_digest(new_manifest)
    assert same_digest and old_digest and new_digest

    class FakeResponse:
        def __init__(self, payload=None, *, status_code=200, headers=None, content=b""):
            self.payload = payload or {}
            self.status_code = status_code
            self.headers = headers or {}
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise proxy.httpx.HTTPStatusError(
                    "failed", request=proxy.httpx.Request("GET", "http://test"), response=None
                )

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(url)
            if url.endswith("/api/tags"):
                return FakeResponse({"models": [
                    # Ollama versions exist that omit the sha256: prefix.
                    {"name": "qwen3:8b", "digest": same_digest.removeprefix("sha256:")},
                    {"name": "rinex20/translategemma3:12b", "digest": old_digest},
                    {"name": "headerless:latest", "digest": old_digest},
                    {"name": "local-model@sha256:abc", "digest": old_digest},
                ]})
            if "/library/headerless/" in url:
                return FakeResponse(old_manifest)
            if "/rinex20/translategemma3/manifests/12b" in url:
                return FakeResponse(new_manifest, headers={"docker-content-digest": new_digest})
            return FakeResponse(
                same_manifest,
                # The Registry body/header digest is intentionally different
                # from Ollama's locally re-serialized manifest digest.
                headers={"docker-content-digest": f"sha256:{'d' * 64}"},
            )

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.get("/api/ollama/models/updates")

    assert response.status_code == 200
    by_model = {item["id"]: item for item in response.json()["data"]}
    assert by_model["qwen3:8b"]["state"] == "latest"
    assert by_model["qwen3:8b"]["local_digest"] == same_digest
    assert by_model["headerless:latest"]["state"] == "latest"
    assert by_model["rinex20/translategemma3:12b"]["state"] == "update"
    assert by_model["rinex20/translategemma3:12b"]["update_available"] is True
    assert by_model["local-model@sha256:abc"]["state"] == "unknown"
    assert by_model["local-model@sha256:abc"]["detail"] == "仅支持 Ollama 公共仓库模型"
    assert not any("local-model" in url for url in calls)
    assert not any(url.endswith(old_digest) for url in calls)


def test_ollama_pull_surfaces_stream_errors_instead_of_reporting_success(tmp_path: Path, monkeypatch) -> None:
    client = client_for(tmp_path)
    login(client)

    class PullResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_bytes(self):
            yield b'{"status":"pulling manifest"}\n'
            yield b'{"error":"unable to load model: missing blob"}\n'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return PullResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    response = client.post("/api/ollama/pull", json={"model": "broken:latest"})
    assert response.status_code == 200
    assert "unable to load model: missing blob" in response.text
    assert "拉取失败" in response.text


def test_ollama_pull_task_requires_success_and_verifies_model(tmp_path: Path, monkeypatch) -> None:
    client_for(tmp_path)
    proxy.OLLAMA_TASKS.clear()
    proxy.OLLAMA_HANDLES.clear()
    task_id = "pull-test"
    proxy.OLLAMA_TASKS[task_id] = {"id": task_id, "model": "qwen3:8b", "status": "queued", "message": "等待开始", "completed": 0, "total": 0, "created_at": 1, "updated_at": 1}

    class ShowResponse:
        status_code = 200

        def json(self):
            return {"name": "qwen3:8b"}

    class PullResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            yield '{"status":"pulling manifest"}'
            yield '{"status":"success"}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            return PullResponse()

        async def post(self, *args, **kwargs):
            return ShowResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    asyncio.run(proxy.run_ollama_pull(task_id, "qwen3:8b"))
    assert proxy.OLLAMA_TASKS[task_id]["status"] == "completed"
    assert "已校验" in proxy.OLLAMA_TASKS[task_id]["message"]


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
    assert result.json()["upstreams"] == ["默认上游", "备用"]


def test_connectivity_lists_configured_upstreams_before_first_check(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = client.get("/api/settings").json()
    settings["upstreams"] = [{"name": "尚未检测", "url": "http://backup:8000/v1"}]
    assert client.put("/api/settings", json=settings).status_code == 200
    proxy.CONNECTIVITY_STATE.update({"checked_at": None, "results": [], "availability_percent": None})

    result = client.get("/api/settings/connectivity")

    assert result.status_code == 200
    assert [item["name"] for item in result.json()["results"]] == ["默认上游", "尚未检测"]
    assert all(item["ok"] is None for item in result.json()["results"])
    assert result.json()["availability_percent"] is None


def test_connectivity_uses_models_url_and_recovers_expired_circuit(tmp_path: Path, monkeypatch) -> None:
    client_for(tmp_path)
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "models_api_url": "http://primary/custom/models",
        "circuit_breaker_failures": 1,
    }).model_dump(mode="json")
    proxy.record_upstream_failure(settings, "primary", "HTTP 503")
    proxy.UPSTREAM_CIRCUITS["primary"]["open_until"] = time.time() - 1
    calls = []

    class FakeResponse:
        def raise_for_status(self): return None

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, **kwargs):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(proxy.run_connectivity_test(settings))
    assert calls == ["http://primary/custom/models"]
    assert result["results"][0]["ok"] is True
    assert "primary" not in proxy.UPSTREAM_CIRCUITS


def test_manual_connectivity_success_immediately_recovers_open_circuit(tmp_path: Path, monkeypatch) -> None:
    client_for(tmp_path)
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "circuit_breaker_failures": 1,
    }).model_dump(mode="json")
    proxy.record_upstream_failure(settings, "primary", "HTTP 503")
    assert proxy.circuit_is_open("primary")

    class FakeResponse:
        def raise_for_status(self): return None

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, *args, **kwargs): return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(proxy.run_connectivity_test(settings, manual=True))

    assert result["results"][0]["ok"] is True
    assert "primary" not in proxy.UPSTREAM_CIRCUITS


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


def test_pricing_and_analytics_are_persisted(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = client.get("/api/settings").json()
    settings["model_pricing"] = [{
        "pattern": "gpt-*", "upstream": "", "input_price": 2.0,
        "output_price": 8.0, "cached_price": 0.5,
    }]
    assert client.put("/api/settings", json=settings).status_code == 200
    now = int(time.time())
    with proxy.database_connection() as database:
        database.execute(
            """INSERT INTO api_usage
               (id, token_name, at, path, method, model, resolved_model, upstream,
                latency_ms, input_tokens, output_tokens, cached_tokens, total_tokens, status_code)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("priced", "demo", now, "/v1/responses", "POST", "gpt-alias", "gpt-real", "primary", 250, 1000, 500, 800, 1500, 200),
        )
    proxy.update_usage_record("priced", total_tokens=1500)
    with proxy.database_connection() as database:
        cost = database.execute("SELECT cost_usd FROM api_usage WHERE id = 'priced'").fetchone()[0]
    assert cost == 0.0048
    analytics = client.get("/api/analytics?days=1&group_by=model")
    assert analytics.status_code == 200
    assert analytics.json()["overview"]["calls"] == 1
    assert analytics.json()["overview"]["p95_latency"] == 250
    assert analytics.json()["groups"][0]["cost"] == 0.0048
    monthly = client.get("/api/analytics?days=30&group_by=model&interval=month")
    assert monthly.status_code == 200
    assert monthly.json()["interval"] == "month"
    assert "bucket" in monthly.json()["trend"][0]
    exported = client.get("/api/analytics/export?days=1&group_by=model")
    assert exported.status_code == 200
    assert "text/csv" in exported.headers["content-type"]
    assert "gpt-alias" in exported.text


def test_token_monthly_budget_can_disable_token(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = client.get("/api/settings").json()
    settings["model_pricing"] = [{"pattern": "*", "upstream": "", "input_price": 1000, "output_price": 0, "cached_price": 0}]
    assert client.put("/api/settings", json=settings).status_code == 200
    created = client.post("/api/tokens", json={"name": "budget", "monthly_budget_usd": 0.0001, "budget_action": "disable"}).json()
    token = proxy.database_tokens()[0]
    message = proxy.token_limit_error(token, int(time.time()), 1000, "gpt-test")
    assert "自动停用" in message
    assert proxy.database_tokens()[0]["enabled"] == 0


def test_config_snapshots_restore_and_backup(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    initial = client.get("/api/settings").json()
    initial["default_upstream_name"] = "before"
    assert client.put("/api/settings", json=initial).status_code == 200
    snapshot = client.post("/api/settings/snapshots", json={"reason": "known good"})
    assert snapshot.status_code == 200
    changed = client.get("/api/settings").json()
    changed["default_upstream_name"] = "after"
    assert client.put("/api/settings", json=changed).status_code == 200
    listed = client.get("/api/settings/snapshots").json()["data"]
    known = next(item for item in listed if item["id"] == snapshot.json()["id"])
    assert "default_upstream_name" in known["changed_keys"]
    restored = client.post(f"/api/settings/snapshots/{snapshot.json()['id']}/restore")
    assert restored.status_code == 200
    assert client.get("/api/settings").json()["default_upstream_name"] == "before"
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    assert backup.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        assert {"settings.json", "cleanllm.db", "manifest.json"}.issubset(archive.namelist())


def test_health_and_prometheus_metrics(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/health/live").json()["status"] == "alive"
    assert client.get("/health/ready").status_code == 200
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "cleanllm_requests_total" in metrics.text
    assert "cleanllm_tokens_total" in metrics.text
    assert 'cleanllm_upstream_requests_total{upstream="默认上游"} 0' in metrics.text


def test_status_events_are_broadcast_to_all_subscribers() -> None:
    first, second = asyncio.Queue(), asyncio.Queue()
    proxy.STATUS_SUBSCRIBERS.update({first, second})
    try:
        proxy.publish_status("request", {"status": 200})
        assert first.get_nowait()["data"]["status"] == 200
        assert second.get_nowait()["data"]["status"] == 200
    finally:
        proxy.STATUS_SUBSCRIBERS.difference_update({first, second})


def test_second_stage_management_apis_require_admin(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/api/analytics").status_code == 401
    assert client.get("/api/settings/snapshots").status_code == 401
    assert client.get("/api/backup").status_code == 401
    assert client.post("/api/alerts/test", json={"url": "http://example.test/hook"}).status_code == 401


def test_upstream_batch_status_and_safe_export(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    settings = client.get("/api/settings").json()
    settings["api_key"] = "primary-secret"
    settings["upstreams"] = [{"name": "备用", "url": "http://backup/v1", "api_key": "backup-secret"}]
    assert client.put("/api/settings", json=settings).status_code == 200

    disabled = client.patch("/api/upstreams/status", json={"names": ["备用"], "enabled": False})
    assert disabled.status_code == 200
    loaded = client.get("/api/settings").json()
    assert loaded["upstreams"][0]["enabled"] is False
    assert [item["name"] for item in proxy.configured_upstreams(loaded)] == ["默认上游"]

    exported = client.get("/api/upstreams/export")
    assert exported.status_code == 200
    payload = exported.json()
    assert [item["api_key"] for item in payload["upstreams"]] == ["", ""]
    assert "primary-secret" not in exported.text
    assert "backup-secret" not in exported.text

    cannot_disable_all = client.patch("/api/upstreams/status", json={"names": ["默认上游"], "enabled": False})
    assert cannot_disable_all.status_code == 422


def test_upstream_import_renames_duplicates(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    imported = client.post("/api/upstreams/import", json={
        "mode": "append",
        "upstreams": [{"name": "默认上游", "url": "http://second/v1", "enabled": True}],
    })
    assert imported.status_code == 200, imported.text
    names = [client.get("/api/settings").json()["default_upstream_name"], *[
        item["name"] for item in client.get("/api/settings").json()["upstreams"]
    ]]
    assert names == ["默认上游", "默认上游 2"]


def test_model_metadata_normalizes_context_capabilities_and_interfaces() -> None:
    metadata = proxy.model_metadata({
        "context_window": 128000,
        "capabilities": {"tools": True, "audio": False},
        "supported_endpoints": ["/v1/chat/completions", "/v1/responses"],
    }, "vision-model")
    assert metadata["context_length"] == 128000
    assert metadata["capabilities"] == ["tools", "vision"]
    assert metadata["interfaces"] == ["v1/chat/completions", "v1/responses"]
    assert proxy.model_metadata({}, "text-embedding-3-small")["interfaces"] == ["v1/embeddings"]


def test_token_note_ip_allowlist_and_audit_log(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    created = client.post("/api/tokens", json={
        "name": "restricted",
        "note": "office client",
        "ip_allowlist": ["10.0.0.25", "2001:db8::/32"],
    })
    assert created.status_code == 200, created.text
    listed = client.get("/api/tokens").json()["data"][0]
    assert listed["note"] == "office client"
    assert listed["ip_allowlist"] == ["10.0.0.25/32", "2001:db8::/32"]
    saved = proxy.database_tokens()[0]
    assert proxy.token_allows_ip(saved, "10.0.0.25")
    assert not proxy.token_allows_ip(saved, "10.0.0.26")

    rejected = client.get("/v1/models", headers={"Authorization": f"Bearer {created.json()['token']}"})
    assert rejected.status_code == 403
    assert "IP" in rejected.json()["detail"]
    audit = client.get("/api/audit/logs").json()["data"]
    assert any(item["action"] == "创建令牌" and item["actor"] == "admin" for item in audit)
    assert all("office client" not in json.dumps(item) for item in audit)


def test_third_stage_management_apis_require_admin(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    assert client.get("/api/upstreams/export").status_code == 401
    assert client.post("/api/upstreams/import", json={"upstreams": [{"name": "x", "url": "http://x/v1"}]}).status_code == 401
    assert client.patch("/api/upstreams/status", json={"names": ["x"], "enabled": True}).status_code == 401
    assert client.get("/api/audit/logs").status_code == 401


def test_sse_frame_helpers_and_retry_after() -> None:
    frame, remainder = proxy.pop_sse_frame(b"event: response.output_text.delta\ndata: {\"delta\":\"ok\"}\n\nrest")
    assert frame is not None and remainder == b"rest"
    raw, payload = proxy.sse_frame_payload(frame)
    assert raw.startswith("{\"delta\"")
    assert payload == {"delta": "ok"}
    assert proxy.chat_event_state("[DONE]", None) == (False, True, True)
    assert proxy.retry_after_seconds({"retry-after": "7"}) == 7


def test_responses_event_state_distinguishes_failure_and_output() -> None:
    assert proxy.responses_event_state({"type": "response.created"}) == (False, False, False)
    assert proxy.responses_event_state({"type": "response.failed", "error": {"message": "down"}})[0]
    assert proxy.responses_event_state({"type": "response.output_text.delta", "delta": "hello"})[1]


def test_client_disconnect_releases_half_open_probe_without_penalizing_upstream() -> None:
    proxy.UPSTREAM_CIRCUITS["provider"] = {
        "failures": 3,
        "open_until": time.time() - 1,
        "phase": "open",
        "probe_in_flight": False,
    }
    assert proxy.acquire_upstream("provider") is True
    assert proxy.UPSTREAM_CIRCUITS["provider"]["probe_in_flight"] is True

    proxy.record_client_disconnect(None, "provider")

    assert proxy.UPSTREAM_CIRCUITS["provider"]["probe_in_flight"] is False
    assert proxy.UPSTREAM_CIRCUITS["provider"]["failures"] == 3
    proxy.UPSTREAM_CIRCUITS.clear()


def test_runtime_invalidation_clears_negative_routes_and_circuits() -> None:
    proxy.MODEL_CACHE.update({"at": time.time(), "data": [{"id": "stale"}], "source": "old"})
    proxy.ROUTE_AFFINITY["model"] = ("provider", time.time() + 60)
    proxy.NO_ROUTE_CACHE["model|path"] = time.time() + 30
    proxy.UPSTREAM_CIRCUITS["provider"] = {"failures": 1}

    proxy.invalidate_proxy_runtime()

    assert proxy.MODEL_CACHE["data"] is None
    assert proxy.MODEL_CACHE["failed_upstreams"] == []
    assert proxy.ROUTE_AFFINITY == {}
    assert proxy.NO_ROUTE_CACHE == {}
    assert proxy.UPSTREAM_CIRCUITS == {}


def test_failed_model_discovery_upstream_remains_inference_fallback() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "https://primary.example/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "uncertain", "url": "https://uncertain.example/v1"}],
    }).model_dump(mode="json")
    proxy.MODEL_CACHE.update({
        "data": [{"id": "shared", "upstream": "primary", "interfaces": ["v1/chat/completions"]}],
        "at": time.time(),
        "source": "test",
        "failed_upstreams": ["uncertain"],
    })
    request = SimpleNamespace(
        state=SimpleNamespace(api_token=None, usage_id="route-test"),
        url=SimpleNamespace(path="/v1/chat/completions"),
    )

    candidates = proxy.route_upstreams(settings, "shared", request)

    assert [item["name"] for item in candidates] == ["primary", "uncertain"]
    proxy.invalidate_proxy_runtime()


def test_route_model_interface_paths_ignore_leading_slash() -> None:
    settings = proxy.SettingsUpdate.model_validate({
        "target_api_url": "http://primary/v1",
        "default_upstream_name": "primary",
        "upstreams": [{"name": "backup", "url": "http://backup/v1"}],
    }).model_dump(mode="json")
    proxy.MODEL_CACHE.update({
        "data": [{
            "id": "shared",
            "upstream": "primary",
            "interfaces": ["v1/chat/completions", "v1/responses"],
        }],
        "at": time.time(),
        "source": "test",
        "failed_upstreams": [],
    })
    request = SimpleNamespace(
        state=SimpleNamespace(api_token=None, usage_id="route-interface"),
        url=SimpleNamespace(path="/v1/responses"),
    )
    candidates = proxy.route_upstreams(
        settings, "shared", request, capability_path="/v1/responses"
    )
    assert [item["name"] for item in candidates] == ["primary"]
    proxy.invalidate_proxy_runtime()


def test_token_cipher_is_authenticated_and_reads_legacy_records() -> None:
    token_id = "token-id"
    token = "cln_test-secret"
    cipher = proxy.token_cipher(token, token_id)
    assert cipher.startswith("v2.")
    assert proxy.token_plain(cipher, token_id) == token

    payload = bytearray(proxy.base64.urlsafe_b64decode(cipher[3:].encode()))
    payload[-1] ^= 1
    damaged = "v2." + proxy.base64.urlsafe_b64encode(payload).decode()
    try:
        proxy.token_plain(damaged, token_id)
        assert False, "tampered token ciphertext must be rejected"
    except ValueError:
        pass

    key = proxy.hashlib.sha256(proxy.session_secret() + token_id.encode()).digest()
    legacy = bytes(value ^ key[index % len(key)] for index, value in enumerate(token.encode()))
    assert proxy.token_plain(proxy.base64.urlsafe_b64encode(legacy).decode(), token_id) == token


def test_usage_logs_support_real_pagination(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    login(client)
    now = int(time.time())
    with proxy.database_connection() as database:
        database.executemany(
            """INSERT INTO api_usage
               (id, token_id, token_name, at, path, method, model, total_tokens)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
            [(f"page-{index}", "分页客户端", now + index, "/v1/responses", "POST", "test", index)
             for index in range(20)],
        )

    first = client.get("/api/usage?log_limit=8&log_offset=0").json()
    second = client.get("/api/usage?log_limit=8&log_offset=8").json()

    assert len(first["logs"]) == 8
    assert len(second["logs"]) == 8
    assert first["log_total"] == 20
    assert first["has_more_logs"] is True
    assert {item["id"] for item in first["logs"]}.isdisjoint(item["id"] for item in second["logs"])


def test_login_failures_are_rate_limited(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    for _ in range(proxy.LOGIN_RATE_MAX_FAILURES):
        assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    limited = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
