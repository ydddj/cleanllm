import hashlib
import hmac
import base64
import asyncio
import copy
import fnmatch
import json
import logging
import os
import re
import secrets
import tempfile
import time
import uuid
import signal
import socket
import sqlite3
import tarfile
import threading
import io
from contextlib import asynccontextmanager
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
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

logger = logging.getLogger("cleanllm")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "cleanllm.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
STATIC_DIR = BASE_DIR / "static"
VERSION_FILE = BASE_DIR / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.0.0"
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
    "default_upstream_name": "默认上游",
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
    "connectivity_interval_minutes": int(os.getenv("CONNECTIVITY_INTERVAL_MINUTES", "10")),
    "route_affinity_minutes": int(os.getenv("ROUTE_AFFINITY_MINUTES", "15")),
    "appearance_background": "",
    "appearance_backgrounds": [],
    "appearance_glass_opacity": 50,
    "appearance_glass_brightness": 45,
    "appearance_glass_blur": 10,
    "appearance_mask_opacity": 50,
}
RUNTIME_SETTINGS_KEYS = {"connectivity_history", "api_tokens", "export_history", "api_usage"}


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    global CONNECTIVITY_TASK
    history = database_connectivity_history()
    if history:
        CONNECTIVITY_STATE["checked_at"] = history[-1]["ts"]
    CONNECTIVITY_TASK = asyncio.create_task(connectivity_scheduler())
    try:
        yield
    finally:
        CONNECTIVITY_TASK.cancel()
        try:
            await CONNECTIVITY_TASK
        except asyncio.CancelledError:
            pass
        CONNECTIVITY_TASK = None


app = FastAPI(title="CleanLLM", version=APP_VERSION, lifespan=app_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
OLLAMA_TASKS: dict[str, dict[str, Any]] = {}
OLLAMA_HANDLES: dict[str, asyncio.Task] = {}
MODEL_CACHE: dict[str, Any] = {"at": 0.0, "data": None, "source": ""}
CONNECTIVITY_STATE: dict[str, Any] = {"checked_at": None, "results": [], "availability_percent": None}
CONNECTIVITY_LOCK = asyncio.Lock()
CONNECTIVITY_TASK: asyncio.Task | None = None
STATUS_EVENTS: asyncio.Queue = asyncio.Queue(maxsize=20)
ROUTE_AFFINITY: dict[str, tuple[str, float]] = {}
INITIALIZED_DATABASES: set[str] = set()
DATABASE_INIT_LOCK = threading.Lock()


class LoginRequest(BaseModel):
    username: str
    password: str


class SettingsUpdate(BaseModel):
    target_api_url: HttpUrl
    default_upstream_name: str = Field(default="默认上游", min_length=1, max_length=80)
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    models_api_url: str = ""
    ollama_api_url: str = ""
    clean_patterns: list[str] = Field(default_factory=list, max_length=30)
    log_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1 * 1024 * 1024, le=50 * 1024 * 1024)
    upstreams: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    model_routes: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    model_cache_ttl: int = Field(default=60, ge=0, le=86400)
    connectivity_interval_minutes: int = Field(default=10, ge=1, le=1440)
    route_affinity_minutes: int = Field(default=15, ge=0, le=1440)
    log_level: str = Field(default="WARNING", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    appearance_background: str = Field(default="", max_length=7_000_000)
    appearance_backgrounds: list[str] = Field(default_factory=list, max_length=20)
    appearance_glass_opacity: int = Field(default=50, ge=0, le=100)
    appearance_glass_brightness: int = Field(default=45, ge=0, le=100)
    appearance_glass_blur: int = Field(default=10, ge=0, le=100)
    appearance_mask_opacity: int = Field(default=50, ge=0, le=100)

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

    @field_validator("upstreams")
    @classmethod
    def validate_upstreams(cls, upstreams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(upstreams, start=2):
            if not isinstance(item, dict):
                raise ValueError(f"上游 {index} 必须是对象")
            name = str(item.get("name") or f"上游 {index}").strip()
            if not name or len(name) > 80:
                raise ValueError(f"上游 {index} 名称长度必须为 1 至 80 个字符")
            url = str(item.get("url") or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"上游 {name} 的接口地址无效")
            try:
                timeout = int(item.get("timeout") or 120)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"上游 {name} 的请求超时必须是整数") from exc
            if not 1 <= timeout <= 3600:
                raise ValueError(f"上游 {name} 的请求超时范围为 1 至 3600 秒")
            models_url = cls.validate_models_url(str(item.get("models_url") or ""))
            ollama_url = cls.validate_models_url(str(item.get("ollama_url") or ""))
            patterns = cls.validate_patterns(item.get("clean_patterns") or [])
            routes = cls.validate_model_routes(item.get("model_routes") or [])
            cleaned.append({
                "name": name,
                "url": url,
                "api_key": str(item.get("api_key") or ""),
                "timeout": timeout,
                "models_url": models_url,
                "ollama_url": ollama_url,
                "clean_patterns": patterns,
                "model_routes": routes,
            })
        return cleaned

    @field_validator("model_routes")
    @classmethod
    def validate_model_routes(cls, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for index, route in enumerate(routes, start=1):
            if not isinstance(route, dict):
                raise ValueError(f"模型路由 {index} 必须是对象")
            pattern = str(route.get("pattern") or "").strip()
            names = route.get("upstreams") or []
            if not pattern or len(pattern) > 200:
                raise ValueError(f"模型路由 {index} 必须包含有效 pattern")
            if not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names):
                raise ValueError(f"模型路由 {index} 的 upstreams 必须是名称数组")
            cleaned.append({"pattern": pattern, "upstreams": list(dict.fromkeys(name.strip() for name in names))})
        return cleaned

    @field_validator("appearance_background")
    @classmethod
    def validate_background(cls, value: str) -> str:
        value = str(value or "")
        if value and not value.startswith("data:image/") and not re.fullmatch(
            r"/api/appearance/background/[0-9a-f]{64}\.(?:png|jpg|webp|gif|avif)", value
        ):
            raise ValueError("背景图必须来自本实例图库")
        return value

    @field_validator("appearance_backgrounds")
    @classmethod
    def validate_backgrounds(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(cls.validate_background(value) for value in values if value))

    @model_validator(mode="after")
    def validate_upstream_names(self) -> "SettingsUpdate":
        names = [self.default_upstream_name.strip(), *(item["name"] for item in self.upstreams)]
        if len(names) != len(set(names)):
            raise ValueError("上游名称不能重复")
        self.default_upstream_name = names[0]
        return self


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
    expires_at: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("expires_at")
    @classmethod
    def validate_expiration(cls, value: int | None) -> int | None:
        if value is not None and value <= int(time.time()):
            raise ValueError("过期时间必须晚于当前时间")
        return value

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if any(len(value) > 200 for value in cleaned):
            raise ValueError("单个模型规则不能超过 200 个字符")
        return cleaned


class ApiTokenStatusUpdate(BaseModel):
    enabled: bool


class ApiTokenPolicyUpdate(BaseModel):
    expires_at: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("expires_at")
    @classmethod
    def validate_expiration(cls, value: int | None) -> int | None:
        if value is not None and value <= int(time.time()):
            raise ValueError("过期时间必须晚于当前时间")
        return value

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, values: list[str]) -> list[str]:
        return ApiTokenRequest.validate_allowed_models(values)


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


def database_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "cleanllm.db"
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    key = str(path.resolve())
    if key in INITIALIZED_DATABASES:
        return connection
    with DATABASE_INIT_LOCK:
        if key in INITIALIZED_DATABASES:
            return connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_tokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            hash TEXT NOT NULL UNIQUE,
            cipher TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_used_at INTEGER,
            expires_at INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            allowed_models TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS api_usage (
            id TEXT PRIMARY KEY,
            token_id TEXT,
            token_name TEXT NOT NULL,
            at INTEGER NOT NULL,
            path TEXT NOT NULL,
            method TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            latency_ms REAL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            visible INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(token_id) REFERENCES api_tokens(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_api_usage_at ON api_usage(at);
        CREATE INDEX IF NOT EXISTS idx_api_usage_token ON api_usage(token_id, at);
        CREATE TABLE IF NOT EXISTS connectivity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at REAL NOT NULL,
            results_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_connectivity_checked_at ON connectivity_history(checked_at);
        CREATE TABLE IF NOT EXISTS export_history (
            id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0
        );
            """
        )
        token_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_tokens)")}
        if "allowed_models" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN allowed_models TEXT NOT NULL DEFAULT '[]'")
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_usage)")}
        if "visible" not in usage_columns:
            connection.execute("ALTER TABLE api_usage ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        connection.execute("DELETE FROM api_usage WHERE at < ?", (int(time.time()) - 400 * 86400,))
        connection.execute("DELETE FROM connectivity_history WHERE checked_at < ?", (time.time() - 8 * 86400,))
        INITIALIZED_DATABASES.add(key)
    return connection


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def database_tokens() -> list[dict[str, Any]]:
    with database_connection() as database:
        return [row_dict(row) for row in database.execute("SELECT * FROM api_tokens ORDER BY created_at DESC")]


def database_usage(since: int = 0) -> list[dict[str, Any]]:
    with database_connection() as database:
        rows = database.execute("SELECT * FROM api_usage WHERE at >= ? ORDER BY at", (since,))
        return [row_dict(row) for row in rows]


def database_connectivity_history() -> list[dict[str, Any]]:
    with database_connection() as database:
        rows = database.execute(
            "SELECT checked_at, results_json FROM connectivity_history ORDER BY checked_at DESC LIMIT 12000",
        ).fetchall()
    return [
        {"ts": float(row["checked_at"]), "results": json.loads(row["results_json"])}
        for row in reversed(rows)
    ]


def database_add_connectivity(checked_at: float, results: list[dict[str, Any]]) -> None:
    with database_connection() as database:
        database.execute(
            "INSERT INTO connectivity_history(checked_at, results_json) VALUES (?, ?)",
            (checked_at, json.dumps(results, ensure_ascii=False)),
        )
        database.execute(
            "DELETE FROM connectivity_history WHERE checked_at < ?",
            (checked_at - 8 * 86400,),
        )


def database_export_history() -> list[dict[str, Any]]:
    with database_connection() as database:
        rows = database.execute("SELECT * FROM export_history ORDER BY created_at DESC LIMIT 100").fetchall()
        return [row_dict(row) for row in rows]


def database_add_export(item: dict[str, Any]) -> None:
    with database_connection() as database:
        database.execute(
            "INSERT INTO export_history(id, model, filename, created_at, size) VALUES (?, ?, ?, ?, ?)",
            (item["id"], item["model"], item["filename"], item["created_at"], item["size"]),
        )
        database.execute(
            "DELETE FROM export_history WHERE id NOT IN (SELECT id FROM export_history ORDER BY created_at DESC LIMIT 100)"
        )


def token_is_active(item: dict[str, Any], now: int | None = None) -> bool:
    if not bool(item.get("enabled", 1)):
        return False
    expires_at = item.get("expires_at")
    if expires_at in (None, ""):
        return True
    try:
        return int(expires_at) > (int(time.time()) if now is None else now)
    except (TypeError, ValueError):
        return False


def token_model_rules(item: dict[str, Any]) -> list[str]:
    value = item.get("allowed_models", "[]")
    try:
        rules = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return [str(rule) for rule in rules if str(rule).strip()] if isinstance(rules, list) else []


def token_allows_model(item: dict[str, Any], model: str) -> bool:
    rules = token_model_rules(item)
    return not rules or (bool(model) and any(fnmatch.fnmatchcase(model, rule) for rule in rules))


async def require_api_token(request: Request) -> None:
    now = int(time.time())
    configured = database_tokens()
    if not configured:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
    digest = token_digest(supplied) if supplied else ""
    matched = next(
        (
            item
            for item in configured
            if secrets.compare_digest(digest, str(item.get("hash")))
            and token_is_active(item, now)
        ),
        None,
    )
    if not matched:
        raise HTTPException(status_code=401, detail="API 访问令牌无效或缺失")
    # Keep a lightweight estimate when an upstream does not return usage metadata.
    # The request body is cached by Starlette, so downstream handlers can read it again.
    input_tokens = 0
    model = ""
    try:
        raw_body = await request.body()
        input_tokens = max(0, len(raw_body) // 4)
        body_data = json.loads(raw_body) if raw_body else {}
        if isinstance(body_data, dict):
            model = str(body_data.get("model") or "")
    except Exception:
        pass
    if model and not token_allows_model(matched, model):
        raise HTTPException(status_code=403, detail=f"当前 API令牌无权使用模型：{model}")
    request.state.api_token = matched
    usage_id = uuid.uuid4().hex
    request.state.usage_id = usage_id
    usage_item = {"id": usage_id, "token_id": matched.get("id"), "token_name": matched.get("name", ""), "at": int(time.time()), "path": request.url.path, "method": request.method, "model": model, "latency_ms": None, "input_tokens": input_tokens, "output_tokens": 0, "total_tokens": input_tokens}
    with database_connection() as database:
        database.execute(
            "UPDATE api_tokens SET last_used_at = ?, total_tokens = total_tokens + ? WHERE id = ?",
            (int(time.time()), input_tokens, matched.get("id")),
        )
        database.execute(
            """INSERT INTO api_usage
               (id, token_id, token_name, at, path, method, model, latency_ms, input_tokens, output_tokens, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(usage_item[key] for key in ("id", "token_id", "token_name", "at", "path", "method", "model", "latency_ms", "input_tokens", "output_tokens", "total_tokens")),
        )
        database.execute("DELETE FROM api_usage WHERE at < ?", (now - 400 * 86400,))


def update_usage_record(usage_id: str | None, **values: Any) -> None:
    if not usage_id or not values:
        return
    allowed = {"latency_ms", "input_tokens", "output_tokens", "total_tokens"}
    changes = {key: value for key, value in values.items() if key in allowed}
    if not changes:
        return
    with database_connection() as database:
        current = database.execute(
            "SELECT token_id, total_tokens FROM api_usage WHERE id = ?", (usage_id,)
        ).fetchone()
        if current is None:
            return
        previous_total = max(0, int(current["total_tokens"] or 0))
        assignments = ", ".join(f"{key} = ?" for key in changes)
        database.execute(
            f"UPDATE api_usage SET {assignments} WHERE id = ?",
            (*changes.values(), usage_id),
        )
        if "total_tokens" in changes and current["token_id"]:
            delta = max(0, int(changes["total_tokens"] or 0)) - previous_total
            database.execute(
                "UPDATE api_tokens SET total_tokens = MAX(0, total_tokens + ?) WHERE id = ?",
                (delta, current["token_id"]),
            )


def update_usage_record_safely(usage_id: str | None, **values: Any) -> None:
    """Usage accounting must never interrupt an upstream model response."""
    try:
        update_usage_record(usage_id, **values)
    except Exception as exc:
        logger.warning("Could not persist API usage update: %s", exc)


def usage_values(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict) or not usage:
        return {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def load_settings() -> dict[str, Any]:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings.update({key: value for key, value in saved.items() if key not in RUNTIME_SETTINGS_KEYS})
            original_background = str(settings.get("appearance_background") or "")
            original_backgrounds = list(settings.get("appearance_backgrounds") or [])
            normalize_background_settings(settings)
            embedded_backgrounds = str(settings.get("appearance_background") or "").startswith("data:image/") or any(
                str(value).startswith("data:image/") for value in settings.get("appearance_backgrounds", [])
            )
            backgrounds_changed = original_background != settings.get("appearance_background") or original_backgrounds != settings.get("appearance_backgrounds")
            if any(key in saved for key in RUNTIME_SETTINGS_KEYS) or embedded_backgrounds or backgrounds_changed:
                save_settings(settings)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load settings: %s", exc)
    return settings


def persist_background(value: str) -> str:
    if not value.startswith("data:image/"):
        if value and not re.fullmatch(r"/api/appearance/background/[0-9a-f]{64}\.(?:png|jpg|webp|gif|avif)", value):
            return ""
        return value
    match = re.fullmatch(r"data:image/(png|jpeg|jpg|webp|gif|avif);base64,(.+)", value, re.DOTALL | re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=400, detail="背景图格式无效")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="背景图数据无效") from exc
    if not content or len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="背景图大小必须在 5 MB 以内")
    extension = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else match.group(1).lower()
    filename = f"{hashlib.sha256(content).hexdigest()}.{extension}"
    directory = DATA_DIR / "backgrounds"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        temporary = directory / f".{filename}.{secrets.token_hex(4)}.tmp"
        temporary.write_bytes(content)
        temporary.replace(target)
    return f"/api/appearance/background/{filename}"


def normalize_background_settings(settings: dict[str, Any]) -> None:
    originals = [str(value) for value in settings.get("appearance_backgrounds", []) if value]
    selected_original = str(settings.get("appearance_background") or "")
    values = originals + ([selected_original] if selected_original and selected_original not in originals else [])
    replacements = {value: persist_background(value) for value in values}
    normalized = list(dict.fromkeys(replacements[value] for value in values if replacements[value]))[-20:]
    settings["appearance_backgrounds"] = normalized
    settings["appearance_background"] = replacements.get(selected_original, selected_original)
    directory = DATA_DIR / "backgrounds"
    if directory.is_dir():
        used = {Path(value).name for value in normalized if value.startswith("/api/appearance/background/")}
        for path in directory.iterdir():
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg|webp|gif|avif)", path.name) and path.name not in used:
                path.unlink(missing_ok=True)


def save_settings(settings: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        normalize_background_settings(settings)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=DATA_DIR)
        temporary = Path(path)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({key: value for key, value in settings.items() if key not in RUNTIME_SETTINGS_KEYS}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(SETTINGS_FILE)
        configured_limit = max(1 * 1024 * 1024, min(int(settings.get("log_max_bytes", MAX_LOG_BYTES)), 50 * 1024 * 1024))
        for handler in logger.handlers:
            if isinstance(handler, CappedFileHandler):
                handler.max_bytes = configured_limit
            logger.setLevel(getattr(logging, str(settings.get("log_level", "WARNING")).upper(), logging.WARNING))
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        logger.exception("Could not save settings")
        raise HTTPException(
            status_code=500, detail=f"无法保存设置，请检查数据卷写入权限：{exc}"
        ) from exc


def clean_content(text: str, settings: dict[str, Any], *, strip_result: bool = True) -> str:
    for pattern in settings.get("clean_patterns", []):
        try:
            text = re.sub(pattern, "", text)
        except re.error as exc:
            logger.warning("Skipping invalid saved regex %r: %s", pattern, exc)
    return text.strip() if strip_result else text


def models_url_for_target(target_url: str, override: str = "") -> str:
    override = str(override or "").strip()
    if override:
        return override
    parsed = urlsplit(str(target_url))
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)] + "/models"
            break
    else:
        if path == "/v1":
            path = "/v1/models"
        elif path == "/":
            path = "/models"
        else:
            path = path.rsplit("/", 1)[0] + "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def chat_url_for_target(target_url: str) -> str:
    """Accept a host, /v1, or a complete endpoint and normalize it internally."""
    parsed = urlsplit(target_url)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1/chat/completions"
    elif path == "/v1":
        path = "/v1/chat/completions"
    elif path.endswith("/models"):
        path = path[:-7].rstrip("/") + "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def endpoint_url_for_target(target_url: str, endpoint_path: str) -> str:
    parsed = urlsplit(chat_url_for_target(target_url))
    base_path = parsed.path.removesuffix("/chat/completions").rstrip("/")
    path = f"{base_path}/{endpoint_path.lstrip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def models_url(settings: dict[str, Any]) -> str:
    return models_url_for_target(str(settings["target_api_url"]), str(settings.get("models_api_url") or ""))


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
        "name": str(settings.get("default_upstream_name") or "默认上游"),
        "url": chat_url_for_target(str(settings["target_api_url"])),
        "api_key": str(settings.get("api_key") or ""),
        "timeout": int(settings.get("timeout_seconds") or 120),
        "models_url": models_url(settings),
        "clean_patterns": list(settings.get("clean_patterns") or []),
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
            "url": chat_url_for_target(str(item["url"])),
            "api_key": str(item.get("api_key") or ""),
            "timeout": max(1, min(int(item.get("timeout") or 120), 3600)),
            "models_url": models_url_for_target(str(item["url"]), str(item.get("models_url") or "")),
            "clean_patterns": list(item.get("clean_patterns") or settings.get("clean_patterns") or []),
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
    if preferred:
        return preferred + [item for item in upstreams if item not in preferred]
    affinity = ROUTE_AFFINITY.get(model)
    if affinity and affinity[1] > time.time():
        preferred = [item for item in upstreams if item["name"] == affinity[0]]
        if preferred:
            return preferred + [item for item in upstreams if item not in preferred]
        ROUTE_AFFINITY.pop(model, None)
    if MODEL_CACHE.get("data"):
        available = [item.get("upstream") for item in MODEL_CACHE["data"] if item.get("id") == model]
        if available:
            preferred = [item for name in available for item in upstreams if item["name"] == name]
            return preferred + [item for item in upstreams if item not in preferred]
    return preferred + [item for item in upstreams if item not in preferred]


def remember_route(settings: dict[str, Any], model: str, upstream_name: str) -> None:
    minutes = max(0, int(settings.get("route_affinity_minutes", 15)))
    if model and upstream_name and minutes:
        ROUTE_AFFINITY[model] = (upstream_name, time.time() + minutes * 60)


def upstream_stream_timeout(upstream: dict[str, Any]) -> httpx.Timeout:
    seconds = max(1.0, float(upstream.get("timeout") or 120))
    return httpx.Timeout(seconds, connect=min(seconds, 10.0), read=seconds, write=seconds, pool=min(seconds, 10.0))


def clean_stream_line(line: str, settings: dict[str, Any]) -> str:
    if not line.startswith("data: ") or line.strip() == "data: [DONE]":
        return line
    try:
        payload = json.loads(line[6:])
        for choice in payload.get("choices", []):
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                delta["content"] = clean_content(delta["content"], settings, strip_result=False)
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
        update_usage_record_safely(getattr(request.state, "usage_id", None), latency_ms=round(elapsed_ms, 1))
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


@app.get("/api/appearance/background/{filename}", include_in_schema=False)
async def appearance_background(filename: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg|webp|gif|avif)", filename):
        raise HTTPException(status_code=404, detail="背景图不存在")
    path = DATA_DIR / "backgrounds" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="背景图不存在")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


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


def connectivity_metrics(
    history: list[dict[str, Any]], name: str, checked_at: float
) -> dict[str, float | int]:
    samples = [entry for entry in history if entry.get("ts", 0) >= checked_at - 7 * 86400]
    week_values = [
        result.get("ok")
        for entry in samples
        for result in entry.get("results", [])
        if result.get("name") == name
    ]
    day_values = [
        result.get("ok")
        for entry in samples
        if entry.get("ts", 0) >= checked_at - 86400
        for result in entry.get("results", [])
        if result.get("name") == name
    ]
    local_now = datetime.fromtimestamp(checked_at).astimezone()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today_values = [
        result.get("ok")
        for entry in samples
        if entry.get("ts", 0) >= today_start
        for result in entry.get("results", [])
        if result.get("name") == name
    ]
    return {
        "availability_24h": round(sum(day_values) / len(day_values) * 100, 1) if day_values else 0,
        "availability_7d": round(sum(week_values) / len(week_values) * 100, 1) if week_values else 0,
        "checks_today": len(today_values),
        "failures_today": sum(1 for ok in today_values if not ok),
    }


async def run_connectivity_test(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one shared connectivity sample for every configured upstream."""
    settings = settings or load_settings()
    results = []
    async with CONNECTIVITY_LOCK:
        async with httpx.AsyncClient() as client:
            for upstream in configured_upstreams(settings):
                headers = {"Authorization": f"Bearer {upstream['api_key']}"} if upstream["api_key"] else {}
                started = time.perf_counter()
                try:
                    response = await client.get(upstream["models_url"], headers=headers, timeout=8.0)
                    response.raise_for_status()
                    results.append({"name": upstream["name"], "ok": True, "latency_ms": round((time.perf_counter()-started)*1000), "detail": "模型接口正常"})
                except Exception as exc:
                    results.append({"name": upstream["name"], "ok": False, "latency_ms": round((time.perf_counter()-started)*1000), "detail": str(exc)})
        checked = len(results)
        healthy = sum(1 for item in results if item.get("ok"))
        checked_at = time.time()
        percent = round(healthy / checked * 100, 1) if checked else 0
        database_add_connectivity(
            checked_at,
            [{"name": item["name"], "ok": item["ok"]} for item in results],
        )
        history = database_connectivity_history()
        CONNECTIVITY_STATE.update({"checked_at": checked_at, "results": results, "availability_percent": percent})
        for item in results:
            item.update(connectivity_metrics(history, item["name"], checked_at))
        return {**CONNECTIVITY_STATE}


@app.post("/api/settings/test")
async def test_connections(_: None = Depends(require_admin)) -> dict[str, Any]:
    return await run_connectivity_test()


async def connectivity_scheduler() -> None:
    """Keep connectivity history current even when no admin browser is open."""
    while True:
        try:
            settings = load_settings()
            interval = max(1, min(int(settings.get("connectivity_interval_minutes", 10)), 1440))
            checked_at = float(CONNECTIVITY_STATE.get("checked_at") or 0)
            delay = max(1.0, checked_at + interval * 60 - time.time())
            await asyncio.sleep(delay)
            await run_connectivity_test()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Automatic upstream connectivity test failed: %s", exc)
            await asyncio.sleep(60)


@app.get("/api/settings/connectivity")
async def get_connectivity(_: None = Depends(require_admin)) -> dict[str, Any]:
    if CONNECTIVITY_STATE.get("checked_at") is None:
        history = database_connectivity_history()
        if history:
            latest = history[-1]
            results = [dict(item) for item in latest.get("results", [])]
            for item in results:
                item.setdefault("detail", "历史检测结果")
                item.update(connectivity_metrics(history, str(item.get("name") or ""), float(latest["ts"])))
            healthy = sum(1 for item in results if item.get("ok"))
            CONNECTIVITY_STATE.update({
                "checked_at": latest["ts"],
                "results": results,
                "availability_percent": round(healthy / len(results) * 100, 1) if results else 0,
            })
    return {**CONNECTIVITY_STATE}


@app.get("/api/settings/export")
async def export_settings(_: None = Depends(require_admin)) -> Response:
    settings = load_settings()
    safe = {key: value for key, value in settings.items() if key in DEFAULT_SETTINGS}
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
    reset = copy.deepcopy(DEFAULT_SETTINGS)
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
    items = database_tokens()
    now = int(time.time())
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token = ""
        try:
            token = token_plain(str(item.get("cipher") or ""), str(item.get("id"))) if item.get("cipher") else ""
        except (ValueError, UnicodeDecodeError):
            pass
        token_id = str(item.get("id"))
        expires_at = item.get("expires_at")
        result.append({
            "id": token_id,
            "name": str(item.get("name")),
            "token": token,
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
            "expires_at": expires_at,
            "enabled": bool(item.get("enabled", 1)),
            "allowed_models": token_model_rules(item),
            "status": "active" if token_is_active(item, now) else ("disabled" if not item.get("enabled", 1) else "expired"),
            "total_tokens": max(0, int(item.get("total_tokens", 0) or 0)),
        })
    return {"data": result}


@app.post("/api/tokens")
async def create_api_token(update: ApiTokenRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    raw = "cln_" + secrets.token_urlsafe(24)
    token_id = uuid.uuid4().hex
    item = {"id": token_id, "name": update.name.strip(), "hash": token_digest(raw), "cipher": token_cipher(raw, token_id), "created_at": int(time.time()), "last_used_at": None, "expires_at": update.expires_at, "enabled": 1, "total_tokens": 0, "allowed_models": json.dumps(update.allowed_models, ensure_ascii=False)}
    with database_connection() as database:
        database.execute(
            """INSERT INTO api_tokens
               (id, name, hash, cipher, created_at, last_used_at, expires_at, enabled, total_tokens, allowed_models)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(item[key] for key in ("id", "name", "hash", "cipher", "created_at", "last_used_at", "expires_at", "enabled", "total_tokens", "allowed_models")),
        )
    return {"id": item["id"], "name": item["name"], "token": raw, "created_at": item["created_at"]}


@app.patch("/api/tokens/{token_id}/status")
async def update_api_token_status(
    token_id: str, update: ApiTokenStatusUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    with database_connection() as database:
        cursor = database.execute(
            "UPDATE api_tokens SET enabled = ? WHERE id = ?", (int(update.enabled), token_id)
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="令牌不存在")
    return {"message": "令牌已启用" if update.enabled else "令牌已停用"}


@app.patch("/api/tokens/{token_id}/policy")
async def update_api_token_policy(
    token_id: str, update: ApiTokenPolicyUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    with database_connection() as database:
        cursor = database.execute(
            "UPDATE api_tokens SET expires_at = ?, allowed_models = ? WHERE id = ?",
            (update.expires_at, json.dumps(update.allowed_models, ensure_ascii=False), token_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="令牌不存在")
    return {"message": "令牌策略已保存"}


@app.delete("/api/tokens/{token_id}")
async def revoke_api_token(token_id: str, _: None = Depends(require_admin)) -> dict[str, str]:
    with database_connection() as database:
        cursor = database.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="令牌不存在")
    return {"message": "令牌已删除"}


@app.get("/api/usage")
async def api_usage(
    token_id: str = "", token_name: str = "", _: None = Depends(require_admin)
) -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    year_start = today_start.replace(month=1, day=1)
    bounds = {
        "year": int(year_start.timestamp()),
        "month": int(month_start.timestamp()),
        "week": int(week_start.timestamp()),
        "yesterday": int(yesterday_start.timestamp()),
        "today": int(today_start.timestamp()),
    }
    where = "at >= ?"
    params: list[Any] = [bounds["year"]]
    if token_id:
        where += " AND token_id = ?"
        params.append(token_id)
    elif token_name:  # Backward compatibility for older admin clients.
        where += " AND token_name = ?"
        params.append(token_name)
    with database_connection() as database:
        summary = database.execute(
            f"""SELECT COUNT(*) AS total,
                SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS today,
                SUM(CASE WHEN at >= ? AND at < ? THEN 1 ELSE 0 END) AS yesterday,
                SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS week,
                SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS month,
                SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS year,
                SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS tokens_today,
                SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS tokens_week,
                SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS tokens_month
                FROM api_usage WHERE {where}""",
            (
                bounds["today"], bounds["yesterday"], bounds["today"],
                bounds["week"], bounds["month"], bounds["year"],
                bounds["today"], bounds["week"], bounds["month"], *params,
            ),
        ).fetchone()
        log_rows = database.execute(
            f"SELECT * FROM api_usage WHERE {where} AND visible = 1 ORDER BY at DESC LIMIT 100",
            params,
        ).fetchall()
        by_token_rows = database.execute(
            """SELECT COALESCE(token_id, '') AS token_id, token_name, COUNT(*) AS calls
               FROM api_usage WHERE at >= ? GROUP BY token_id, token_name""",
            (bounds["year"],),
        ).fetchall()
    values = row_dict(summary)
    by_token = {
        str(row["token_id"] or row["token_name"] or "未命名"): int(row["calls"])
        for row in by_token_rows
    }
    return {
        "total": int(values.get("total") or 0),
        "today": int(values.get("today") or 0),
        "yesterday": int(values.get("yesterday") or 0),
        "week": int(values.get("week") or 0),
        "month": int(values.get("month") or 0),
        "year": int(values.get("year") or 0),
        "tokens_today": int(values.get("tokens_today") or 0),
        "tokens_week": int(values.get("tokens_week") or 0),
        "tokens_month": int(values.get("tokens_month") or 0),
        "by_token": by_token,
        "logs": [row_dict(row) for row in log_rows],
    }


@app.delete("/api/usage/logs")
async def clear_usage_logs(_: None = Depends(require_admin)) -> dict[str, str]:
    with database_connection() as database:
        database.execute(
            """UPDATE api_usage
               SET visible = 0, path = '', method = '', model = '', latency_ms = NULL
               WHERE visible = 1"""
        )
    return {"message": "使用日志已清除，调用与 Token 统计不受影响"}


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
    upstreams = configured_upstreams(settings)
    sources = "|".join(f"{item['name']}:{item['models_url']}" for item in upstreams)
    ttl = max(0, int(settings.get("model_cache_ttl", 60)))
    if not refresh and MODEL_CACHE.get("data") is not None and time.time() - float(MODEL_CACHE.get("at", 0)) < ttl and MODEL_CACHE.get("source") == sources:
        return {"source": "多个上游" if len(upstreams) > 1 else upstreams[0]["models_url"], "count": len(MODEL_CACHE["data"]), "cached": True, "data": MODEL_CACHE["data"]}
    models: list[dict[str, Any]] = []
    failures: list[str] = []
    async with httpx.AsyncClient() as client:
        for upstream in upstreams:
            headers = {"Accept": "application/json"}
            if upstream["api_key"]:
                headers["Authorization"] = f"Bearer {upstream['api_key']}"
            try:
                response = await client.get(upstream["models_url"], headers=headers, timeout=min(float(upstream["timeout"]), 30.0))
                response.raise_for_status(); payload = response.json()
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                failures.append(f"{upstream['name']}: {exc}"); continue
            raw_models = payload.get("data", []) if isinstance(payload, dict) else []
            if not raw_models and isinstance(payload, dict): raw_models = payload.get("models", [])
            for item in raw_models if isinstance(raw_models, list) else []:
                model_id = item if isinstance(item, str) else item.get("id") or item.get("name") or item.get("model") if isinstance(item, dict) else None
                if model_id:
                    models.append({"id": str(model_id), "owned_by": str(item.get("owned_by") or item.get("owner") or "upstream") if isinstance(item, dict) else "upstream", "created": item.get("created") or item.get("modified_at") if isinstance(item, dict) else None, "upstream": upstream["name"]})
    if not models and failures:
        raise HTTPException(status_code=502, detail="获取上游模型失败：" + "；".join(failures))
    upstream_order = {item["name"]: index for index, item in enumerate(upstreams)}
    models.sort(key=lambda item: (upstream_order.get(item["upstream"], len(upstreams)), item["id"].lower()))
    MODEL_CACHE.update({"at": time.time(), "data": models, "source": sources})
    logger.info("Discovered %d models from %d upstreams", len(models), len(upstreams))
    return {"source": "多个上游" if len(upstreams) > 1 else upstreams[0]["models_url"], "count": len(models), "cached": False, "data": models, "failures": failures}


@app.get("/v1/models")
async def public_models(request: Request, _: None = Depends(require_api_token)) -> dict[str, Any]:
    result = await get_models(False, None)
    token = getattr(request.state, "api_token", None)
    models = [item for item in result.get("data", []) if token is None or token_allows_model(token, str(item.get("id") or ""))]
    return {
        "object": "list",
        "data": [
            {"id": item["id"], "object": "model", "created": item.get("created") or 0, "owned_by": item.get("owned_by") or "upstream"}
            for item in models
        ],
    }


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
    # Archives are download artifacts, not persistent application data. Keep them
    # outside /data so the volume only retains settings and export metadata.
    temporary = tempfile.NamedTemporaryFile(prefix="cleanllm-model-", suffix=".tar.gz", delete=False)
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
    database_add_export({"id": uuid.uuid4().hex, "model": model, "filename": filename, "created_at": int(time.time()), "size": archive_path.stat().st_size})
    return FileResponse(archive_path, filename=filename, media_type="application/gzip", background=BackgroundTask(archive_path.unlink, missing_ok=True))


@app.get("/api/ollama/export-history")
async def export_history(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"data": database_export_history()}


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


async def native_responses_stream(payload: dict[str, Any], settings: dict[str, Any], request: Request) -> Response:
    """Relay native Responses SSE while preserving event order and stream semantics."""
    failures: list[str] = []
    for upstream in route_upstreams(settings, str(payload.get("model") or "")):
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        client = httpx.AsyncClient(timeout=upstream_stream_timeout(upstream))
        responses_url = upstream["url"].replace("/chat/completions", "/responses")
        try:
            logger.info("Responses upstream request: %s -> %s", upstream["name"], responses_url)
            response = await client.send(
                client.build_request("POST", responses_url, json={**payload, "stream": True}, headers=headers),
                stream=True,
            )
        except httpx.RequestError as exc:
            failures.append(f"{upstream['name']}: {exc}")
            await client.aclose()
            continue
        if response.status_code >= 400:
            failures.append(f"{upstream['name']}: HTTP {response.status_code}")
            await response.aclose()
            await client.aclose()
            continue
        started = time.perf_counter()
        response_settings = {
            **settings,
            "clean_patterns": upstream.get("clean_patterns", settings.get("clean_patterns", [])),
        }

        async def relay() -> Any:
            pending = b""
            state: dict[str, Any] = {"completed": False, "terminal": False, "text_done": False, "text": "", "response": None}

            def process_line(raw_line: bytes) -> bytes:
                line = raw_line.rstrip(b"\r")
                if not line.startswith(b"data:"):
                    return raw_line
                data = line[5:].lstrip()
                if not data:
                    return raw_line
                if data == b"[DONE]":
                    state["done_marker"] = True
                    return raw_line
                try:
                    event = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    return raw_line
                if not isinstance(event, dict):
                    return raw_line
                event_type = str(event.get("type") or "")
                if event_type == "response.output_text.delta":
                    delta = clean_content(str(event.get("delta") or ""), response_settings, strip_result=False)
                    event["delta"] = delta
                    state["text"] += delta
                elif event_type == "response.output_text.done":
                    state["text_done"] = True
                    if not state["text"]:
                        text = clean_content(str(event.get("text") or ""), response_settings)
                        event["text"] = text
                        state["text"] = text
                response_data = event.get("response")
                if isinstance(response_data, dict):
                    state["response"] = response_data
                if event_type == "response.completed" or (
                    isinstance(response_data, dict) and response_data.get("status") == "completed"
                ):
                    state["completed"] = True
                    state["terminal"] = True
                    update_usage_record_safely(
                        getattr(request.state, "usage_id", None),
                        **usage_values(response_data.get("usage")),
                    )
                    remember_route(settings, str(payload.get("model") or ""), upstream["name"])
                elif event_type in {"response.failed", "response.incomplete"}:
                    state["terminal"] = True
                return b"data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

            try:
                async for chunk in response.aiter_bytes():
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        yield process_line(line) + b"\n"
            except httpx.RequestError as exc:
                logger.warning("Responses stream from %s disconnected: %s", upstream["name"], exc)
                if not state["terminal"]:
                    failed = {
                        "type": "response.failed",
                        "response": {
                            "id": f"resp-{secrets.token_hex(12)}",
                            "object": "response",
                            "created_at": int(time.time()),
                            "model": payload.get("model"),
                            "status": "failed",
                            "error": {"type": "upstream_error", "message": "上游响应流意外中断"},
                        },
                    }
                    yield f"\n\nevent: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}\n\n".encode()
            else:
                if pending:
                    yield process_line(pending)
                if not state["completed"]:
                    failed = {
                        "type": "response.failed",
                        "response": {
                            "id": f"resp-{secrets.token_hex(12)}",
                            "object": "response",
                            "created_at": int(time.time()),
                            "model": payload.get("model"),
                            "status": "failed",
                            "error": {"type": "upstream_error", "message": "上游响应流未正常结束"},
                        },
                    }
                    logger.warning("Responses stream from %s ended without response.completed", upstream["name"])
                    yield f"\nevent: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}\n\n".encode()
            finally:
                await response.aclose()
                await client.aclose()
                if state["completed"]:
                    logger.info("Responses stream completed: %s", upstream["name"])
                update_usage_record_safely(
                    getattr(request.state, "usage_id", None),
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                )

        return StreamingResponse(
            relay(),
            status_code=response.status_code,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "所有上游 Responses 接口均不可用：" + "；".join(failures), "type": "upstream_error"}},
    )


@app.post("/v1/embeddings")
@app.post("/v1/completions")
@app.post("/v1/images/generations")
@app.post("/v1/audio/transcriptions")
@app.post("/v1/audio/translations")
@app.post("/v1/audio/speech")
@app.post("/v1/moderations")
@app.post("/v1/rerank")
async def compatible_api(request: Request, _: None = Depends(require_api_token)) -> Response:
    """Pass common OpenAI-compatible APIs to the configured upstreams."""
    body = await request.body()
    payload: dict[str, Any] = {}
    if "application/json" in request.headers.get("content-type", ""):
        try:
            candidate = json.loads(body)
            payload = candidate if isinstance(candidate, dict) else {}
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="请求体不是有效 JSON")
    endpoint = request.url.path.removeprefix("/v1/")
    settings = load_settings()
    failures: list[str] = []
    for upstream in route_upstreams(settings, str(payload.get("model") or "")):
        url = endpoint_url_for_target(upstream["url"], endpoint)
        headers = {
            "Content-Type": request.headers.get("content-type", "application/json"),
            "Accept": request.headers.get("accept", "application/json"),
        }
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        try:
            async with httpx.AsyncClient(timeout=float(upstream["timeout"])) as client:
                response = await client.post(url, content=body, headers=headers)
            if response.status_code >= 400:
                failures.append(f"{upstream['name']}: HTTP {response.status_code}")
                continue
            remember_route(settings, str(payload.get("model") or endpoint), upstream["name"])
            response_headers = {}
            if response.headers.get("content-disposition"):
                response_headers["Content-Disposition"] = response.headers["content-disposition"]
            if "application/json" in response.headers.get("content-type", ""):
                try:
                    result = response.json()
                    update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values(result.get("usage") if isinstance(result, dict) else {}))
                except ValueError:
                    pass
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type", "application/octet-stream"),
                headers=response_headers,
            )
        except httpx.RequestError as exc:
            failures.append(f"{upstream['name']}: {exc}")
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "所有上游均不可用：" + "；".join(failures), "type": "upstream_error"}},
    )


@app.post("/v1/responses")
async def responses_api(request: Request, _: None = Depends(require_api_token)) -> Response:
    """Accept the OpenAI Responses request shape while using the existing chat upstreams."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是有效 JSON") from exc
    if not isinstance(payload, dict) or not payload.get("model"):
        raise HTTPException(status_code=400, detail="Responses 请求必须包含 model")
    source = payload.get("input", "")
    if isinstance(source, str):
        messages = [{"role": "user", "content": source}]
    elif isinstance(source, list):
        messages = []
        for item in source:
            if isinstance(item, dict) and item.get("role"):
                content = item.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
                messages.append({"role": item["role"], "content": content})
    else:
        messages = [{"role": "user", "content": str(source)}]
    instructions = payload.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": str(instructions)})
    chat_payload = {"model": payload["model"], "messages": messages, "stream": False}
    for key in ("temperature", "top_p", "max_output_tokens"):
        if key in payload:
            chat_payload["max_tokens" if key == "max_output_tokens" else key] = payload[key]
    settings = load_settings(); failures = []
    logger.info("Responses request received for model %s (stream=%s)", payload["model"], payload.get("stream") is True)
    if payload.get("stream") is True:
        native_stream = await native_responses_stream(payload, settings, request)
        if native_stream.status_code != 502:
            return native_stream
    if payload.get("stream") is True:
        response_id = f"resp-{secrets.token_hex(12)}"
        created_at = int(time.time())
        async def response_events():
            base = {"id": response_id, "object": "response", "created_at": created_at, "model": payload["model"], "status": "in_progress", "output": []}
            yield f"event: response.created\ndata: {json.dumps({'type':'response.created','response':base}, ensure_ascii=False)}\n\n"
            complete_text = ""
            selected_name = None
            sequence_number = 0
            item_id = f"msg-{secrets.token_hex(8)}"
            native_completed = False
            chat_done = False
            stream_payload = {**chat_payload, "stream": True}
            for upstream in route_upstreams(settings, str(payload["model"])):
                headers = {"Content-Type": "application/json"}
                if upstream["api_key"]: headers["Authorization"] = f"Bearer {upstream['api_key']}"
                try:
                    responses_url = upstream["url"]
                    logger.info("Responses chat fallback request: %s -> %s", upstream["name"], responses_url)
                    async with httpx.AsyncClient(timeout=upstream_stream_timeout(upstream)) as client:
                        async with client.stream("POST", responses_url, json=stream_payload, headers=headers) as upstream_response:
                            if upstream_response.status_code >= 400:
                                logger.warning("Responses upstream %s returned HTTP %s", upstream["name"], upstream_response.status_code)
                                continue
                            selected_name = upstream["name"]
                            emitted = False
                            event_name = "message"
                            async for line in upstream_response.aiter_lines():
                                if line.startswith("event: "):
                                    event_name = line[7:].strip() or "message"
                                    continue
                                if line == "data: [DONE]":
                                    chat_done = True
                                    continue
                                if not line.startswith("data: "):
                                    continue
                                try:
                                    chunk = json.loads(line[6:])
                                    if isinstance(chunk, dict) and str(chunk.get("type", "")).startswith("response."):
                                        emitted = True
                                        if chunk.get("type") == "response.output_text.delta":
                                            delta = clean_content(str(chunk.get("delta", "")), {**settings, "clean_patterns": upstream.get("clean_patterns", [])}, strip_result=False)
                                            chunk["delta"] = delta
                                            complete_text += delta
                                        if chunk.get("type") == "response.completed":
                                            native_completed = True
                                            update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values((chunk.get("response") or {}).get("usage")))
                                        yield f"event: {event_name}\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                                        continue
                                    delta = chunk.get("delta", "") or chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                except (ValueError, IndexError, AttributeError):
                                    continue
                                if not isinstance(delta, str) or not delta: continue
                                delta = clean_content(delta, {**settings, "clean_patterns": upstream.get("clean_patterns", [])}, strip_result=False)
                                if not delta: continue
                                complete_text += delta
                                emitted = True
                                sequence_number += 1
                                event = {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": delta, "response_id": response_id, "sequence_number": sequence_number}
                                yield f"event: response.output_text.delta\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                            if not emitted and "application/json" in upstream_response.headers.get("content-type", ""):
                                try:
                                    data = json.loads(await upstream_response.aread())
                                    delta = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                                    if isinstance(delta, str) and delta:
                                        complete_text += clean_content(delta, {**settings, "clean_patterns": upstream.get("clean_patterns", [])})
                                        sequence_number += 1
                                        yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','item_id':item_id,'output_index':0,'content_index':0,'delta':complete_text,'response_id':response_id,'sequence_number':sequence_number}, ensure_ascii=False)}\n\n"
                                except (ValueError, IndexError, AttributeError):
                                    pass
                    break
                except httpx.RequestError:
                    continue
            if native_completed:
                remember_route(settings, str(payload["model"]), str(selected_name or ""))
                return
            if selected_name is None:
                failed = {**base, "status": "failed", "error": {"message": "所有上游均不可用", "type": "upstream_error"}}
                yield f"event: response.failed\ndata: {json.dumps({'type':'response.failed','response':failed}, ensure_ascii=False)}\n\n"
                return
            if not chat_done:
                failed = {**base, "status": "failed", "error": {"message": "上游响应流未正常结束", "type": "upstream_error"}}
                yield f"event: response.failed\ndata: {json.dumps({'type':'response.failed','response':failed}, ensure_ascii=False)}\n\n"
                return
            remember_route(settings, str(payload["model"]), str(selected_name))
            completed = {**base, "status": "completed", "output": [{"id": item_id, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": complete_text}]}]}
            sequence_number += 1
            completed["sequence_number"] = sequence_number
            yield f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','item_id':item_id,'output_index':0,'content_index':0,'text':complete_text,'response_id':response_id,'sequence_number':sequence_number}, ensure_ascii=False)}\n\n"
            yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':completed}, ensure_ascii=False)}\n\n"
        return StreamingResponse(response_events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})
    async with httpx.AsyncClient() as client:
        for upstream in route_upstreams(settings, str(payload["model"])):
            headers = {"Content-Type": "application/json"}
            if upstream["api_key"]: headers["Authorization"] = f"Bearer {upstream['api_key']}"
            try:
                responses_url = upstream["url"].replace("/chat/completions", "/responses")
                logger.info("Responses upstream request: %s -> %s", upstream["name"], responses_url)
                response = await client.post(responses_url, json={**payload, "stream": False}, headers=headers, timeout=float(upstream["timeout"]))
                if response.status_code in {404, 405}:
                    response = await client.post(upstream["url"], json=chat_payload, headers=headers, timeout=float(upstream["timeout"]))
                if response.status_code >= 400: failures.append(f"{upstream['name']}: HTTP {response.status_code}"); continue
                data = response.json(); text = data.get("output_text", "")
                if not text:
                    output = data.get("output", [])
                    text = "".join(part.get("text", "") for item in output if isinstance(item, dict) for part in item.get("content", []) if isinstance(part, dict))
                if not text:
                    choices = data.get("choices", []); text = choices[0].get("message", {}).get("content", "") if choices else ""
                text = clean_content(text, {**settings, "clean_patterns": upstream.get("clean_patterns", settings.get("clean_patterns", []))})
                now = int(time.time())
                result = {"id": f"resp-{secrets.token_hex(12)}", "object": "response", "created_at": now, "model": payload["model"], "status": "completed", "output": [{"id": f"msg-{secrets.token_hex(8)}", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "usage": data.get("usage", {})}
                update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values(data.get("usage")))
                remember_route(settings, str(payload["model"]), upstream["name"])
                if payload.get("stream") is True:
                    async def events():
                        yield f"event: response.created\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                        yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','delta':text,'response_id':result['id']}, ensure_ascii=False)}\n\n"
                        yield f"event: response.completed\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                    return StreamingResponse(events(), media_type="text/event-stream")
                return JSONResponse(status_code=response.status_code, content=result)
            except (httpx.RequestError, ValueError) as exc:
                logger.warning("Responses upstream %s failed: %s", upstream["name"], exc)
                failures.append(f"{upstream['name']}: {exc}")
    return JSONResponse(status_code=502, content={"error": {"message": "所有上游均不可用：" + "；".join(failures), "type": "upstream_error"}})


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
                candidate_client = httpx.AsyncClient(timeout=upstream_stream_timeout(upstream))
                candidate = await candidate_client.send(candidate_client.build_request("POST", upstream["url"], json=payload, headers=headers), stream=True)
                if candidate.status_code >= 400:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    await candidate.aclose()
                    await candidate_client.aclose()
                    continue
                client, response, selected = candidate_client, candidate, upstream
            else:
                async with httpx.AsyncClient() as candidate_client:
                    candidate = await candidate_client.post(upstream["url"], json=payload, headers=headers, timeout=float(upstream["timeout"]))
                if candidate.status_code >= 400:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    continue
                response, selected = candidate, upstream
            break
        except httpx.RequestError as exc:
            failures.append(f"{upstream['name']}: {exc}")
    if response is None:
        return JSONResponse(status_code=502, content={"error": {"message": "所有上游均不可用：" + "；".join(failures), "type": "upstream_error"}})
    logger.info("Model %s routed to %s", payload.get("model", ""), selected["name"])
    response_settings = {**settings, "clean_patterns": selected.get("clean_patterns", settings.get("clean_patterns", []))}
    if streaming:
        async def stream_response():
            buffer = ""
            completed = False
            try:
                async for text in response.aiter_text():
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip() == "data: [DONE]":
                            completed = True
                        elif line.startswith("data: "):
                            try:
                                update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values(json.loads(line[6:]).get("usage")))
                            except (ValueError, AttributeError):
                                pass
                        yield (clean_stream_line(line, response_settings) + "\n").encode("utf-8")
                if buffer:
                    if buffer.strip() == "data: [DONE]":
                        completed = True
                    yield clean_stream_line(buffer, response_settings).encode("utf-8")
            finally:
                await response.aclose()
                await client.aclose()
                if completed:
                    remember_route(settings, str(payload.get("model") or ""), selected["name"])
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
            message["content"] = clean_content(content, response_settings)
    update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values(data.get("usage") if isinstance(data, dict) else {}))
    remember_route(settings, str(payload.get("model") or ""), selected["name"])
    return JSONResponse(status_code=response.status_code, content=data)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=11515)
