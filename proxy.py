import hashlib
import hmac
import base64
import asyncio
import fnmatch
import json
import logging
import os
import re
import secrets
import time
import uuid
import signal
import socket
import tarfile
import tempfile
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, field_validator

logger = logging.getLogger("cleanllm")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "cleanllm.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
STATIC_DIR = BASE_DIR / "static"
OLLAMA_MODELS_DIR = Path(os.getenv("OLLAMA_MODELS_DIR", "/ollama-models"))
SESSION_COOKIE = "cleanllm_session"
SESSION_MAX_AGE = 12 * 60 * 60

DEFAULT_PATTERNS = [
    r"(?is)[<＜]\s*[\|｜]*\s*channel\s*[>＞].*?[<＜]\s*[\|｜/]*\s*channel\s*[\|｜]*\s*[>＞]",
    r"(?is)[<＜]\s*think\s*[>＞].*?[<＜]\s*[\|｜/]*\s*think\s*[>＞]",
    r"(?i)[<＜]\s*[\|｜/]*\s*[a-zA-Z0-9_]+\s*[\|｜/]*\s*[>＞]",
]
DEFAULT_SETTINGS = {
    "target_api_url": os.getenv(
        "TARGET_API_URL", "http://host.docker.internal:11434/v1/chat/completions"
    ),
    "api_key": os.getenv("UPSTREAM_API_KEY", ""),
    "timeout_seconds": int(os.getenv("REQUEST_TIMEOUT", "120")),
    "models_api_url": os.getenv("MODELS_API_URL", ""),
    "ollama_api_url": os.getenv("OLLAMA_API_URL", ""),
    "clean_patterns": DEFAULT_PATTERNS,
    "upstreams": [],
    "model_routes": [],
    "log_max_bytes": int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
    "log_level": os.getenv("LOG_LEVEL", "WARNING").upper(),
    "model_cache_ttl": int(os.getenv("MODEL_CACHE_TTL", "60")),
    "api_tokens": [],
    "export_history": [],
    "api_usage": [],
    "appearance_background": "",
    "appearance_backgrounds": [],
    "appearance_glass_opacity": 49,
    "appearance_glass_brightness": 32,
    "appearance_glass_blur": 13,
    "appearance_mask_opacity": 69,
}

app = FastAPI(title="CleanLLM", version="1.0.58")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
OLLAMA_TASKS: dict[str, dict[str, Any]] = {}
OLLAMA_HANDLES: dict[str, asyncio.Task] = {}
MODEL_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "source": ""}
STATUS_EVENTS: asyncio.Queue = asyncio.Queue(maxsize=20)
API_USAGE: list[dict[str, Any]] = []


class LoginRequest(BaseModel):
    username: str = "admin"
    password: str


class SettingsUpdate(BaseModel):
    target_api_url: HttpUrl
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    models_api_url: str = ""
    ollama_api_url: str = ""
    clean_patterns: list[str] = Field(default_factory=list, max_length=30)
    log_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1 * 1024 * 1024, le=50 * 1024 * 1024)
    upstreams: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    model_routes: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    model_cache_ttl: int = Field(default=60, ge=0, le=86400)
    log_level: str = Field(default="WARNING", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    appearance_background: str = Field(default="", max_length=7_000_000)
    appearance_backgrounds: list[str] = Field(default_factory=list, max_length=20)
    appearance_glass_opacity: int = Field(default=49, ge=0, le=100)
    appearance_glass_brightness: int = Field(default=32, ge=0, le=100)
    appearance_glass_blur: int = Field(default=13, ge=0, le=100)
    appearance_mask_opacity: int = Field(default=69, ge=0, le=100)

    @field_validator("models_api_url", "ollama_api_url")
    @classmethod
    def validate_models_url(cls, value: str) -> str:
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("接口地址必须是有效的 HTTP/HTTPS URL")
        return value

    @field_validator("clean_patterns")
    @classmethod
    def validate_patterns(cls, patterns: list[str]) -> list[str]:
        cleaned: list[str] = []
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern:
                continue
            if len(pattern) > 2000:
                raise ValueError("单条正则表达式不能超过 2000 个字符")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"无效正则表达式：{pattern}（{exc}）") from exc
            cleaned.append(pattern)
        return cleaned


class AccountUpdate(BaseModel):
    current_password: str
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    new_password: str = Field(min_length=8, max_length=128)


class OllamaModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ConfigImportRequest(BaseModel):
    settings: dict[str, Any]


class OllamaCopyRequest(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)


class OllamaKeepAliveRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    keep_alive: str = Field(default="5m", max_length=20)


class OllamaImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    modelfile: str = Field(default="", max_length=2_000_000)


class ApiTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def publish_status(event: str, data: dict[str, Any]) -> None:
    try:
        STATUS_EVENTS.put_nowait({"event": event, "data": data, "id": int(time.time() * 1000)})
    except asyncio.QueueFull:
        try:
            STATUS_EVENTS.get_nowait()
            STATUS_EVENTS.put_nowait({"event": event, "data": data, "id": int(time.time() * 1000)})
        except asyncio.QueueEmpty:
            pass


class CappedFileHandler(logging.FileHandler):
    def __init__(self, filename: Path, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(filename, encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            if self.stream and self.stream.tell() > self.max_bytes:
                self.stream.flush()
                self.stream.close()
                keep_bytes = self.max_bytes // 2
                with open(self.baseFilename, "rb") as source:
                    source.seek(max(0, os.path.getsize(self.baseFilename) - keep_bytes))
                    tail = source.read()
                newline = tail.find(b"\n")
                if newline >= 0:
                    tail = tail[newline + 1 :]
                with open(self.baseFilename, "wb") as target:
                    target.write(b"--- older log entries trimmed (5 MB limit) ---\n")
                    target.write(tail)
                self.stream = self._open()
        except OSError:
            self.handleError(record)


def configure_file_logging() -> None:
    if any(isinstance(handler, CappedFileHandler) for handler in logger.handlers):
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handler = CappedFileHandler(LOG_FILE, MAX_LOG_BYTES)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "WARNING").upper(), logging.WARNING))
        logger.propagate = True
    except OSError as exc:
        logging.getLogger("uvicorn.error").warning("File logging disabled: %s", exc)


configure_file_logging()


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "admin")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_text, digest_text = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def configured_credentials() -> tuple[str, str | None]:
    settings = load_settings()
    password_hash = settings.get("admin_password_hash")
    if isinstance(password_hash, str) and password_hash:
        return str(settings.get("admin_username") or "admin"), password_hash
    return admin_username(), None


def credentials_valid(username: str, password: str) -> bool:
    configured_username, password_hash = configured_credentials()
    username_ok = secrets.compare_digest(username.encode(), configured_username.encode())
    if password_hash:
        return username_ok and verify_password(password, password_hash)
    password_ok = bool(admin_password()) and secrets.compare_digest(password.encode(), admin_password().encode())
    return username_ok and password_ok


def session_secret() -> bytes:
    value = os.getenv("SESSION_SECRET") or admin_password()
    return value.encode("utf-8")


def auth_revision() -> str:
    return str(load_settings().get("_auth_revision") or "environment")


def create_session_token() -> str:
    timestamp = str(int(time.time()))
    revision = auth_revision()
    message = f"{timestamp}.{revision}"
    signature = hmac.new(session_secret(), message.encode(), hashlib.sha256).hexdigest()
    return f"{message}.{signature}"


def valid_session(token: str | None) -> bool:
    if not token or not session_secret():
        return False
    try:
        timestamp_text, revision, signature = token.split(".", 2)
        timestamp = int(timestamp_text)
    except (ValueError, TypeError):
        return False
    if timestamp > time.time() + 60 or time.time() - timestamp > SESSION_MAX_AGE:
        return False
    if not secrets.compare_digest(revision, auth_revision()):
        return False
    expected = hmac.new(session_secret(), f"{timestamp_text}.{revision}".encode(), hashlib.sha256).hexdigest()
    return secrets.compare_digest(signature, expected)


def require_admin(request: Request) -> None:
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_cipher(token: str, token_id: str) -> str:
    key = hashlib.sha256(session_secret() + token_id.encode()).digest()
    raw = token.encode()
    encrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(raw))
    return base64.urlsafe_b64encode(encrypted).decode()


def token_plain(cipher: str, token_id: str) -> str:
    key = hashlib.sha256(session_secret() + token_id.encode()).digest()
    encrypted = base64.urlsafe_b64decode(cipher.encode())
    return bytes(value ^ key[index % len(key)] for index, value in enumerate(encrypted)).decode()


def require_api_token(request: Request) -> None:
    settings = load_settings()
    configured = [item for item in settings.get("api_tokens", []) if isinstance(item, dict) and item.get("hash")]
    if not configured:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    digest = token_digest(supplied) if supplied else ""
    matched = next((item for item in configured if secrets.compare_digest(digest, str(item.get("hash")))), None)
    if not matched:
        raise HTTPException(status_code=401, detail="API 访问令牌无效或缺失")
    matched["last_used_at"] = int(time.time())
    save_settings(settings)
    usage_item = {"token_id": matched.get("id"), "token_name": matched.get("name", ""), "at": int(time.time()), "path": request.url.path, "method": request.method}
    API_USAGE.append(usage_item); del API_USAGE[:-500]
    settings.setdefault("api_usage", []).append(usage_item); settings["api_usage"] = settings["api_usage"][-1000:]; save_settings(settings)


def load_settings() -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    settings["clean_patterns"] = DEFAULT_PATTERNS.copy()
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update(saved)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load settings: %s", exc)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_FILE.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(SETTINGS_FILE)
        configured_limit = max(1 * 1024 * 1024, min(int(settings.get("log_max_bytes", MAX_LOG_BYTES)), 50 * 1024 * 1024))
        for handler in logger.handlers:
            if isinstance(handler, CappedFileHandler):
                handler.max_bytes = configured_limit
            logger.setLevel(getattr(logging, str(settings.get("log_level", "WARNING")).upper(), logging.WARNING))
    except OSError as exc:
        logger.exception("Could not save settings")
        raise HTTPException(
            status_code=500, detail=f"无法保存设置，请检查数据卷写入权限：{exc}"
        ) from exc


def clean_content(text: str, settings: dict[str, Any]) -> str:
    for pattern in settings.get("clean_patterns", []):
        try:
            text = re.sub(pattern, "", text)
        except re.error as exc:
            logger.warning("Skipping invalid saved regex %r: %s", pattern, exc)
    return text.strip()


def models_url(settings: dict[str, Any]) -> str:
    override = str(settings.get("models_api_url") or "").strip()
    if override:
        return override
    parsed = urlsplit(str(settings["target_api_url"]))
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] + "/models"
            break
    else:
        path = path.rsplit("/", 1)[0] + "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def ollama_base_url(settings: dict[str, Any]) -> str:
    override = str(settings.get("ollama_api_url") or "").strip()
    if override:
        return override.rstrip("/")
    parsed = urlsplit(str(settings["target_api_url"]))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def ollama_manifest_path(model: str) -> Path:
    reference, separator, tag = model.rpartition(":")
    if not separator or "/" in tag:
        reference, tag = model, "latest"
    parts = [part for part in reference.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=400, detail="模型名称无效")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry, parts = parts[0], parts[1:]
    else:
        registry = "registry.ollama.ai"
    if len(parts) == 1:
        namespace, repository = "library", parts[0]
    elif len(parts) == 2:
        namespace, repository = parts
    else:
        raise HTTPException(status_code=400, detail="暂不支持该模型名称结构")
    return OLLAMA_MODELS_DIR / "manifests" / registry / namespace / repository / tag


def configured_upstreams(settings: dict[str, Any]) -> list[dict[str, Any]]:
    primary = {
        "name": "默认上游",
        "url": str(settings["target_api_url"]),
        "api_key": str(settings.get("api_key") or ""),
        "timeout": int(settings.get("timeout_seconds") or 120),
    }
    result = [primary]
    for item in settings.get("upstreams", []):
        if not isinstance(item, dict) or not item.get("url"):
            continue
        parsed = urlsplit(str(item["url"]))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        result.append({
            "name": str(item.get("name") or f"上游 {len(result) + 1}"),
            "url": str(item["url"]),
            "api_key": str(item.get("api_key") or ""),
            "timeout": max(1, min(int(item.get("timeout") or 120), 3600)),
        })
    return result


def route_upstreams(settings: dict[str, Any], model: str) -> list[dict[str, Any]]:
    upstreams = configured_upstreams(settings)
    names = {item["name"]: item for item in upstreams}
    preferred: list[dict[str, Any]] = []
    for route in settings.get("model_routes", []):
        if not isinstance(route, dict) or not fnmatch.fnmatchcase(model, str(route.get("pattern") or "")):
            continue
        for name in route.get("upstreams", []):
            if name in names and names[name] not in preferred:
                preferred.append(names[name])
        break
    return preferred + [item for item in upstreams if item not in preferred]


def clean_stream_line(line: str, settings: dict[str, Any]) -> str:
    if not line.startswith("data: ") or line.strip() == "data: [DONE]":
        return line
    try:
        payload = json.loads(line[6:])
        for choice in payload.get("choices", []):
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                delta["content"] = clean_content(delta["content"], settings)
        return "data: " + json.dumps(payload, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return line


def tail_log(limit: int) -> list[str]:
    try:
        if not LOG_FILE.exists():
            return []
        with LOG_FILE.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - min(size, 512 * 1024)))
            text = handle.read().decode("utf-8", errors="replace")
        return text.splitlines()[-limit:]
    except OSError as exc:
        logger.warning("Could not read log file: %s", exc)
        return []


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看容器日志"})


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s failed", request.method, request.url.path)
        raise
    if request.url.path != "/health":
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("%s %s -> %s (%.1f ms)", request.method, request.url.path, response.status_code, elapsed_ms)
        publish_status("request", {"path": request.url.path, "status": response.status_code, "latency_ms": round(elapsed_ms)})
    return response


@app.get("/", include_in_schema=False)
async def index(request: Request):
    # Always serve the shell first; protected API calls perform the session check.
    # This prevents browsers from flashing the legacy login document during refresh.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})

@app.get("/api/appearance", include_in_schema=False)
async def public_appearance() -> dict[str, str]:
    """Expose only the selected background so the login page can match the instance theme."""
    return {"background": str(load_settings().get("appearance_background") or "")}


@app.post("/api/login")
async def login(login_data: LoginRequest) -> JSONResponse:
    _, password_hash = configured_credentials()
    if not password_hash and not admin_password():
        raise HTTPException(status_code=503, detail="尚未配置初始 ADMIN_PASSWORD")
    if not credentials_valid(login_data.username, login_data.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response = JSONResponse({"message": "登录成功"})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"message": "已退出登录"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings")
async def get_settings(_: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    return {key: settings[key] for key in DEFAULT_SETTINGS if key not in {"api_tokens", "export_history", "api_usage"}}


@app.put("/api/settings")
async def update_settings(
    update: SettingsUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    settings = load_settings()
    settings.update(update.model_dump(mode="json"))
    save_settings(settings)
    return {"message": "设置已保存"}


@app.post("/api/settings/test")
async def test_connections(_: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    results = []
    async with httpx.AsyncClient() as client:
        for upstream in configured_upstreams(settings):
            headers = {"Authorization": f"Bearer {upstream['api_key']}"} if upstream["api_key"] else {}
            url = upstream["url"]
            parsed = urlsplit(url)
            probe = urlunsplit((parsed.scheme, parsed.netloc, "/v1/models", "", ""))
            started = time.perf_counter()
            try:
                response = await client.get(probe, headers=headers, timeout=8.0)
                response.raise_for_status()
                results.append({"name": upstream["name"], "ok": True, "latency_ms": round((time.perf_counter()-started)*1000), "detail": "模型接口正常"})
            except Exception as exc:
                results.append({"name": upstream["name"], "ok": False, "latency_ms": round((time.perf_counter()-started)*1000), "detail": str(exc)})
        try:
            response = await client.get(f"{ollama_base_url(settings)}/api/version", timeout=5.0)
            response.raise_for_status()
            results.append({"name": "Ollama 管理接口", "ok": True, "detail": response.json().get("version", "正常")})
        except Exception as exc:
            results.append({"name": "Ollama 管理接口", "ok": False, "detail": str(exc)})
    return {"results": results}


@app.get("/api/settings/export")
async def export_settings(_: None = Depends(require_admin)) -> Response:
    settings = load_settings()
    safe = {key: value for key, value in settings.items() if key in DEFAULT_SETTINGS and key not in {"api_tokens", "export_history", "api_usage"}}
    safe["api_key"] = ""
    for upstream in safe.get("upstreams", []):
        if isinstance(upstream, dict):
            upstream["api_key"] = ""
    payload = json.dumps({"version": 1, "settings": safe}, ensure_ascii=False, indent=2)
    return Response(payload, media_type="application/json", headers={"Content-Disposition": "attachment; filename=cleanllm-settings.json"})


@app.post("/api/settings/import")
async def import_settings(update: ConfigImportRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    current = load_settings()
    candidate = {key: update.settings.get(key, current.get(key)) for key in DEFAULT_SETTINGS}
    validated = SettingsUpdate.model_validate(candidate).model_dump(mode="json")
    current.update(validated)
    save_settings(current)
    return {"message": "配置已导入"}


@app.post("/api/settings/reset")
async def reset_settings(_: None = Depends(require_admin)) -> dict[str, str]:
    current = load_settings()
    preserved = {key: current[key] for key in ("admin_username", "admin_password_hash", "_auth_revision") if key in current}
    reset = DEFAULT_SETTINGS.copy()
    reset["clean_patterns"] = DEFAULT_PATTERNS.copy()
    reset.update(preserved)
    save_settings(reset)
    return {"message": "代理设置已恢复默认值"}


@app.get("/api/account")
async def get_account(_: None = Depends(require_admin)) -> dict[str, str]:
    username, _ = configured_credentials()
    return {"username": username}


@app.put("/api/account")
async def update_account(
    update: AccountUpdate, _: None = Depends(require_admin)
) -> JSONResponse:
    current_username, _ = configured_credentials()
    if not credentials_valid(current_username, update.current_password):
        raise HTTPException(status_code=403, detail="当前密码错误")
    settings = load_settings()
    settings["admin_username"] = update.username
    settings["admin_password_hash"] = hash_password(update.new_password)
    settings["_auth_revision"] = secrets.token_hex(16)
    save_settings(settings)
    response = JSONResponse({"message": "账户已更新，请重新登录"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/tokens")
async def list_api_tokens(_: None = Depends(require_admin)) -> dict[str, Any]:
    items = load_settings().get("api_tokens", [])
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token = ""
        try:
            token = token_plain(str(item.get("cipher") or ""), str(item.get("id"))) if item.get("cipher") else ""
        except (ValueError, UnicodeDecodeError):
            pass
        result.append({"id": str(item.get("id")), "name": str(item.get("name")), "token": token, "created_at": item.get("created_at"), "last_used_at": item.get("last_used_at")})
    return {"data": result}


@app.post("/api/tokens")
async def create_api_token(update: ApiTokenRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    raw = "cln_" + secrets.token_urlsafe(24)
    settings = load_settings()
    token_id = uuid.uuid4().hex
    item = {"id": token_id, "name": update.name.strip(), "hash": token_digest(raw), "cipher": token_cipher(raw, token_id), "created_at": int(time.time()), "last_used_at": None}
    settings.setdefault("api_tokens", []).append(item)
    save_settings(settings)
    return {"id": item["id"], "name": item["name"], "token": raw, "created_at": item["created_at"]}


@app.delete("/api/tokens/{token_id}")
async def revoke_api_token(token_id: str, _: None = Depends(require_admin)) -> dict[str, str]:
    settings = load_settings(); before = len(settings.get("api_tokens", []))
    settings["api_tokens"] = [i for i in settings.get("api_tokens", []) if not (isinstance(i, dict) and str(i.get("id")) == token_id)]
    if len(settings["api_tokens"]) == before: raise HTTPException(status_code=404, detail="令牌不存在")
    save_settings(settings); return {"message": "令牌已撤销"}


@app.get("/api/usage")
async def api_usage(_: None = Depends(require_admin)) -> dict[str, Any]:
    now = int(time.time()); recent = [item for item in load_settings().get("api_usage", API_USAGE) if now - int(item.get("at", 0)) < 30 * 86400]
    local_now = datetime.now().astimezone()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    def in_period(item: dict[str, Any], start: datetime, end: datetime | None = None) -> bool:
        at = datetime.fromtimestamp(int(item.get("at", 0)), local_now.tzinfo)
        return at >= start and (end is None or at < end)
    by_token: dict[str, int] = {}
    for item in recent:
        key = str(item.get("token_name") or "未命名"); by_token[key] = by_token.get(key, 0) + 1
    return {
        "total": len(recent),
        "today": sum(1 for item in recent if in_period(item, today_start)),
        "yesterday": sum(1 for item in recent if in_period(item, yesterday_start, today_start)),
        "week": sum(1 for item in recent if in_period(item, week_start)),
        "month": sum(1 for item in recent if in_period(item, month_start)),
        "by_token": by_token,
        "logs": list(reversed(recent[-100:])),
    }


@app.get("/api/system/events")
async def system_events(_: None = Depends(require_admin)) -> StreamingResponse:
    async def stream():
        yield "event: connected\ndata: {\"status\":\"ok\"}\n\n"
        while True:
            try:
                item = await asyncio.wait_for(STATUS_EVENTS.get(), timeout=25)
                yield f"id: {item['id']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


@app.get("/api/models")
async def get_models(refresh: bool = False, _: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    url = models_url(settings)
    ttl = max(0, int(settings.get("model_cache_ttl", 60)))
    if not refresh and MODEL_CACHE.get("data") is not None and time.time() - float(MODEL_CACHE.get("at", 0)) < ttl and MODEL_CACHE.get("source") == url:
        return {"source": url, "count": len(MODEL_CACHE["data"]), "cached": True, "data": MODEL_CACHE["data"]}
    headers: dict[str, str] = {"Accept": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, headers=headers, timeout=min(float(settings["timeout_seconds"]), 30.0)
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        logger.warning("Model discovery failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"获取上游模型失败：{exc}") from exc
    raw_models = payload.get("data", []) if isinstance(payload, dict) else []
    if not raw_models and isinstance(payload, dict):
        raw_models = payload.get("models", [])
    models: list[dict[str, Any]] = []
    for item in raw_models if isinstance(raw_models, list) else []:
        if isinstance(item, str):
            models.append({"id": item, "owned_by": "upstream", "created": None})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if model_id:
                models.append(
                    {
                        "id": str(model_id),
                        "owned_by": str(item.get("owned_by") or item.get("owner") or "upstream"),
                        "created": item.get("created") or item.get("modified_at"),
                    }
                )
    models.sort(key=lambda item: item["id"].lower())
    MODEL_CACHE.update({"at": time.time(), "data": models, "source": url})
    logger.info("Discovered %d upstream models from %s", len(models), url)
    return {"source": url, "count": len(models), "cached": False, "data": models}


@app.get("/api/ollama/status")
async def get_ollama_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    base_url = ollama_base_url(load_settings())
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/api/version", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
        return {"available": True, "version": payload.get("version", "未知"), "base_url": base_url}
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        logger.info("Ollama is unavailable at %s: %s", base_url, exc)
        return {"available": False, "version": None, "base_url": base_url}


@app.get("/api/ollama/models")
async def get_ollama_models(_: None = Depends(require_admin)) -> dict[str, Any]:
    base_url = ollama_base_url(load_settings())
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{base_url}/api/tags", timeout=15.0)
            response.raise_for_status()
            payload = response.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"无法获取 Ollama 模型：{exc}") from exc
    data = []
    for item in payload.get("models", []) if isinstance(payload, dict) else []:
        if isinstance(item, dict) and (item.get("name") or item.get("model")):
            data.append({
                "id": str(item.get("name") or item.get("model")),
                "size": item.get("size"),
                "digest": item.get("digest"),
                "modified_at": item.get("modified_at"),
                "details": item.get("details") or {},
            })
    data.sort(key=lambda item: item["id"].lower())
    return {"source": base_url, "count": len(data), "data": data}


@app.post("/api/ollama/pull")
async def pull_ollama_model(
    update: OllamaModelRequest, _: None = Depends(require_admin)
) -> StreamingResponse:
    base_url = ollama_base_url(load_settings())
    logger.info("Pulling Ollama model %s", update.model)

    async def stream():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{base_url}/api/pull", json={"name": update.model, "stream": True}
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except Exception as exc:
            logger.warning("Ollama pull failed for %s: %s", update.model, exc)
            yield (json.dumps({"error": f"拉取失败：{exc}"}, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


async def run_ollama_pull(task_id: str, model: str) -> None:
    task = OLLAMA_TASKS[task_id]
    task.update(status="running", updated_at=int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{ollama_base_url(load_settings())}/api/pull", json={"name": model, "stream": True}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    item = json.loads(line)
                    task.update(message=item.get("status", "处理中"), completed=item.get("completed", 0), total=item.get("total", 0), updated_at=int(time.time()))
        task.update(status="completed", message="拉取完成", updated_at=int(time.time()))
    except asyncio.CancelledError:
        task.update(status="cancelled", message="已取消", updated_at=int(time.time()))
    except Exception as exc:
        task.update(status="failed", message=str(exc), updated_at=int(time.time()))


@app.post("/api/ollama/tasks")
async def create_ollama_task(update: OllamaModelRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    OLLAMA_TASKS[task_id] = {"id": task_id, "model": update.model, "status": "queued", "message": "等待开始", "completed": 0, "total": 0, "created_at": int(time.time()), "updated_at": int(time.time())}
    OLLAMA_HANDLES[task_id] = asyncio.create_task(run_ollama_pull(task_id, update.model))
    return OLLAMA_TASKS[task_id]


@app.get("/api/ollama/tasks")
async def list_ollama_tasks(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"data": sorted(OLLAMA_TASKS.values(), key=lambda item: item["created_at"], reverse=True)}


@app.post("/api/ollama/tasks/{task_id}/cancel")
async def cancel_ollama_task(task_id: str, _: None = Depends(require_admin)) -> dict[str, str]:
    handle = OLLAMA_HANDLES.get(task_id)
    if not handle or handle.done():
        raise HTTPException(status_code=409, detail="任务已结束或不存在")
    handle.cancel()
    return {"message": "取消请求已发送"}


@app.post("/api/ollama/tasks/{task_id}/retry")
async def retry_ollama_task(task_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    old = OLLAMA_TASKS.get(task_id)
    if not old or old["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="只有失败或取消的任务可以重试")
    return await create_ollama_task(OllamaModelRequest(model=old["model"]), None)


@app.get("/api/ollama/export")
async def export_ollama_model_safe(model: str, _: None = Depends(require_admin)) -> Response:
    return await export_ollama_model(model, None)


@app.get("/api/ollama/archive")
async def export_ollama_archive(model: str, _: None = Depends(require_admin)) -> FileResponse:
    manifest = ollama_manifest_path(model)
    try:
        manifest = manifest.resolve(strict=True)
        root = OLLAMA_MODELS_DIR.resolve(strict=True)
        manifest.relative_to(root)
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail=f"无法读取模型文件。请将 Ollama models 目录只读挂载到 /ollama-models：{exc}") from exc
    digests = []
    config = manifest_data.get("config", {})
    if isinstance(config, dict) and config.get("digest"):
        digests.append(str(config["digest"]))
    for layer in manifest_data.get("layers", []):
        if isinstance(layer, dict) and layer.get("digest"):
            digests.append(str(layer["digest"]))
    blobs = []
    for digest in dict.fromkeys(digests):
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            raise HTTPException(status_code=400, detail="模型 manifest 包含无效 blob 摘要")
        blob = root / "blobs" / digest.replace(":", "-")
        if not blob.is_file():
            raise HTTPException(status_code=404, detail=f"模型 blob 不完整：{digest}")
        blobs.append(blob)
    temporary = tempfile.NamedTemporaryFile(prefix="cleanllm-model-", suffix=".tar.gz", delete=False, dir=DATA_DIR)
    temporary.close()
    archive_path = Path(temporary.name)
    def build_archive() -> None:
        with tarfile.open(archive_path, "w:gz", compresslevel=1) as archive:
            archive.add(manifest, arcname=str(manifest.relative_to(root)))
            for blob in blobs:
                archive.add(blob, arcname=str(blob.relative_to(root)))
            metadata = json.dumps({"format": "cleanllm-ollama-archive-v1", "model": model}, ensure_ascii=False).encode()
            info = tarfile.TarInfo("cleanllm-model.json")
            info.size = len(metadata)
            archive.addfile(info, io.BytesIO(metadata))
    try:
        # Tar/gzip of multi-gigabyte blobs must not block FastAPI's event loop.
        await asyncio.to_thread(build_archive)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", model) + ".ollama.tar.gz"
    settings = load_settings()
    history = settings.setdefault("export_history", [])
    history.insert(0, {"id": uuid.uuid4().hex, "model": model, "filename": filename, "created_at": int(time.time()), "size": archive_path.stat().st_size})
    settings["export_history"] = history[:100]
    save_settings(settings)
    return FileResponse(archive_path, filename=filename, media_type="application/gzip", background=BackgroundTask(archive_path.unlink, missing_ok=True))


@app.get("/api/ollama/export-history")
async def export_history(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"data": load_settings().get("export_history", [])}


@app.get("/api/ollama/models/{model:path}")
async def show_ollama_model(model: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(f"{ollama_base_url(load_settings())}/api/show", json={"name": model}, timeout=20.0)
            response.raise_for_status()
            return response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"获取模型详情失败：{exc}") from exc


@app.post("/api/ollama/copy")
async def copy_ollama_model(update: OllamaCopyRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(f"{ollama_base_url(load_settings())}/api/copy", json={"source": update.source, "destination": update.destination}, timeout=30.0)
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise HTTPException(status_code=502, detail=f"复制模型失败：{exc}") from exc
    return {"message": f"已复制为 {update.destination}"}


@app.post("/api/ollama/keep-alive")
async def keep_alive_ollama_model(update: OllamaKeepAliveRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(f"{ollama_base_url(load_settings())}/api/generate", json={"model": update.model, "keep_alive": update.keep_alive, "stream": False}, timeout=30.0)
            response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise HTTPException(status_code=502, detail=f"模型加载状态修改失败：{exc}") from exc
    return {"message": "模型已卸载" if update.keep_alive == "0" else "模型已加载"}


@app.get("/api/ollama/models/{model:path}/export")
async def export_ollama_model(model: str, _: None = Depends(require_admin)) -> Response:
    # /api/show is optional in Ollama-compatible servers; export a portable
    # definition directly so export never fails because of a 400 response.
    detail = {"modelfile": f"FROM {model}\n"}
    payload = json.dumps({
        "id": model, "name": model, "object": "model", "created": 0, "owned_by": "ollama",
        "ollama": {"name": model, "model": model, "modified_at": None, "size": None, "digest": None, "details": {}, "connection_type": "local", "urls": []},
        "loaded": False, "connection_type": "local", "tags": [], "actions": [], "filters": [], "is_active": True,
        "cleanllm": {"format": "ollama-metadata-v1", "modelfile": detail.get("modelfile", ""), "parameters": detail.get("parameters", ""), "template": detail.get("template", ""), "system": detail.get("system", "")}
    }, ensure_ascii=False, indent=2)
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", model) + ".cleanllm-model.json"
    return Response(payload, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/ollama/models/import")
async def import_ollama_model(update: OllamaImportRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            modelfile = update.modelfile.strip() or f"FROM {update.name}\n"
            response = await client.post(f"{ollama_base_url(load_settings())}/api/create", json={"model": update.name, "modelfile": modelfile, "stream": False})
            response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise HTTPException(status_code=502, detail=f"导入模型定义失败：{exc}") from exc
    return {"message": f"模型 {update.name} 已导入"}


@app.get("/api/changelog")
async def get_changelog(_: None = Depends(require_admin)) -> dict[str, str]:
    path = BASE_DIR / "CHANGELOG.md"
    return {"content": path.read_text(encoding="utf-8") if path.exists() else "暂无更新记录"}


@app.post("/api/system/restart")
async def restart_container(_: None = Depends(require_admin)) -> dict[str, str]:
    logger.warning("Container restart requested from Web UI")
    docker_socket = Path("/var/run/docker.sock")
    if docker_socket.exists():
        container_id = socket.gethostname()
        async def restart_with_docker() -> None:
            def call_engine() -> bool:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.settimeout(5)
                    client.connect(str(docker_socket))
                    for target in (container_id, "cleanllm"):
                        request = f"POST /v1.41/containers/{target}/restart?t=10 HTTP/1.1\r\nHost: docker\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                        client.sendall(request.encode("ascii"))
                        response = client.recv(4096)
                        if b" 204 " in response or b" 200 " in response:
                            return True
                        client.close(); client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); client.settimeout(5); client.connect(str(docker_socket))
                    return False
                finally:
                    client.close()
            try:
                restarted = await asyncio.to_thread(call_engine)
            except OSError:
                restarted = False
            if not restarted:
                logger.error("Docker Engine restart request failed; falling back to process restart")
                os.kill(os.getpid(), signal.SIGTERM)
        asyncio.create_task(restart_with_docker())
        return {"message": "Docker 已收到重启请求，页面将在数秒后恢复"}
    asyncio.get_running_loop().call_later(0.4, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"message": "正在退出当前进程，请由容器重启策略自动拉起"}


@app.delete("/api/ollama/models")
async def delete_ollama_model(
    update: OllamaModelRequest, _: None = Depends(require_admin)
) -> dict[str, str]:
    base_url = ollama_base_url(load_settings())
    try:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                "DELETE", f"{base_url}/api/delete", json={"name": update.model}, timeout=30.0
            )
            response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise HTTPException(status_code=502, detail=f"删除 Ollama 模型失败：{exc}") from exc
    logger.info("Deleted Ollama model %s", update.model)
    return {"message": f"模型 {update.model} 已删除"}


@app.get("/api/logs")
async def get_logs(
    limit: int = 500, _: None = Depends(require_admin)
) -> dict[str, Any]:
    limit = max(1, min(limit, 2000))
    try:
        size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    except OSError:
        size = 0
    return {
        "lines": tail_log(limit),
        "size_bytes": size,
        "max_bytes": int(load_settings().get("log_max_bytes", MAX_LOG_BYTES)),
    }


@app.delete("/api/logs")
async def clear_logs(_: None = Depends(require_admin)) -> dict[str, str]:
    try:
        LOG_FILE.write_text("", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"清除日志失败：{exc}") from exc
    logger.info("System log cleared from Web UI")
    return {"message": "系统日志已清除"}


@app.post("/v1/chat/completions")
async def proxy_api(request: Request, _: None = Depends(require_api_token)) -> Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    settings = load_settings()
    streaming = payload.get("stream") is True
    failures = []
    response = None
    selected = None
    client = None
    for upstream in route_upstreams(settings, str(payload.get("model") or "")):
        headers = {"Content-Type": "application/json"}
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        try:
            if streaming:
                candidate_client = httpx.AsyncClient(timeout=None)
                candidate = await candidate_client.send(candidate_client.build_request("POST", upstream["url"], json=payload, headers=headers), stream=True)
                if candidate.status_code >= 500:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    await candidate.aclose()
                    await candidate_client.aclose()
                    continue
                client, response, selected = candidate_client, candidate, upstream
            else:
                async with httpx.AsyncClient() as candidate_client:
                    candidate = await candidate_client.post(upstream["url"], json=payload, headers=headers, timeout=float(upstream["timeout"]))
                if candidate.status_code >= 500:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    continue
                response, selected = candidate, upstream
            break
        except httpx.RequestError as exc:
            failures.append(f"{upstream['name']}: {exc}")
    if response is None:
        return JSONResponse(status_code=502, content={"error": {"message": "所有上游均不可用：" + "；".join(failures), "type": "upstream_error"}})
    logger.info("Model %s routed to %s", payload.get("model", ""), selected["name"])
    if streaming:
        async def stream_response():
            buffer = ""
            try:
                async for text in response.aiter_text():
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        yield (clean_stream_line(line, settings) + "\n").encode("utf-8")
                if buffer:
                    yield clean_stream_line(buffer, settings).encode("utf-8")
            finally:
                await response.aclose()
                await client.aclose()
        return StreamingResponse(stream_response(), status_code=response.status_code, media_type=response.headers.get("content-type", "text/event-stream"))
    try:
        data = response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={
                "error": {"message": "上游返回了非 JSON 内容", "type": "upstream_error"}
            },
        )
    choices = data.get("choices", []) if isinstance(data, dict) else []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            message["content"] = clean_content(content, settings)
    return JSONResponse(status_code=response.status_code, content=data)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=11515)
