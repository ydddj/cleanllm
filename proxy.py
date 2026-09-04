import hashlib
import hmac
import ipaddress
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
import csv
import zipfile
from email.utils import parsedate_to_datetime
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
    "default_upstream_enabled": True,
    "model_routes": [],
    "log_max_bytes": int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
    "log_level": os.getenv("LOG_LEVEL", "WARNING").upper(),
    "model_cache_ttl": int(os.getenv("MODEL_CACHE_TTL", "60")),
    "connectivity_interval_minutes": int(os.getenv("CONNECTIVITY_INTERVAL_MINUTES", "10")),
    "route_affinity_minutes": int(os.getenv("ROUTE_AFFINITY_MINUTES", "15")),
    "circuit_breaker_failures": int(os.getenv("CIRCUIT_BREAKER_FAILURES", "3")),
    "circuit_breaker_cooldown_seconds": int(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60")),
    "virtual_models": [],
    "model_pricing": [],
    "webhook_url": os.getenv("ALERT_WEBHOOK_URL", ""),
    "alert_upstream_failures": int(os.getenv("ALERT_UPSTREAM_FAILURES", "3")),
    "alert_error_rate_percent": int(os.getenv("ALERT_ERROR_RATE_PERCENT", "30")),
    "alert_budget_percent": int(os.getenv("ALERT_BUDGET_PERCENT", "80")),
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
CONNECTIVITY_WAKEUP = asyncio.Event()
STATUS_SUBSCRIBERS: set[asyncio.Queue] = set()
ROUTE_AFFINITY: dict[str, tuple[str, float]] = {}
UPSTREAM_CIRCUITS: dict[str, dict[str, Any]] = {}
NO_ROUTE_CACHE: dict[str, float] = {}
ALERT_COOLDOWNS: dict[str, float] = {}
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
    default_upstream_enabled: bool = True
    model_routes: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    model_cache_ttl: int = Field(default=60, ge=0, le=86400)
    connectivity_interval_minutes: int = Field(default=10, ge=1, le=1440)
    route_affinity_minutes: int = Field(default=15, ge=0, le=1440)
    circuit_breaker_failures: int = Field(default=3, ge=1, le=20)
    circuit_breaker_cooldown_seconds: int = Field(default=60, ge=5, le=3600)
    virtual_models: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    model_pricing: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    webhook_url: str = Field(default="", max_length=2000)
    alert_upstream_failures: int = Field(default=3, ge=1, le=100)
    alert_error_rate_percent: int = Field(default=30, ge=1, le=100)
    alert_budget_percent: int = Field(default=80, ge=1, le=100)
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
                "enabled": bool(item.get("enabled", True)),
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

    @field_validator("virtual_models")
    @classmethod
    def validate_virtual_models(cls, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        aliases: set[str] = set()
        for index, item in enumerate(models, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"虚拟模型 {index} 必须是对象")
            alias = str(item.get("alias") or "").strip()
            target = str(item.get("target") or "").strip()
            upstreams = item.get("upstreams") or []
            if not alias or len(alias) > 200 or not target or len(target) > 200:
                raise ValueError(f"虚拟模型 {index} 必须包含有效的别名和目标模型")
            if alias in aliases:
                raise ValueError(f"虚拟模型别名不能重复：{alias}")
            if not isinstance(upstreams, list) or not all(isinstance(name, str) and name.strip() for name in upstreams):
                raise ValueError(f"虚拟模型 {alias} 的 upstreams 必须是名称数组")
            routes = item.get("routes") or []
            if not isinstance(routes, list):
                raise ValueError(f"虚拟模型 {alias} 的 routes 必须是数组")
            cleaned_routes: list[dict[str, Any]] = []
            for route_index, route in enumerate(routes, start=1):
                if not isinstance(route, dict):
                    raise ValueError(f"虚拟模型 {alias} 的路由 {route_index} 必须是对象")
                upstream = str(route.get("upstream") or "").strip()
                if not upstream:
                    raise ValueError(f"虚拟模型 {alias} 的路由 {route_index} 缺少上游")
                token_patterns = route.get("token_patterns") or []
                path_patterns = route.get("path_patterns") or []
                if not isinstance(token_patterns, list) or not all(isinstance(value, str) for value in token_patterns):
                    raise ValueError(f"虚拟模型 {alias} 的令牌规则必须是字符串数组")
                if not isinstance(path_patterns, list) or not all(isinstance(value, str) for value in path_patterns):
                    raise ValueError(f"虚拟模型 {alias} 的路径规则必须是字符串数组")
                start_time = str(route.get("start_time") or "").strip()
                end_time = str(route.get("end_time") or "").strip()
                if bool(start_time) != bool(end_time):
                    raise ValueError(f"虚拟模型 {alias} 的开始和结束时间必须同时填写")
                for value in (start_time, end_time):
                    if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                        raise ValueError(f"虚拟模型 {alias} 的时间必须使用 HH:MM")
                cleaned_routes.append({
                    "upstream": upstream,
                    "priority": max(0, min(int(route.get("priority") or 0), 1000)),
                    "weight": max(1, min(int(route.get("weight") or 1), 1000)),
                    "token_patterns": [value.strip() for value in token_patterns if value.strip()],
                    "path_patterns": [value.strip() for value in path_patterns if value.strip()],
                    "start_time": start_time,
                    "end_time": end_time,
                })
            aliases.add(alias)
            cleaned.append({
                "alias": alias,
                "target": target,
                "upstreams": list(dict.fromkeys(name.strip() for name in upstreams)),
                "routes": cleaned_routes,
            })
        return cleaned

    @field_validator("model_pricing")
    @classmethod
    def validate_model_pricing(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for index, item in enumerate(values, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"模型价格 {index} 必须是对象")
            pattern = str(item.get("pattern") or "").strip()
            upstream = str(item.get("upstream") or "").strip()
            if not pattern or len(pattern) > 200 or len(upstream) > 80:
                raise ValueError(f"模型价格 {index} 的模型规则无效")
            prices: dict[str, float] = {}
            for key in ("input_price", "output_price", "cached_price"):
                try:
                    value = float(item.get(key) or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"模型价格 {index} 必须是数字") from exc
                if not 0 <= value <= 1_000_000:
                    raise ValueError(f"模型价格 {index} 必须在 0 至 1000000 之间")
                prices[key] = value
            cleaned.append({"pattern": pattern, "upstream": upstream, **prices})
        return cleaned

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, value: str) -> str:
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Webhook 地址必须是有效的 HTTP/HTTPS URL")
        return value

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
        known = set(names)
        if not self.default_upstream_enabled and not any(item.get("enabled", True) for item in self.upstreams):
            raise ValueError("至少需要启用一个上游")
        for route in self.model_routes:
            unknown = [name for name in route["upstreams"] if name not in known]
            if unknown:
                raise ValueError(f"模型路由引用了不存在的上游：{', '.join(unknown)}")
        for item in self.virtual_models:
            referenced = [*item["upstreams"], *(route["upstream"] for route in item.get("routes", []))]
            unknown = [name for name in referenced if name not in known]
            if unknown:
                raise ValueError(f"虚拟模型 {item['alias']} 引用了不存在的上游：{', '.join(unknown)}")
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


class UpstreamBatchStatusRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=21)
    enabled: bool


class UpstreamImportRequest(BaseModel):
    upstreams: list[dict[str, Any]] = Field(min_length=1, max_length=21)
    mode: str = Field(default="append", pattern=r"^(append|replace)$")


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
    note: str = Field(default="", max_length=500)
    ip_allowlist: list[str] = Field(default_factory=list, max_length=100)
    expires_at: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list, max_length=100)
    rpm_limit: int = Field(default=0, ge=0, le=1_000_000)
    tpm_limit: int = Field(default=0, ge=0, le=1_000_000_000)
    daily_token_limit: int = Field(default=0, ge=0, le=100_000_000_000)
    monthly_token_limit: int = Field(default=0, ge=0, le=1_000_000_000_000)
    monthly_budget_usd: float = Field(default=0, ge=0, le=1_000_000_000)
    budget_action: str = Field(default="warn", pattern=r"^(warn|disable)$")

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

    @field_validator("ip_allowlist")
    @classmethod
    def validate_ip_allowlist(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            raw = str(value).strip()
            if not raw:
                continue
            try:
                normalized = str(ipaddress.ip_network(raw, strict=False))
            except ValueError as exc:
                raise ValueError(f"无效来源 IP 或 CIDR：{raw}") from exc
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned


class ApiTokenStatusUpdate(BaseModel):
    enabled: bool


class ApiTokenPolicyUpdate(BaseModel):
    note: str = Field(default="", max_length=500)
    ip_allowlist: list[str] = Field(default_factory=list, max_length=100)
    expires_at: int | None = Field(default=None, ge=1)
    allowed_models: list[str] = Field(default_factory=list, max_length=100)
    rpm_limit: int = Field(default=0, ge=0, le=1_000_000)
    tpm_limit: int = Field(default=0, ge=0, le=1_000_000_000)
    daily_token_limit: int = Field(default=0, ge=0, le=100_000_000_000)
    monthly_token_limit: int = Field(default=0, ge=0, le=1_000_000_000_000)
    monthly_budget_usd: float = Field(default=0, ge=0, le=1_000_000_000)
    budget_action: str = Field(default="warn", pattern=r"^(warn|disable)$")

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

    @field_validator("ip_allowlist")
    @classmethod
    def validate_ip_allowlist(cls, values: list[str]) -> list[str]:
        return ApiTokenRequest.validate_ip_allowlist(values)


class CompatibilityTestRequest(BaseModel):
    upstream: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)


class SnapshotRequest(BaseModel):
    reason: str = Field(default="手动快照", min_length=1, max_length=200)


class WebhookTestRequest(BaseModel):
    url: str = Field(default="", max_length=2000)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return SettingsUpdate.validate_webhook_url(value)


def publish_status(event: str, data: dict[str, Any]) -> None:
    item = {"event": event, "data": data, "id": int(time.time() * 1000)}
    for queue in tuple(STATUS_SUBSCRIBERS):
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(item)
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
                    limit_mb = self.max_bytes / (1024 * 1024)
                    marker = f"--- older log entries trimmed ({limit_mb:g} MB limit) ---\n"
                    target.write(marker.encode("utf-8"))
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
            allowed_models TEXT NOT NULL DEFAULT '[]',
            rpm_limit INTEGER NOT NULL DEFAULT 0,
            tpm_limit INTEGER NOT NULL DEFAULT 0,
            daily_token_limit INTEGER NOT NULL DEFAULT 0,
            monthly_token_limit INTEGER NOT NULL DEFAULT 0,
            monthly_budget_usd REAL NOT NULL DEFAULT 0,
            budget_action TEXT NOT NULL DEFAULT 'warn',
            note TEXT NOT NULL DEFAULT '',
            ip_allowlist TEXT NOT NULL DEFAULT '[]'
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
            resolved_model TEXT NOT NULL DEFAULT '',
            upstream TEXT NOT NULL DEFAULT '',
            status_code INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            cached_tokens INTEGER NOT NULL DEFAULT 0,
            first_byte_ms REAL,
            termination_reason TEXT NOT NULL DEFAULT '',
            cost_usd REAL NOT NULL DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS config_snapshots (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            reason TEXT NOT NULL,
            settings_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_config_snapshots_created ON config_snapshots(created_at DESC);
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL,
            actor TEXT NOT NULL,
            source_ip TEXT NOT NULL,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            status_code INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);
            """
        )
        token_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_tokens)")}
        if "allowed_models" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN allowed_models TEXT NOT NULL DEFAULT '[]'")
        for column in ("rpm_limit", "tpm_limit", "daily_token_limit", "monthly_token_limit"):
            if column not in token_columns:
                connection.execute(f"ALTER TABLE api_tokens ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if "monthly_budget_usd" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN monthly_budget_usd REAL NOT NULL DEFAULT 0")
        if "budget_action" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN budget_action TEXT NOT NULL DEFAULT 'warn'")
        if "note" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "ip_allowlist" not in token_columns:
            connection.execute("ALTER TABLE api_tokens ADD COLUMN ip_allowlist TEXT NOT NULL DEFAULT '[]'")
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_usage)")}
        if "visible" not in usage_columns:
            connection.execute("ALTER TABLE api_usage ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        usage_additions = {
            "resolved_model": "TEXT NOT NULL DEFAULT ''",
            "upstream": "TEXT NOT NULL DEFAULT ''",
            "status_code": "INTEGER",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "error": "TEXT NOT NULL DEFAULT ''",
            "cached_tokens": "INTEGER NOT NULL DEFAULT 0",
            "first_byte_ms": "REAL",
            "termination_reason": "TEXT NOT NULL DEFAULT ''",
            "cost_usd": "REAL NOT NULL DEFAULT 0",
        }
        for column, definition in usage_additions.items():
            if column not in usage_columns:
                connection.execute(f"ALTER TABLE api_usage ADD COLUMN {column} {definition}")
        connection.execute("DELETE FROM api_usage WHERE at < ?", (int(time.time()) - 400 * 86400,))
        connection.execute("DELETE FROM connectivity_history WHERE checked_at < ?", (time.time() - 8 * 86400,))
        connection.execute("DELETE FROM audit_logs WHERE created_at < ?", (int(time.time()) - 400 * 86400,))
        INITIALIZED_DATABASES.add(key)
    return connection


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def record_audit(actor: str, source_ip: str, action: str, resource: str, status_code: int) -> None:
    try:
        with database_connection() as database:
            database.execute(
                "INSERT INTO audit_logs(created_at, actor, source_ip, action, resource, status_code) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), actor[:80], source_ip[:80], action[:80], resource[:80], status_code),
            )
            database.execute(
                "DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 10000)"
            )
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not persist audit log: %s", exc)


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


def token_ip_rules(item: dict[str, Any]) -> list[str]:
    value = item.get("ip_allowlist", "[]")
    try:
        rules = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return [str(rule) for rule in rules if str(rule).strip()] if isinstance(rules, list) else []


def token_allows_ip(item: dict[str, Any], source_ip: str) -> bool:
    rules = token_ip_rules(item)
    if not rules:
        return True
    try:
        address = ipaddress.ip_address(source_ip)
        return any(address in ipaddress.ip_network(rule, strict=False) for rule in rules)
    except ValueError:
        return False


def pricing_for(settings: dict[str, Any], model: str, upstream: str = "") -> dict[str, float]:
    for item in settings.get("model_pricing", []):
        if not isinstance(item, dict) or not fnmatch.fnmatchcase(model, str(item.get("pattern") or "")):
            continue
        configured_upstream = str(item.get("upstream") or "")
        if configured_upstream and configured_upstream != upstream:
            continue
        return {key: max(0.0, float(item.get(key) or 0)) for key in ("input_price", "output_price", "cached_price")}
    return {"input_price": 0.0, "output_price": 0.0, "cached_price": 0.0}


def usage_cost_usd(settings: dict[str, Any], model: str, upstream: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    prices = pricing_for(settings, model, upstream)
    cached = min(max(0, cached_tokens), max(0, input_tokens))
    regular_input = max(0, input_tokens - cached)
    return round((regular_input * prices["input_price"] + output_tokens * prices["output_price"] + cached * prices["cached_price"]) / 1_000_000, 8)


async def send_webhook_alert(kind: str, title: str, message: str) -> None:
    settings = load_settings()
    url = str(settings.get("webhook_url") or "")
    if not url:
        return
    key = f"{kind}:{title}"
    if ALERT_COOLDOWNS.get(key, 0) > time.time():
        return
    ALERT_COOLDOWNS[key] = time.time() + 15 * 60
    payload = {"title": title, "content": message, "message": message, "level": "warning", "source": "CleanLLM", "event": kind}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        logger.warning("Webhook alert delivery failed: %s", exc)


def schedule_alert(kind: str, title: str, message: str) -> None:
    try:
        asyncio.get_running_loop().create_task(send_webhook_alert(kind, title, message))
    except RuntimeError:
        pass


def token_limit_error(item: dict[str, Any], now: int, estimated_tokens: int, model: str = "") -> str:
    token_id = str(item.get("id") or "")
    local_now = datetime.fromtimestamp(now).astimezone()
    day_start = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month_start = int(local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    with database_connection() as database:
        row = database.execute(
            """SELECT
               SUM(CASE WHEN at >= ? THEN 1 ELSE 0 END) AS rpm,
               SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS tpm,
               SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS daily_tokens,
               SUM(CASE WHEN at >= ? THEN total_tokens ELSE 0 END) AS monthly_tokens,
               SUM(CASE WHEN at >= ? THEN cost_usd ELSE 0 END) AS monthly_cost
               FROM api_usage WHERE token_id = ?""",
            (now - 60, now - 60, day_start, month_start, month_start, token_id),
        ).fetchone()
    checks = (
        ("rpm_limit", int(row["rpm"] or 0) + 1, "每分钟请求数"),
        ("tpm_limit", int(row["tpm"] or 0) + estimated_tokens, "每分钟 Token"),
        ("daily_token_limit", int(row["daily_tokens"] or 0) + estimated_tokens, "每日 Token"),
        ("monthly_token_limit", int(row["monthly_tokens"] or 0) + estimated_tokens, "每月 Token"),
    )
    for key, projected, label in checks:
        limit = max(0, int(item.get(key, 0) or 0))
        if limit and projected > limit:
            return f"API令牌已达到{label}限额"
    budget = max(0.0, float(item.get("monthly_budget_usd") or 0))
    if budget:
        estimated_cost = usage_cost_usd(load_settings(), model, "", estimated_tokens, 0)
        projected_cost = float(row["monthly_cost"] or 0) + estimated_cost
        if projected_cost >= budget:
            schedule_alert("token_budget", f"令牌预算已用尽：{item.get('name', '')}", f"本月预计费用 ${projected_cost:.4f}，预算 ${budget:.4f}")
            if item.get("budget_action") == "disable":
                with database_connection() as database:
                    database.execute("UPDATE api_tokens SET enabled = 0 WHERE id = ?", (token_id,))
                return "API令牌已达到每月费用预算并自动停用"
    return ""


async def require_api_token(request: Request) -> None:
    now = int(time.time())
    configured = database_tokens()
    matched = None
    if configured:
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
        source_ip = request.client.host if request.client else ""
        if not token_allows_ip(matched, source_ip):
            raise HTTPException(status_code=403, detail="当前来源 IP 不在 API令牌白名单中")
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
    if matched and model and not token_allows_model(matched, model):
        raise HTTPException(status_code=403, detail=f"当前 API令牌无权使用模型：{model}")
    if matched:
        limit_error = token_limit_error(matched, now, input_tokens, model)
        if limit_error:
            raise HTTPException(status_code=429, detail=limit_error)
    request.state.api_token = matched
    usage_id = uuid.uuid4().hex
    request.state.usage_id = usage_id
    usage_item = {"id": usage_id, "token_id": matched.get("id") if matched else None, "token_name": matched.get("name", "") if matched else "未鉴权", "at": now, "path": request.url.path, "method": request.method, "model": model, "latency_ms": None, "input_tokens": input_tokens, "output_tokens": 0, "total_tokens": input_tokens}
    with database_connection() as database:
        if matched:
            database.execute(
                "UPDATE api_tokens SET last_used_at = ?, total_tokens = total_tokens + ? WHERE id = ?",
                (now, input_tokens, matched.get("id")),
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
    allowed = {"latency_ms", "first_byte_ms", "input_tokens", "output_tokens", "cached_tokens", "total_tokens", "resolved_model", "upstream", "status_code", "attempts", "error", "termination_reason"}
    changes = {key: value for key, value in values.items() if key in allowed}
    if "error" in changes:
        changes["error"] = str(changes["error"] or "")[:500]
    if not changes:
        return
    with database_connection() as database:
        current = database.execute("SELECT * FROM api_usage WHERE id = ?", (usage_id,)).fetchone()
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
        updated = database.execute("SELECT * FROM api_usage WHERE id = ?", (usage_id,)).fetchone()
        if updated is not None:
            settings = load_settings()
            model = str(updated["resolved_model"] or updated["model"] or "")
            cost = usage_cost_usd(
                settings, model, str(updated["upstream"] or ""),
                int(updated["input_tokens"] or 0), int(updated["output_tokens"] or 0),
                int(updated["cached_tokens"] or 0),
            )
            database.execute("UPDATE api_usage SET cost_usd = ? WHERE id = ?", (cost, usage_id))
            if updated["token_id"]:
                token = database.execute("SELECT * FROM api_tokens WHERE id = ?", (updated["token_id"],)).fetchone()
                if token is not None and float(token["monthly_budget_usd"] or 0) > 0:
                    local_now = datetime.now().astimezone()
                    month_start = int(local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
                    month_cost = float(database.execute(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE token_id = ? AND at >= ?",
                        (updated["token_id"], month_start),
                    ).fetchone()[0])
                    budget = float(token["monthly_budget_usd"])
                    percent = int(settings.get("alert_budget_percent", 80))
                    if month_cost >= budget * percent / 100:
                        schedule_alert("token_budget", f"令牌预算提醒：{token['name']}", f"本月费用 ${month_cost:.4f}，预算 ${budget:.4f}")
                    if month_cost >= budget and token["budget_action"] == "disable":
                        database.execute("UPDATE api_tokens SET enabled = 0 WHERE id = ?", (updated["token_id"],))


def update_usage_record_safely(usage_id: str | None, **values: Any) -> None:
    """Usage accounting must never interrupt an upstream model response."""
    try:
        update_usage_record(usage_id, **values)
    except Exception as exc:
        logger.warning("Could not persist API usage update: %s", exc)


def check_error_rate_alert() -> None:
    settings = load_settings()
    if not settings.get("webhook_url"):
        return
    with database_connection() as database:
        row = database.execute(
            """SELECT COUNT(*) calls,
                      SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors
               FROM api_usage WHERE at >= ?""", (int(time.time()) - 300,),
        ).fetchone()
    calls = int(row["calls"] or 0)
    errors = int(row["errors"] or 0)
    rate = errors / calls * 100 if calls else 0
    threshold = int(settings.get("alert_error_rate_percent", 30))
    if calls >= 10 and rate >= threshold:
        schedule_alert("error_rate", "代理错误率升高", f"最近 5 分钟 {calls} 次请求中失败 {errors} 次，错误率 {rate:.1f}%")


def usage_values(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict) or not usage:
        return {}
    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens", usage.get("cached_tokens", 0)) or 0) if isinstance(input_details, dict) else 0
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens, "total_tokens": total_tokens}


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


def upstream_records(settings: dict[str, Any]) -> list[dict[str, Any]]:
    primary = {
        "name": str(settings.get("default_upstream_name") or "默认上游"),
        "url": chat_url_for_target(str(settings["target_api_url"])),
        "enabled": bool(settings.get("default_upstream_enabled", True)),
        "api_key": str(settings.get("api_key") or ""),
        "timeout": int(settings.get("timeout_seconds") or 120),
        "models_url": str(settings.get("models_api_url") or ""),
        "ollama_url": str(settings.get("ollama_api_url") or ""),
        "clean_patterns": list(settings.get("clean_patterns") or []),
        "model_routes": list(settings.get("model_routes") or []),
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
            "enabled": bool(item.get("enabled", True)),
            "api_key": str(item.get("api_key") or ""),
            "timeout": max(1, min(int(item.get("timeout") or 120), 3600)),
            "models_url": str(item.get("models_url") or ""),
            "ollama_url": str(item.get("ollama_url") or ""),
            "clean_patterns": list(item.get("clean_patterns") or settings.get("clean_patterns") or []),
            "model_routes": list(item.get("model_routes") or []),
        })
    return result


def configured_upstreams(settings: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for item in upstream_records(settings):
        if not item["enabled"]:
            continue
        result.append({
            **item,
            "models_url": models_url_for_target(item["url"], item["models_url"]),
        })
    return result


def apply_upstream_records(settings: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    primary, *secondary = records
    settings.update({
        "default_upstream_name": primary["name"],
        "default_upstream_enabled": bool(primary.get("enabled", True)),
        "target_api_url": primary["url"],
        "api_key": primary.get("api_key", ""),
        "timeout_seconds": primary.get("timeout", 120),
        "models_api_url": primary.get("models_url", ""),
        "ollama_api_url": primary.get("ollama_url", ""),
        "clean_patterns": primary.get("clean_patterns", []),
        "model_routes": primary.get("model_routes", []),
        "upstreams": secondary,
    })
    return settings


def virtual_model(settings: dict[str, Any], requested_model: str) -> dict[str, Any] | None:
    for item in settings.get("virtual_models", []):
        if isinstance(item, dict) and str(item.get("alias") or "") == requested_model:
            return item
    return None


def resolved_model(settings: dict[str, Any], requested_model: str) -> str:
    item = virtual_model(settings, requested_model)
    return str(item.get("target") or requested_model) if item else requested_model


def circuit_is_open(name: str, now: float | None = None) -> bool:
    state = UPSTREAM_CIRCUITS.get(name) or {}
    return float(state.get("open_until") or 0) > (time.time() if now is None else now)


def record_upstream_success(name: str) -> None:
    if name:
        UPSTREAM_CIRCUITS.pop(name, None)


def release_upstream_probe(name: str) -> None:
    """Release a half-open lease after a non-circuit compatibility response."""
    state = UPSTREAM_CIRCUITS.get(name)
    if state and state.get("phase") == "half_open":
        state["probe_in_flight"] = False


def acquire_upstream(name: str, now: float | None = None) -> bool:
    """Return whether a request may use an upstream, allowing one half-open probe."""
    state = UPSTREAM_CIRCUITS.get(name)
    if not state:
        return True
    current = time.time() if now is None else now
    if float(state.get("open_until") or 0) > current:
        state["phase"] = "open"
        return False
    if int(state.get("failures") or 0) <= 0 or state.get("phase", "closed") == "closed":
        return True
    state["phase"] = "half_open"
    state["open_until"] = 0.0
    if state.get("probe_in_flight"):
        return False
    state["probe_in_flight"] = True
    state["probe_started_at"] = current
    return True


def upstream_is_routable(name: str, now: float | None = None) -> bool:
    state = UPSTREAM_CIRCUITS.get(name) or {}
    return float(state.get("open_until") or 0) <= (time.time() if now is None else now)


def record_upstream_failure(settings: dict[str, Any], name: str, error: str) -> None:
    record_typed_upstream_failure(settings, name, error, "transient")


def retry_after_seconds(headers: Any) -> int:
    value = str(headers.get("retry-after", "") if headers else "").strip()
    if not value:
        return 0
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            return max(0, int(retry_at.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return 0


def record_typed_upstream_failure(
    settings: dict[str, Any],
    name: str,
    error: str,
    category: str = "transient",
    retry_after: int = 0,
) -> None:
    """Record failures with conservative, error-specific cooldown durations."""
    if not name:
        return
    state = UPSTREAM_CIRCUITS.setdefault(name, {
        "failures": 0, "open_until": 0.0, "last_error": "", "phase": "closed",
        "probe_in_flight": False, "open_count": 0,
    })
    threshold = max(1, int(settings.get("circuit_breaker_failures", 3)))
    if category == "stream":
        threshold = max(3, threshold)
    state["failures"] = int(state.get("failures") or 0) + 1
    state["last_error"] = str(error or "上游请求失败")[:500]
    state["last_failure_at"] = time.time()
    if state["failures"] < threshold:
        state["probe_in_flight"] = False
        return
    base = max(5, int(settings.get("circuit_breaker_cooldown_seconds", 60)))
    if category == "rate_limit":
        cooldown = max(5, min(retry_after or 30, 120))
    elif category == "auth":
        cooldown = 300 if int(state.get("open_count") or 0) == 0 else 1800
    else:
        cooldown = min(base * (2 ** min(int(state.get("open_count") or 0), 3)), 300)
    state["open_count"] = int(state.get("open_count") or 0) + 1
    state["open_until"] = time.time() + cooldown
    state["phase"] = "open"
    state["probe_in_flight"] = False
    logger.warning("Upstream circuit opened: %s (%s, %s seconds)", name, state["last_error"], cooldown)
    if state["failures"] >= int(settings.get("alert_upstream_failures", threshold)):
        schedule_alert("upstream_offline", f"上游已熔断：{name}", f"连续失败 {state['failures']} 次，冷却 {cooldown} 秒；{state['last_error']}")


def record_upstream_http_result(
    settings: dict[str, Any], name: str, status_code: int, headers: Any = None
) -> None:
    if status_code == 429:
        record_typed_upstream_failure(
            settings, name, "HTTP 429", "rate_limit", retry_after_seconds(headers)
        )
    elif status_code >= 500:
        record_typed_upstream_failure(settings, name, f"HTTP {status_code}", "transient")
    else:
        release_upstream_probe(name)


def virtual_route_matches(route: dict[str, Any], token_name: str, token_id: str, path: str, at: datetime) -> bool:
    token_patterns = route.get("token_patterns") or []
    path_patterns = route.get("path_patterns") or []
    if token_patterns and not any(
        fnmatch.fnmatchcase(value, pattern)
        for pattern in token_patterns
        for value in (token_name, token_id)
        if value
    ):
        return False
    if path_patterns and not any(fnmatch.fnmatchcase(path, pattern) for pattern in path_patterns):
        return False
    start, end = str(route.get("start_time") or ""), str(route.get("end_time") or "")
    if start and end:
        current = at.strftime("%H:%M")
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end
    return True


def weighted_virtual_upstreams(routes: list[dict[str, Any]], seed: str) -> list[str]:
    ordered: list[str] = []
    priorities = sorted({int(route.get("priority") or 0) for route in routes})
    for priority in priorities:
        group = [route for route in routes if int(route.get("priority") or 0) == priority]
        while group:
            digest = hashlib.sha256(f"{seed}:{len(ordered)}".encode()).digest()
            point = int.from_bytes(digest[:8], "big") % sum(max(1, int(item.get("weight") or 1)) for item in group)
            total = 0
            selected = group[-1]
            for item in group:
                total += max(1, int(item.get("weight") or 1))
                if point < total:
                    selected = item
                    break
            name = str(selected.get("upstream") or "")
            if name and name not in ordered:
                ordered.append(name)
            group.remove(selected)
    return ordered


def route_upstreams(settings: dict[str, Any], model: str, request: Request | None = None) -> list[dict[str, Any]]:
    route_key = f"{model}|{request.url.path if request else ''}"
    cached_until = NO_ROUTE_CACHE.get(route_key, 0.0)
    if cached_until > time.time():
        return []
    if cached_until:
        NO_ROUTE_CACHE.pop(route_key, None)
    upstreams = configured_upstreams(settings)
    names = {item["name"]: item for item in upstreams}
    preferred: list[dict[str, Any]] = []
    virtual = virtual_model(settings, model)
    if virtual:
        token = getattr(request.state, "api_token", None) if request else None
        token_name = str((token or {}).get("name") or "")
        token_id = str((token or {}).get("id") or "")
        path = request.url.path if request else ""
        routes = [route for route in virtual.get("routes", []) if virtual_route_matches(route, token_name, token_id, path, datetime.now().astimezone())]
        seed = str(getattr(request.state, "usage_id", "") if request else model)
        route_names = weighted_virtual_upstreams(routes, seed) if routes else []
        for name in [*route_names, *virtual.get("upstreams", [])]:
            if name in names and names[name] not in preferred:
                preferred.append(names[name])
    for route in settings.get("model_routes", []):
        if not isinstance(route, dict) or not fnmatch.fnmatchcase(model, str(route.get("pattern") or "")):
            continue
        for name in route.get("upstreams", []):
            if name in names and names[name] not in preferred:
                preferred.append(names[name])
        break
    affinity = ROUTE_AFFINITY.get(model)
    if not preferred and affinity and affinity[1] > time.time():
        preferred = [item for item in upstreams if item["name"] == affinity[0]]
        if not preferred:
            ROUTE_AFFINITY.pop(model, None)
    elif affinity and affinity[1] <= time.time():
        ROUTE_AFFINITY.pop(model, None)
    if not preferred and MODEL_CACHE.get("data"):
        lookup_model = resolved_model(settings, model)
        available = [item.get("upstream") for item in MODEL_CACHE["data"] if item.get("id") == lookup_model]
        if available:
            preferred = [item for name in available for item in upstreams if item["name"] == name]
    ordered = preferred + [item for item in upstreams if item not in preferred]
    available = [item for item in ordered if upstream_is_routable(item["name"])]
    # When discovery has explicitly declared endpoint support, avoid sending
    # an incompatible protocol to that upstream. Missing metadata remains
    # eligible so non-standard providers continue to work.
    if request is not None and MODEL_CACHE.get("data"):
        endpoint = request.url.path.removeprefix("/")
        lookup_model = resolved_model(settings, model)
        compatible = []
        declared_any = False
        for item in available:
            records = [record for record in MODEL_CACHE["data"] if record.get("id") == lookup_model and record.get("upstream") == item["name"]]
            declared = [interface for record in records for interface in (record.get("interfaces") or [])]
            declared_any = declared_any or bool(declared)
            if declared and endpoint not in {str(value).lstrip("/") for value in declared}:
                continue
            compatible.append(item)
        if declared_any:
            available = compatible
    if not available:
        NO_ROUTE_CACHE[route_key] = time.time() + 30.0
    return available


def remember_route(settings: dict[str, Any], model: str, upstream_name: str) -> None:
    minutes = max(0, int(settings.get("route_affinity_minutes", 15)))
    for key in list(NO_ROUTE_CACHE):
        if key.startswith(f"{model}|"):
            NO_ROUTE_CACHE.pop(key, None)
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


@app.exception_handler(HTTPException)
async def http_exception_response(request: Request, exc: HTTPException) -> JSONResponse:
    if request.url.path.startswith("/v1/") and exc.status_code == 429:
        return JSONResponse(
            status_code=429,
            content={"error": {"message": str(exc.detail), "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            headers=exc.headers,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


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
        usage_id = getattr(request.state, "usage_id", None)
        update_usage_record_safely(
            usage_id,
            latency_ms=round(elapsed_ms, 1),
            status_code=response.status_code,
        )
        if usage_id:
            check_error_rate_alert()
        if usage_id:
            response.headers["X-Request-ID"] = usage_id
        logger.info("%s %s -> %s (%.1f ms)", request.method, request.url.path, response.status_code, elapsed_ms)
        publish_status("request", {"path": request.url.path, "status": response.status_code, "latency_ms": round(elapsed_ms)})
        audit = audit_event_for(request.method, request.url.path)
        if audit and response.status_code < 400 and valid_session(request.cookies.get(SESSION_COOKIE)):
            actor, _ = configured_credentials()
            record_audit(actor, request.client.host if request.client else "", audit[0], audit[1], response.status_code)
    return response


def audit_event_for(method: str, path: str) -> tuple[str, str] | None:
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if path == "/api/settings":
        return "更新配置", "系统配置"
    if path.startswith("/api/settings/snapshots"):
        return ("恢复配置快照" if path.endswith("/restore") else "创建配置快照", "系统配置")
    if path in {"/api/settings/import", "/api/settings/reset"}:
        return ("导入配置" if path.endswith("/import") else "恢复默认配置", "系统配置")
    if path.startswith("/api/upstreams"):
        return ("修改上游", "上游配置")
    if path.startswith("/api/tokens"):
        return ({"POST": "创建令牌", "PATCH": "修改令牌", "DELETE": "删除令牌"}.get(method, "修改令牌"), "API令牌")
    if path.startswith("/api/ollama"):
        return ({"POST": "执行模型操作", "DELETE": "删除模型"}.get(method, "修改模型"), "Ollama 模型")
    if path == "/api/account":
        return "更新管理员账户", "账户安全"
    if path == "/api/system/restart":
        return "重启服务", "系统"
    if path in {"/api/logs", "/api/usage/logs"} and method == "DELETE":
        return "清除日志", "日志"
    return None


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


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "alive", "version": APP_VERSION}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    try:
        with database_connection() as database:
            database.execute("SELECT 1").fetchone()
        settings = load_settings()
        ready = bool(str(settings.get("target_api_url") or ""))
    except (OSError, sqlite3.Error):
        ready = False
    return JSONResponse(status_code=200 if ready else 503, content={"status": "ready" if ready else "not_ready"})


def prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    since = int(time.time()) - 400 * 86400
    with database_connection() as database:
        total = database.execute(
            """SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens,
                      COALESCE(SUM(cost_usd),0) cost,
                      SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors
               FROM api_usage WHERE at >= ?""", (since,),
        ).fetchone()
        upstreams = database.execute(
            """SELECT COALESCE(NULLIF(upstream,''),'unassigned') upstream, COUNT(*) calls,
                      SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors,
                      COALESCE(AVG(latency_ms),0) latency
               FROM api_usage WHERE at >= ? GROUP BY upstream""", (since,),
        ).fetchall()
    lines = [
        "# HELP cleanllm_requests_total Total proxied requests retained by CleanLLM.",
        "# TYPE cleanllm_requests_total counter",
        f"cleanllm_requests_total {int(total['calls'] or 0)}",
        "# TYPE cleanllm_request_errors_total counter",
        f"cleanllm_request_errors_total {int(total['errors'] or 0)}",
        "# TYPE cleanllm_tokens_total counter",
        f"cleanllm_tokens_total {int(total['tokens'] or 0)}",
        "# TYPE cleanllm_estimated_cost_usd_total counter",
        f"cleanllm_estimated_cost_usd_total {float(total['cost'] or 0):.8f}",
    ]
    usage_by_upstream = {str(row["upstream"]): row for row in upstreams}
    configured_names = [item["name"] for item in configured_upstreams(load_settings())]
    metric_names = list(dict.fromkeys([*configured_names, *(name for name in usage_by_upstream if name != "unassigned")]))
    for upstream_name in metric_names:
        row = usage_by_upstream.get(upstream_name)
        label = prometheus_label(upstream_name)
        lines.extend([
            f'cleanllm_upstream_requests_total{{upstream="{label}"}} {int(row["calls"] or 0) if row else 0}',
            f'cleanllm_upstream_errors_total{{upstream="{label}"}} {int(row["errors"] or 0) if row else 0}',
            f'cleanllm_upstream_latency_milliseconds{{upstream="{label}"}} {float(row["latency"] or 0) if row else 0:.3f}',
            f'cleanllm_upstream_circuit_open{{upstream="{label}"}} {1 if circuit_is_open(upstream_name) else 0}',
        ])
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/settings")
async def get_settings(_: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    return {key: settings[key] for key in DEFAULT_SETTINGS if key not in {"api_tokens", "export_history", "api_usage"}}


@app.put("/api/settings")
async def update_settings(
    update: SettingsUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    settings = load_settings()
    create_config_snapshot_safely("自动：保存设置前", settings)
    settings.update(update.model_dump(mode="json"))
    save_settings(settings)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    UPSTREAM_CIRCUITS.clear()
    CONNECTIVITY_WAKEUP.set()
    return {"message": "设置已保存"}


def create_config_snapshot(reason: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    item = {
        "id": uuid.uuid4().hex,
        "created_at": int(time.time()),
        "reason": reason.strip()[:200] or "配置快照",
        "settings_json": json.dumps(settings or load_settings(), ensure_ascii=False),
    }
    with database_connection() as database:
        database.execute(
            "INSERT INTO config_snapshots(id, created_at, reason, settings_json) VALUES (?, ?, ?, ?)",
            (item["id"], item["created_at"], item["reason"], item["settings_json"]),
        )
        database.execute(
            "DELETE FROM config_snapshots WHERE id NOT IN (SELECT id FROM config_snapshots ORDER BY created_at DESC LIMIT 30)"
        )
    return {key: item[key] for key in ("id", "created_at", "reason")}


def create_config_snapshot_safely(reason: str, settings: dict[str, Any] | None = None) -> None:
    try:
        create_config_snapshot(reason, settings)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Could not create automatic configuration snapshot: %s", exc)


@app.get("/api/settings/snapshots")
async def list_config_snapshots(_: None = Depends(require_admin)) -> dict[str, Any]:
    with database_connection() as database:
        rows = database.execute(
            "SELECT id, created_at, reason, settings_json FROM config_snapshots ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    current = load_settings()
    data = []
    for row in rows:
        item = row_dict(row)
        try:
            snapshot = json.loads(item.pop("settings_json"))
            item["changed_keys"] = sorted(key for key in set(current) | set(snapshot) if current.get(key) != snapshot.get(key))
        except (TypeError, ValueError, json.JSONDecodeError):
            item.pop("settings_json", None)
            item["changed_keys"] = ["快照数据损坏"]
        data.append(item)
    return {"data": data}


@app.post("/api/settings/snapshots")
async def make_config_snapshot(update: SnapshotRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    return {"message": "配置快照已创建", **create_config_snapshot(update.reason)}


@app.post("/api/settings/snapshots/{snapshot_id}/restore")
async def restore_config_snapshot(snapshot_id: str, _: None = Depends(require_admin)) -> dict[str, str]:
    with database_connection() as database:
        row = database.execute("SELECT settings_json FROM config_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="配置快照不存在")
    try:
        restored = SettingsUpdate.model_validate(json.loads(row["settings_json"]))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"配置快照已损坏：{exc}") from exc
    create_config_snapshot_safely("自动：恢复快照前")
    current = load_settings()
    current.update(restored.model_dump(mode="json"))
    save_settings(current)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    UPSTREAM_CIRCUITS.clear()
    CONNECTIVITY_WAKEUP.set()
    return {"message": "配置已恢复"}


@app.get("/api/backup")
async def download_full_backup(_: None = Depends(require_admin)) -> StreamingResponse:
    temporary = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if SETTINGS_FILE.is_file():
            archive.write(SETTINGS_FILE, "settings.json")
        with database_connection() as source:
            disk = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
            disk.close()
            target = None
            try:
                target = sqlite3.connect(disk.name)
                source.backup(target)
                archive.write(disk.name, "cleanllm.db")
            finally:
                if target is not None:
                    target.close()
                Path(disk.name).unlink(missing_ok=True)
        backgrounds = DATA_DIR / "backgrounds"
        if backgrounds.is_dir():
            for path in backgrounds.iterdir():
                if path.is_file():
                    archive.write(path, f"backgrounds/{path.name}")
        archive.writestr("manifest.json", json.dumps({"app": "CleanLLM", "version": APP_VERSION, "created_at": int(time.time())}))
    temporary.seek(0)
    filename = f"cleanllm-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        temporary,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(temporary.close),
    )


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
                    probe = await client.get(upstream["models_url"], headers=headers, timeout=8.0)
                    probe.raise_for_status()
                    detail = "模型列表接口可达（不代表流式生成兼容）"
                    results.append({"name": upstream["name"], "ok": True, "latency_ms": round((time.perf_counter()-started)*1000), "detail": detail})
                    state = UPSTREAM_CIRCUITS.get(upstream["name"]) or {}
                    if state.get("failures") and float(state.get("open_until") or 0) <= time.time():
                        record_upstream_success(upstream["name"])
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
            try:
                await asyncio.wait_for(CONNECTIVITY_WAKEUP.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            else:
                CONNECTIVITY_WAKEUP.clear()
                continue
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
    settings = load_settings()
    history = database_connectivity_history()
    current = {str(item.get("name") or ""): dict(item) for item in CONNECTIVITY_STATE.get("results", [])}
    ordered_results = []
    checked_at = float(CONNECTIVITY_STATE.get("checked_at") or time.time())
    for upstream in configured_upstreams(settings):
        item = current.get(upstream["name"], {
            "name": upstream["name"],
            "ok": None,
            "latency_ms": None,
            "detail": "等待检测",
        })
        item.update(connectivity_metrics(history, upstream["name"], checked_at))
        ordered_results.append(item)
    tested = [item for item in ordered_results if item.get("ok") is not None]
    availability = round(sum(1 for item in tested if item["ok"]) / len(tested) * 100, 1) if tested else None
    return {
        "checked_at": CONNECTIVITY_STATE.get("checked_at"),
        "results": ordered_results,
        "availability_percent": availability,
    }


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
    try:
        validated = SettingsUpdate.model_validate(candidate).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"导入配置无效：{exc}") from exc
    current.update(validated)
    save_settings(current)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    UPSTREAM_CIRCUITS.clear()
    CONNECTIVITY_WAKEUP.set()
    return {"message": "配置已导入"}


@app.get("/api/upstreams/export")
async def export_upstreams(_: None = Depends(require_admin)) -> Response:
    records = upstream_records(load_settings())
    for item in records:
        item["api_key"] = ""
    payload = json.dumps({"version": 1, "upstreams": records}, ensure_ascii=False, indent=2)
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=cleanllm-upstreams.json"},
    )


@app.post("/api/upstreams/import")
async def import_upstreams(update: UpstreamImportRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    current = upstream_records(settings)
    imported = update.upstreams
    combined = imported if update.mode == "replace" else [*current, *imported]
    used: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(combined, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"上游 {index} 必须是对象")
        base_name = str(item.get("name") or f"上游 {index}").strip()
        name = base_name
        suffix = 2
        while name in used:
            name = f"{base_name} {suffix}"
            suffix += 1
        used.add(name)
        normalized.append({**item, "name": name, "api_key": str(item.get("api_key") or "")})
    candidate = apply_upstream_records(copy.deepcopy(settings), normalized)
    try:
        validated = SettingsUpdate.model_validate(candidate).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"上游配置无效：{exc}") from exc
    settings.update(validated)
    create_config_snapshot_safely("自动：导入上游前")
    save_settings(settings)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    CONNECTIVITY_WAKEUP.set()
    return {"message": f"已导入 {len(imported)} 个上游", "count": len(normalized)}


@app.patch("/api/upstreams/status")
async def update_upstream_status(update: UpstreamBatchStatusRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    settings = load_settings()
    records = upstream_records(settings)
    selected = set(update.names)
    matched = 0
    for item in records:
        if item["name"] in selected:
            item["enabled"] = update.enabled
            matched += 1
    if not matched:
        raise HTTPException(status_code=404, detail="未找到需要修改的上游")
    candidate = apply_upstream_records(copy.deepcopy(settings), records)
    try:
        validated = SettingsUpdate.model_validate(candidate).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"上游状态无效：{exc}") from exc
    settings.update(validated)
    create_config_snapshot_safely("自动：批量修改上游前")
    save_settings(settings)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    CONNECTIVITY_WAKEUP.set()
    return {"message": f"已{('启用' if update.enabled else '停用')} {matched} 个上游"}


@app.post("/api/settings/reset")
async def reset_settings(_: None = Depends(require_admin)) -> dict[str, str]:
    current = load_settings()
    preserved = {key: current[key] for key in ("admin_username", "admin_password_hash", "_auth_revision") if key in current}
    reset = copy.deepcopy(DEFAULT_SETTINGS)
    reset.update(preserved)
    save_settings(reset)
    MODEL_CACHE.update({"at": 0.0, "data": None, "source": ""})
    ROUTE_AFFINITY.clear()
    UPSTREAM_CIRCUITS.clear()
    CONNECTIVITY_WAKEUP.set()
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
            "rpm_limit": max(0, int(item.get("rpm_limit", 0) or 0)),
            "tpm_limit": max(0, int(item.get("tpm_limit", 0) or 0)),
            "daily_token_limit": max(0, int(item.get("daily_token_limit", 0) or 0)),
            "monthly_token_limit": max(0, int(item.get("monthly_token_limit", 0) or 0)),
            "monthly_budget_usd": max(0.0, float(item.get("monthly_budget_usd", 0) or 0)),
            "budget_action": str(item.get("budget_action") or "warn"),
            "note": str(item.get("note") or ""),
            "ip_allowlist": token_ip_rules(item),
        })
    return {"data": result}


@app.post("/api/tokens")
async def create_api_token(update: ApiTokenRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    raw = "cln_" + secrets.token_urlsafe(24)
    token_id = uuid.uuid4().hex
    item = {"id": token_id, "name": update.name.strip(), "hash": token_digest(raw), "cipher": token_cipher(raw, token_id), "created_at": int(time.time()), "last_used_at": None, "expires_at": update.expires_at, "enabled": 1, "total_tokens": 0, "allowed_models": json.dumps(update.allowed_models, ensure_ascii=False), "rpm_limit": update.rpm_limit, "tpm_limit": update.tpm_limit, "daily_token_limit": update.daily_token_limit, "monthly_token_limit": update.monthly_token_limit, "monthly_budget_usd": update.monthly_budget_usd, "budget_action": update.budget_action, "note": update.note.strip(), "ip_allowlist": json.dumps(update.ip_allowlist, ensure_ascii=False)}
    with database_connection() as database:
        database.execute(
            """INSERT INTO api_tokens
               (id, name, hash, cipher, created_at, last_used_at, expires_at, enabled, total_tokens, allowed_models,
                rpm_limit, tpm_limit, daily_token_limit, monthly_token_limit, monthly_budget_usd, budget_action, note, ip_allowlist)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(item[key] for key in ("id", "name", "hash", "cipher", "created_at", "last_used_at", "expires_at", "enabled", "total_tokens", "allowed_models", "rpm_limit", "tpm_limit", "daily_token_limit", "monthly_token_limit", "monthly_budget_usd", "budget_action", "note", "ip_allowlist")),
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
            """UPDATE api_tokens SET expires_at = ?, allowed_models = ?, rpm_limit = ?, tpm_limit = ?,
               daily_token_limit = ?, monthly_token_limit = ?, monthly_budget_usd = ?, budget_action = ?, note = ?, ip_allowlist = ? WHERE id = ?""",
            (update.expires_at, json.dumps(update.allowed_models, ensure_ascii=False), update.rpm_limit,
             update.tpm_limit, update.daily_token_limit, update.monthly_token_limit,
             update.monthly_budget_usd, update.budget_action, update.note.strip(),
             json.dumps(update.ip_allowlist, ensure_ascii=False), token_id),
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


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percent + 0.5)))
    return round(ordered[index], 1)


def analytics_data(days: int, group_by: str, interval: str = "day") -> dict[str, Any]:
    since = int(time.time()) - days * 86400
    group_column = {"model": "model", "upstream": "upstream", "token": "token_name"}[group_by]
    with database_connection() as database:
        overview = database.execute(
            """SELECT COUNT(*) calls, COALESCE(SUM(total_tokens),0) tokens,
                      COALESCE(SUM(cost_usd),0) cost,
                      SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors,
                      COALESCE(AVG(latency_ms),0) average_latency
               FROM api_usage WHERE at >= ?""", (since,),
        ).fetchone()
        groups = database.execute(
            f"""SELECT COALESCE(NULLIF({group_column}, ''), '未记录') name, COUNT(*) calls,
                       COALESCE(SUM(total_tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost,
                       SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors,
                       COALESCE(AVG(latency_ms),0) average_latency
                FROM api_usage WHERE at >= ? GROUP BY {group_column} ORDER BY calls DESC LIMIT 100""",
            (since,),
        ).fetchall()
        latency_rows = database.execute(
            f"SELECT COALESCE(NULLIF({group_column}, ''), '未记录') name, latency_ms FROM api_usage WHERE at >= ? AND latency_ms IS NOT NULL",
            (since,),
        ).fetchall()
        bucket_sql = {
            "day": "date(at, 'unixepoch', 'localtime')",
            "week": "strftime('%Y-W%W', at, 'unixepoch', 'localtime')",
            "month": "strftime('%Y-%m', at, 'unixepoch', 'localtime')",
        }[interval]
        trend_rows = database.execute(
            f"""SELECT {bucket_sql} bucket, COUNT(*) calls,
                      COALESCE(SUM(total_tokens),0) tokens, COALESCE(SUM(cost_usd),0) cost,
                      SUM(CASE WHEN status_code >= 400 OR error <> '' THEN 1 ELSE 0 END) errors
               FROM api_usage WHERE at >= ? GROUP BY bucket ORDER BY bucket""", (since,),
        ).fetchall()
    latencies: dict[str, list[float]] = {}
    all_latencies: list[float] = []
    for row in latency_rows:
        value = float(row["latency_ms"])
        latencies.setdefault(str(row["name"]), []).append(value)
        all_latencies.append(value)
    group_items = []
    for row in groups:
        calls = int(row["calls"] or 0)
        errors = int(row["errors"] or 0)
        values = latencies.get(str(row["name"]), [])
        group_items.append({
            "name": str(row["name"]), "calls": calls, "tokens": int(row["tokens"] or 0),
            "cost": round(float(row["cost"] or 0), 6), "errors": errors,
            "error_rate": round(errors / calls * 100, 1) if calls else 0,
            "average_latency": round(float(row["average_latency"] or 0), 1),
            "p50_latency": percentile(values, 0.50), "p95_latency": percentile(values, 0.95),
        })
    calls = int(overview["calls"] or 0)
    errors = int(overview["errors"] or 0)
    return {
        "days": days, "group_by": group_by, "interval": interval,
        "overview": {"calls": calls, "tokens": int(overview["tokens"] or 0), "cost": round(float(overview["cost"] or 0), 6), "errors": errors, "error_rate": round(errors / calls * 100, 1) if calls else 0, "average_latency": round(float(overview["average_latency"] or 0), 1), "p50_latency": percentile(all_latencies, .5), "p95_latency": percentile(all_latencies, .95)},
        "groups": group_items,
        "trend": [row_dict(row) for row in trend_rows],
    }


@app.get("/api/analytics")
async def usage_analytics(days: int = 30, group_by: str = "upstream", interval: str = "day", _: None = Depends(require_admin)) -> dict[str, Any]:
    if days not in {1, 7, 30, 90, 365}:
        raise HTTPException(status_code=400, detail="统计周期只支持 1、7、30、90 或 365 天")
    if group_by not in {"model", "upstream", "token"}:
        raise HTTPException(status_code=400, detail="分组只支持 model、upstream 或 token")
    if interval not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="趋势粒度只支持 day、week 或 month")
    return analytics_data(days, group_by, interval)


@app.get("/api/analytics/export")
async def export_usage_analytics(days: int = 30, group_by: str = "upstream", interval: str = "day", _: None = Depends(require_admin)) -> Response:
    if days not in {1, 7, 30, 90, 365} or group_by not in {"model", "upstream", "token"} or interval not in {"day", "week", "month"}:
        raise HTTPException(status_code=400, detail="统计参数无效")
    data = analytics_data(days, group_by, interval)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["名称", "请求数", "Token", "错误数", "错误率(%)", "平均延迟(ms)", "P50(ms)", "P95(ms)", "费用(USD)"])
    for item in data["groups"]:
        writer.writerow([item["name"], item["calls"], item["tokens"], item["errors"], item["error_rate"], item["average_latency"], item["p50_latency"], item["p95_latency"], item["cost"]])
    content = "\ufeff" + output.getvalue()
    return Response(content=content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="cleanllm-analytics-{days}d.csv"'})


@app.post("/api/alerts/test")
async def test_webhook(update: WebhookTestRequest, _: None = Depends(require_admin)) -> dict[str, str]:
    url = update.url or str(load_settings().get("webhook_url") or "")
    if not url:
        raise HTTPException(status_code=400, detail="请先填写 Webhook 地址")
    payload = {"title": "CleanLLM 测试通知", "content": "Webhook 配置有效", "message": "Webhook 配置有效", "level": "info", "source": "CleanLLM", "event": "test"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise HTTPException(status_code=502, detail=f"Webhook 测试失败：{exc}") from exc
    return {"message": "测试通知已发送"}


@app.get("/api/traces")
async def request_traces(
    limit: int = 100,
    offset: int = 0,
    request_id: str = "",
    model: str = "",
    token: str = "",
    upstream: str = "",
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    where = ["visible = 1"]
    parameters: list[Any] = []
    for column, value in (("id", request_id), ("token_name", token), ("upstream", upstream)):
        value = value.strip()
        if value:
            where.append(f"{column} LIKE ?")
            parameters.append(f"%{value}%")
    if model.strip():
        where.append("(model LIKE ? OR resolved_model LIKE ?)")
        parameters.extend([f"%{model.strip()}%", f"%{model.strip()}%"])
    clause = " AND ".join(where)
    with database_connection() as database:
        total = int(database.execute(f"SELECT COUNT(*) FROM api_usage WHERE {clause}", parameters).fetchone()[0])
        rows = database.execute(
            """SELECT id, token_name, at, path, method, model, resolved_model, upstream,
                      status_code, latency_ms, first_byte_ms, attempts, error, termination_reason
               FROM api_usage WHERE """ + clause + " ORDER BY at DESC, rowid DESC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
    return {"data": [row_dict(row) for row in rows], "total": total, "has_more": offset + len(rows) < total}


@app.get("/api/audit/logs")
async def audit_logs(limit: int = 100, _: None = Depends(require_admin)) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with database_connection() as database:
        rows = database.execute(
            "SELECT created_at, actor, source_ip, action, resource, status_code FROM audit_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"data": [row_dict(row) for row in rows]}


@app.get("/api/routing/status")
async def routing_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    now = time.time()
    data = []
    for upstream in configured_upstreams(settings):
        state = UPSTREAM_CIRCUITS.get(upstream["name"]) or {}
        open_until = float(state.get("open_until") or 0)
        if open_until > now:
            circuit_state = "open"
        elif state.get("phase") == "half_open" or state.get("failures"):
            circuit_state = "half_open"
        else:
            circuit_state = "closed"
        data.append({
            "name": upstream["name"],
            "state": circuit_state,
            "failures": int(state.get("failures") or 0),
            "open_until": open_until if open_until > now else None,
            "probe_in_flight": bool(state.get("probe_in_flight")),
            "last_error": str(state.get("last_error") or ""),
        })
    return {
        "failure_threshold": int(settings.get("circuit_breaker_failures", 3)),
        "cooldown_seconds": int(settings.get("circuit_breaker_cooldown_seconds", 60)),
        "data": data,
    }


@app.post("/api/routing/circuits/reset")
async def reset_routing_circuits(_: None = Depends(require_admin)) -> dict[str, str]:
    UPSTREAM_CIRCUITS.clear()
    NO_ROUTE_CACHE.clear()
    return {"message": "上游熔断状态已重置"}


@app.post("/api/compatibility/test")
async def compatibility_test(
    update: CompatibilityTestRequest, _: None = Depends(require_admin)
) -> dict[str, Any]:
    settings = load_settings()
    upstream = next((item for item in configured_upstreams(settings) if item["name"] == update.upstream), None)
    if upstream is None:
        raise HTTPException(status_code=404, detail="上游不存在")
    test_model = resolved_model(settings, update.model)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if upstream["api_key"]:
        headers["Authorization"] = f"Bearer {upstream['api_key']}"
    tests = [
        ("模型列表", "GET", upstream["models_url"], None),
        ("Chat Completions", "POST", upstream["url"], {"model": test_model, "messages": [{"role": "user", "content": "Reply with OK."}], "stream": False, "max_tokens": 8}),
        ("Responses", "POST", endpoint_url_for_target(upstream["url"], "responses"), {"model": test_model, "input": "Reply with OK.", "stream": False, "max_output_tokens": 8}),
        ("Embeddings", "POST", endpoint_url_for_target(upstream["url"], "embeddings"), {"model": test_model, "input": "compatibility check"}),
    ]
    results = []
    timeout = min(float(upstream["timeout"]), 30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for name, method, url, payload in tests:
            started = time.perf_counter()
            try:
                response = await client.request(method, url, headers=headers, json=payload)
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                results.append({
                    "name": name,
                    "ok": response.status_code < 400,
                    "status_code": response.status_code,
                    "latency_ms": elapsed,
                    "detail": "兼容" if response.status_code < 400 else f"HTTP {response.status_code}",
                })
            except httpx.RequestError as exc:
                results.append({"name": name, "ok": False, "status_code": None, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "detail": str(exc)[:300]})
        stream_tests = [
            ("Chat Completions 流式", upstream["url"], {"model": test_model, "messages": [{"role": "user", "content": "Reply with OK."}], "stream": True, "max_tokens": 8}, {"[DONE]"}),
            ("Responses 流式", endpoint_url_for_target(upstream["url"], "responses"), {"model": test_model, "input": "Reply with OK.", "stream": True, "max_output_tokens": 8}, {"response.completed", "response.incomplete"}),
        ]
        for name, url, payload, terminal_markers in stream_tests:
            started = time.perf_counter()
            status_code = None
            terminal = False
            try:
                async with client.stream("POST", url, headers={**headers, "Accept": "text/event-stream"}, json=payload) as response:
                    status_code = response.status_code
                    if status_code < 400:
                        async for line in response.aiter_lines():
                            if line.startswith("event: ") and line[7:].strip() in terminal_markers:
                                terminal = True
                            if line.startswith("data: "):
                                data = line[6:].strip()
                                if data in terminal_markers:
                                    terminal = True
                                try:
                                    event = json.loads(data)
                                    if str(event.get("type") or "") in terminal_markers:
                                        terminal = True
                                except (ValueError, AttributeError):
                                    pass
                            if terminal:
                                break
                ok = bool(status_code is not None and status_code < 400 and terminal)
                detail = "兼容，收到标准终止事件" if ok else (f"HTTP {status_code}" if status_code and status_code >= 400 else "流已结束，但未收到标准终止事件")
                results.append({"name": name, "ok": ok, "status_code": status_code, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "detail": detail})
            except httpx.RequestError as exc:
                results.append({"name": name, "ok": False, "status_code": status_code, "latency_ms": round((time.perf_counter() - started) * 1000, 1), "detail": str(exc)[:300]})
    return {"upstream": upstream["name"], "model": update.model, "results": results}


@app.get("/api/system/events")
async def system_events(_: None = Depends(require_admin)) -> StreamingResponse:
    async def stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        STATUS_SUBSCRIBERS.add(queue)
        try:
            yield "event: connected\ndata: {\"status\":\"ok\"}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"id: {item['id']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            STATUS_SUBSCRIBERS.discard(queue)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})


def model_metadata(item: dict[str, Any], model_id: str) -> dict[str, Any]:
    context_length = 0
    for key in ("context_length", "max_context_length", "context_window", "max_model_len"):
        try:
            context_length = max(context_length, int(item.get(key) or 0))
        except (TypeError, ValueError):
            pass
    model_info = item.get("model_info") or {}
    if isinstance(model_info, dict):
        for key, value in model_info.items():
            if str(key).endswith("context_length"):
                try:
                    context_length = max(context_length, int(value or 0))
                except (TypeError, ValueError):
                    pass

    capabilities: list[str] = []
    raw_capabilities = item.get("capabilities") or item.get("supported_features") or []
    if isinstance(raw_capabilities, dict):
        raw_capabilities = [key for key, enabled in raw_capabilities.items() if enabled]
    if isinstance(raw_capabilities, list):
        capabilities = [str(value).lower().replace("-", "_") for value in raw_capabilities if str(value).strip()]
    name = model_id.lower()
    inferred = []
    if any(marker in name for marker in ("vision", "-vl", "_vl", "gpt-4o")):
        inferred.append("vision")
    if any(marker in name for marker in ("embedding", "embed")):
        inferred.append("embedding")
    if any(marker in name for marker in ("reasoning", "deepseek-r1", "qwq", "o1", "o3")):
        inferred.append("reasoning")
    capabilities = list(dict.fromkeys([*capabilities, *inferred]))

    raw_interfaces = item.get("supported_endpoints") or item.get("endpoints") or item.get("interfaces") or []
    interfaces = [str(value).strip().lstrip("/") for value in raw_interfaces] if isinstance(raw_interfaces, list) else []
    if not interfaces:
        if "embedding" in capabilities:
            interfaces = ["v1/embeddings"]
        elif "rerank" in name:
            interfaces = ["v1/rerank"]
        elif any(marker in name for marker in ("dall-e", "image")):
            interfaces = ["v1/images/generations"]
        else:
            interfaces = ["v1/chat/completions", "v1/responses"]
    return {"context_length": context_length or None, "capabilities": capabilities, "interfaces": list(dict.fromkeys(interfaces))}


@app.get("/api/models")
async def get_models(refresh: bool = False, _: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    upstreams = configured_upstreams(settings)
    upstream_names = [item["name"] for item in upstreams]
    sources = "|".join(f"{item['name']}:{item['models_url']}" for item in upstreams)
    ttl = max(0, int(settings.get("model_cache_ttl", 60)))
    if not refresh and MODEL_CACHE.get("data") is not None and time.time() - float(MODEL_CACHE.get("at", 0)) < ttl and MODEL_CACHE.get("source") == sources:
        return {"source": "多个上游" if len(upstreams) > 1 else upstreams[0]["models_url"], "count": len(MODEL_CACHE["data"]), "cached": True, "data": MODEL_CACHE["data"], "upstreams": upstream_names}
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
                    details = item if isinstance(item, dict) else {}
                    models.append({"id": str(model_id), "owned_by": str(details.get("owned_by") or details.get("owner") or "upstream"), "created": details.get("created") or details.get("modified_at"), "upstream": upstream["name"], **model_metadata(details, str(model_id))})
    if not models and failures:
        raise HTTPException(status_code=502, detail="获取上游模型失败：" + "；".join(failures))
    upstream_order = {item["name"]: index for index, item in enumerate(upstreams)}
    models.sort(key=lambda item: (upstream_order.get(item["upstream"], len(upstreams)), item["id"].lower()))
    MODEL_CACHE.update({"at": time.time(), "data": models, "source": sources})
    logger.info("Discovered %d models from %d upstreams", len(models), len(upstreams))
    return {"source": "多个上游" if len(upstreams) > 1 else upstreams[0]["models_url"], "count": len(models), "cached": False, "data": models, "failures": failures, "upstreams": upstream_names}


@app.get("/v1/models")
async def public_models(request: Request, _: None = Depends(require_api_token)) -> dict[str, Any]:
    try:
        result = await get_models(False, None)
    except HTTPException:
        if not load_settings().get("virtual_models"):
            raise
        result = {"data": []}
    token = getattr(request.state, "api_token", None)
    models = [item for item in result.get("data", []) if token is None or token_allows_model(token, str(item.get("id") or ""))]
    existing = {str(item.get("id") or "") for item in models}
    for item in load_settings().get("virtual_models", []):
        alias = str(item.get("alias") or "") if isinstance(item, dict) else ""
        if alias and alias not in existing and (token is None or token_allows_model(token, alias)):
            models.append({"id": alias, "created": 0, "owned_by": "cleanllm"})
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


SSE_KEEPALIVE_SECONDS = 8.0
SSE_PREOUTPUT_LIMIT = 256 * 1024


def pop_sse_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    matches = []
    for delimiter in (b"\r\n\r\n", b"\n\n", b"\r\r"):
        index = buffer.find(delimiter)
        if index >= 0:
            matches.append((index, delimiter))
    if not matches:
        return None, buffer
    index, delimiter = min(matches, key=lambda item: item[0])
    end = index + len(delimiter)
    return buffer[:end], buffer[end:]


async def iter_sse_frames(response: Any, keepalive_seconds: float = SSE_KEEPALIVE_SECONDS):
    """Yield complete SSE frames and None while an idle upstream needs a heartbeat."""
    iterator = response.aiter_bytes().__aiter__()
    pending = b""
    read_task: asyncio.Task | None = None
    try:
        while True:
            if read_task is None:
                read_task = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait({read_task}, timeout=keepalive_seconds)
            if not done:
                yield None
                continue
            try:
                chunk = read_task.result()
            except StopAsyncIteration:
                read_task = None
                break
            read_task = None
            pending += chunk
            while True:
                frame, pending = pop_sse_frame(pending)
                if frame is None:
                    break
                yield frame
        if pending.strip():
            yield pending
    finally:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass


def sse_frame_payload(frame: bytes) -> tuple[str, dict[str, Any] | None]:
    data_lines = []
    for line in frame.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return "", None
    raw = b"\n".join(data_lines).decode("utf-8", errors="replace").strip()
    if raw == "[DONE]":
        return raw, None
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw, None
    return raw, payload if isinstance(payload, dict) else None


def rewrite_sse_payload(frame: bytes, payload: dict[str, Any]) -> bytes:
    lines = frame.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    prefix = [line for line in lines if line and not line.startswith(b"data:")]
    data = b"data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"\n".join([*prefix, data]) + b"\n\n"


def responses_event_state(payload: dict[str, Any] | None) -> tuple[bool, bool, bool]:
    """Return semantic failure, meaningful output, and successful terminal state."""
    if not payload:
        return False, False, False
    event_type = str(payload.get("type") or "").lower()
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    status_value = str(payload.get("status") or response.get("status") or "").lower()
    failed = bool(payload.get("error")) or event_type in {
        "error", "response.error", "response.failed"
    } or status_value in {"error", "failed"}
    terminal = event_type in {"response.completed", "response.incomplete"} or status_value in {
        "completed", "incomplete"
    }
    control = {"response.created", "response.in_progress", "response.queued"}
    meaningful = terminal or (event_type.startswith("response.") and event_type not in control and not failed)
    if event_type.endswith(".delta"):
        meaningful = any(bool(payload.get(key)) for key in ("delta", "text", "arguments"))
    return failed, meaningful, terminal


def chat_event_state(raw: str, payload: dict[str, Any] | None) -> tuple[bool, bool, bool]:
    if raw == "[DONE]":
        return False, True, True
    if not payload:
        return False, False, False
    failed = bool(payload.get("error")) or str(payload.get("type") or "").lower() in {
        "error", "response.error", "response.failed"
    }
    meaningful = False
    terminal = False
    for choice in payload.get("choices", []) if isinstance(payload.get("choices"), list) else []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if delta.get("content") or delta.get("tool_calls") or delta.get("function_call"):
            meaningful = True
        if choice.get("finish_reason") is not None:
            meaningful = True
            terminal = True
    return failed, meaningful, terminal


def stream_failure_event(model: str, message: str) -> bytes:
    failed = {
        "type": "response.failed",
        "response": {
            "id": f"resp-{secrets.token_hex(12)}", "object": "response",
            "created_at": int(time.time()), "model": model, "status": "failed",
            "error": {"type": "upstream_error", "message": message},
        },
    }
    return f"event: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}\n\n".encode()


def chat_stream_failure_event(message: str) -> bytes:
    payload = {"error": {"message": message, "type": "upstream_error"}}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


async def native_responses_stream(payload: dict[str, Any], settings: dict[str, Any], request: Request) -> Response:
    """Relay Responses SSE and fail over until the first meaningful output."""
    failures: list[str] = []
    requested_model = str(payload.get("model") or "")
    target_model = resolved_model(settings, requested_model)
    candidates = route_upstreams(settings, requested_model, request)
    usage_id = getattr(request.state, "usage_id", None)
    attempts = 0

    async def connect(upstream: dict[str, Any]) -> tuple[Any, Any, float] | None:
        nonlocal attempts
        if not acquire_upstream(upstream["name"]):
            return None
        attempts += 1
        update_usage_record_safely(
            usage_id, resolved_model=target_model, upstream=upstream["name"], attempts=attempts
        )
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        client = httpx.AsyncClient(timeout=upstream_stream_timeout(upstream))
        responses_url = upstream["url"].replace("/chat/completions", "/responses")
        started = time.perf_counter()
        try:
            logger.info("Responses upstream request: %s -> %s", upstream["name"], responses_url)
            response = await client.send(
                client.build_request(
                    "POST", responses_url,
                    json={**payload, "model": target_model, "stream": True}, headers=headers,
                ),
                stream=True,
            )
        except httpx.RequestError as exc:
            failures.append(f"{upstream['name']}: {exc}")
            record_typed_upstream_failure(settings, upstream["name"], str(exc), "transient")
            await client.aclose()
            return None
        if response.status_code >= 400:
            failures.append(f"{upstream['name']}: HTTP {response.status_code}")
            record_upstream_http_result(
                settings, upstream["name"], response.status_code, getattr(response, "headers", None)
            )
            await response.aclose()
            await client.aclose()
            return None
        return client, response, started

    first_index = -1
    first_connection = None
    for index, upstream in enumerate(candidates):
        first_connection = await connect(upstream)
        if first_connection is not None:
            first_index = index
            break
    if first_connection is None:
        detail = "所有上游 Responses 接口均不可用：" + "；".join(failures)
        update_usage_record_safely(
            usage_id, status_code=502, error=detail, termination_reason="no_upstream_available"
        )
        return JSONResponse(status_code=502, content={"error": {"message": detail, "type": "upstream_error"}})

    async def relay() -> Any:
        connection = first_connection
        output_started = False
        for index in range(first_index, len(candidates)):
            upstream = candidates[index]
            if index != first_index:
                connection = await connect(upstream)
                if connection is None:
                    continue
            client, response, started = connection
            response_settings = {
                **settings,
                "clean_patterns": upstream.get("clean_patterns", settings.get("clean_patterns", [])),
            }
            buffered: list[bytes] = []
            buffered_size = 0
            terminal = False
            successful_terminal = False
            semantic_failure = False
            first_byte_recorded = False
            stream_error = ""
            try:
                async for frame in iter_sse_frames(response):
                    if frame is None:
                        yield b": cleanllm-keepalive\n\n"
                        continue
                    if not first_byte_recorded:
                        update_usage_record_safely(
                            usage_id, first_byte_ms=round((time.perf_counter() - started) * 1000, 1)
                        )
                        first_byte_recorded = True
                    raw, event = sse_frame_payload(frame)
                    if event is not None:
                        event_type = str(event.get("type") or "")
                        response_data = event.get("response") if isinstance(event.get("response"), dict) else {}
                        if event_type == "response.output_text.delta":
                            event["delta"] = clean_content(
                                str(event.get("delta") or ""), response_settings, strip_result=False
                            )
                        elif event_type == "response.output_text.done":
                            event["text"] = clean_content(str(event.get("text") or ""), response_settings)
                        failed, meaningful, successful = responses_event_state(event)
                        if failed:
                            terminal = True
                            semantic_failure = True
                            update_usage_record_safely(usage_id, termination_reason="response.failed")
                        elif successful:
                            terminal = True
                            successful_terminal = True
                            reason = "response.incomplete" if event_type == "response.incomplete" else "response.completed"
                            update_usage_record_safely(
                                usage_id, termination_reason=reason,
                                **usage_values(response_data.get("usage")),
                            )
                        frame = rewrite_sse_payload(frame, event)
                    else:
                        failed, meaningful, successful = False, raw == "[DONE]", False

                    if semantic_failure and not output_started:
                        stream_error = "上游在业务输出前返回失败事件"
                        break
                    if meaningful and not output_started:
                        output_started = True
                        for item in buffered:
                            yield item
                        buffered.clear()
                    if output_started:
                        yield frame
                    else:
                        buffered.append(frame)
                        buffered_size += len(frame)
                        if buffered_size > SSE_PREOUTPUT_LIMIT:
                            stream_error = "业务输出前事件超过缓冲上限"
                            break
            except httpx.RequestError as exc:
                stream_error = f"上游响应流意外中断：{exc}"
            finally:
                await response.aclose()
                await client.aclose()
                update_usage_record_safely(
                    usage_id, latency_ms=round((time.perf_counter() - started) * 1000, 1)
                )

            if successful_terminal:
                record_upstream_success(upstream["name"])
                remember_route(settings, requested_model, upstream["name"])
                logger.info("Responses stream ended normally: %s", upstream["name"])
                return
            failure_message = stream_error or "上游响应流未正常结束"
            failures.append(f"{upstream['name']}: {failure_message}")
            record_typed_upstream_failure(
                settings, upstream["name"], failure_message, "stream"
            )
            if output_started:
                update_usage_record_safely(
                    usage_id, status_code=502, error=failure_message,
                    termination_reason="stream_disconnected" if stream_error else "missing_terminal_event",
                )
                if not terminal:
                    yield stream_failure_event(requested_model, failure_message)
                return
            logger.warning(
                "Responses stream from %s failed before business output; trying next upstream: %s",
                upstream["name"], failure_message,
            )

        detail = "所有上游 Responses 接口均不可用：" + "；".join(failures)
        update_usage_record_safely(
            usage_id, status_code=502, error=detail, termination_reason="no_upstream_available"
        )
        yield stream_failure_event(requested_model, detail)

    return StreamingResponse(
        relay(), status_code=200, media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
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
    requested_model = str(payload.get("model") or "")
    target_model = resolved_model(settings, requested_model)
    outbound_body = json.dumps({**payload, "model": target_model}, ensure_ascii=False).encode() if payload and requested_model else body
    update_usage_record_safely(getattr(request.state, "usage_id", None), resolved_model=target_model)
    failures: list[str] = []
    attempts = 0
    for upstream in route_upstreams(settings, requested_model, request):
        if not acquire_upstream(upstream["name"]):
            continue
        attempts += 1
        update_usage_record_safely(getattr(request.state, "usage_id", None), upstream=upstream["name"], attempts=attempts)
        url = endpoint_url_for_target(upstream["url"], endpoint)
        headers = {
            "Content-Type": request.headers.get("content-type", "application/json"),
            "Accept": request.headers.get("accept", "application/json"),
        }
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        try:
            async with httpx.AsyncClient(timeout=float(upstream["timeout"])) as client:
                response = await client.post(url, content=outbound_body, headers=headers)
            if response.status_code >= 400:
                failures.append(f"{upstream['name']}: HTTP {response.status_code}")
                record_upstream_http_result(settings, upstream["name"], response.status_code)
                continue
            remember_route(settings, str(payload.get("model") or endpoint), upstream["name"])
            record_upstream_success(upstream["name"])
            update_usage_record_safely(getattr(request.state, "usage_id", None), termination_reason="http_completed")
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
            record_upstream_failure(settings, upstream["name"], str(exc))
            failures.append(f"{upstream['name']}: {exc}")
    detail = "所有上游均不可用：" + "；".join(failures)
    update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error=detail, termination_reason="no_upstream_available")
    return JSONResponse(
        status_code=502,
        content={"error": {"message": detail, "type": "upstream_error"}},
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
    settings = load_settings()
    requested_model = str(payload["model"])
    target_model = resolved_model(settings, requested_model)
    chat_payload = {"model": target_model, "messages": messages, "stream": False}
    for key in ("temperature", "top_p", "max_output_tokens"):
        if key in payload:
            chat_payload["max_tokens" if key == "max_output_tokens" else key] = payload[key]
    failures = []
    update_usage_record_safely(getattr(request.state, "usage_id", None), resolved_model=target_model)
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
            attempts = 0
            for upstream in route_upstreams(settings, requested_model, request):
                if not acquire_upstream(upstream["name"]):
                    continue
                attempts += 1
                update_usage_record_safely(getattr(request.state, "usage_id", None), upstream=upstream["name"], attempts=attempts)
                headers = {"Content-Type": "application/json"}
                if upstream["api_key"]: headers["Authorization"] = f"Bearer {upstream['api_key']}"
                try:
                    responses_url = upstream["url"]
                    logger.info("Responses chat fallback request: %s -> %s", upstream["name"], responses_url)
                    async with httpx.AsyncClient(timeout=upstream_stream_timeout(upstream)) as client:
                        async with client.stream("POST", responses_url, json=stream_payload, headers=headers) as upstream_response:
                            if upstream_response.status_code >= 400:
                                logger.warning("Responses upstream %s returned HTTP %s", upstream["name"], upstream_response.status_code)
                                record_upstream_http_result(settings, upstream["name"], upstream_response.status_code)
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
                except httpx.RequestError as exc:
                    record_upstream_failure(settings, upstream["name"], str(exc))
                    continue
            if native_completed:
                record_upstream_success(str(selected_name or ""))
                remember_route(settings, str(payload["model"]), str(selected_name or ""))
                return
            if selected_name is None:
                update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error="所有上游均不可用", termination_reason="no_upstream_available")
                failed = {**base, "status": "failed", "error": {"message": "所有上游均不可用", "type": "upstream_error"}}
                yield f"event: response.failed\ndata: {json.dumps({'type':'response.failed','response':failed}, ensure_ascii=False)}\n\n"
                return
            if not chat_done:
                record_upstream_failure(settings, str(selected_name), "响应流未正常结束")
                update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error="上游响应流未正常结束", termination_reason="missing_terminal_event")
                failed = {**base, "status": "failed", "error": {"message": "上游响应流未正常结束", "type": "upstream_error"}}
                yield f"event: response.failed\ndata: {json.dumps({'type':'response.failed','response':failed}, ensure_ascii=False)}\n\n"
                return
            remember_route(settings, str(payload["model"]), str(selected_name))
            record_upstream_success(str(selected_name))
            update_usage_record_safely(getattr(request.state, "usage_id", None), termination_reason="response.completed")
            completed = {**base, "status": "completed", "output": [{"id": item_id, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": complete_text}]}]}
            sequence_number += 1
            completed["sequence_number"] = sequence_number
            yield f"event: response.output_text.done\ndata: {json.dumps({'type':'response.output_text.done','item_id':item_id,'output_index':0,'content_index':0,'text':complete_text,'response_id':response_id,'sequence_number':sequence_number}, ensure_ascii=False)}\n\n"
            yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':completed}, ensure_ascii=False)}\n\n"
        return StreamingResponse(response_events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache, no-transform", "X-Accel-Buffering":"no", "Connection":"keep-alive"})
    async with httpx.AsyncClient() as client:
        attempts = 0
        for upstream in route_upstreams(settings, requested_model, request):
            if not acquire_upstream(upstream["name"]):
                continue
            attempts += 1
            update_usage_record_safely(getattr(request.state, "usage_id", None), upstream=upstream["name"], attempts=attempts)
            headers = {"Content-Type": "application/json"}
            if upstream["api_key"]: headers["Authorization"] = f"Bearer {upstream['api_key']}"
            try:
                responses_url = upstream["url"].replace("/chat/completions", "/responses")
                logger.info("Responses upstream request: %s -> %s", upstream["name"], responses_url)
                response = await client.post(responses_url, json={**payload, "model": target_model, "stream": False}, headers=headers, timeout=float(upstream["timeout"]))
                if response.status_code in {404, 405}:
                    response = await client.post(upstream["url"], json=chat_payload, headers=headers, timeout=float(upstream["timeout"]))
                if response.status_code >= 400:
                    failures.append(f"{upstream['name']}: HTTP {response.status_code}")
                    record_upstream_http_result(settings, upstream["name"], response.status_code)
                    continue
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
                update_usage_record_safely(getattr(request.state, "usage_id", None), termination_reason="response.completed")
                remember_route(settings, str(payload["model"]), upstream["name"])
                record_upstream_success(upstream["name"])
                if payload.get("stream") is True:
                    async def events():
                        yield f"event: response.created\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                        yield f"event: response.output_text.delta\ndata: {json.dumps({'type':'response.output_text.delta','delta':text,'response_id':result['id']}, ensure_ascii=False)}\n\n"
                        yield f"event: response.completed\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
                    return StreamingResponse(events(), media_type="text/event-stream")
                return JSONResponse(status_code=response.status_code, content=result)
            except (httpx.RequestError, ValueError) as exc:
                logger.warning("Responses upstream %s failed: %s", upstream["name"], exc)
                record_upstream_failure(settings, upstream["name"], str(exc))
                failures.append(f"{upstream['name']}: {exc}")
    detail = "所有上游均不可用：" + "；".join(failures)
    update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error=detail, termination_reason="no_upstream_available")
    return JSONResponse(status_code=502, content={"error": {"message": detail, "type": "upstream_error"}})


@app.post("/v1/chat/completions")
async def proxy_api(request: Request, _: None = Depends(require_api_token)) -> Response:
    proxy_started = time.perf_counter()
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    settings = load_settings()
    requested_model = str(payload.get("model") or "")
    target_model = resolved_model(settings, requested_model)
    upstream_payload = {**payload, "model": target_model} if requested_model else payload
    update_usage_record_safely(getattr(request.state, "usage_id", None), resolved_model=target_model)
    streaming = payload.get("stream") is True
    failures = []
    response = None
    selected = None
    client = None
    attempts = 0
    candidates = route_upstreams(settings, requested_model, request)
    selected_index = -1
    for index, upstream in enumerate(candidates):
        if not acquire_upstream(upstream["name"]):
            continue
        attempts += 1
        update_usage_record_safely(getattr(request.state, "usage_id", None), upstream=upstream["name"], attempts=attempts)
        headers = {"Content-Type": "application/json"}
        if upstream["api_key"]:
            headers["Authorization"] = f"Bearer {upstream['api_key']}"
        try:
            if streaming:
                candidate_client = httpx.AsyncClient(timeout=upstream_stream_timeout(upstream))
                candidate = await candidate_client.send(candidate_client.build_request("POST", upstream["url"], json=upstream_payload, headers=headers), stream=True)
                if candidate.status_code >= 400:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    record_upstream_http_result(
                        settings, upstream["name"], candidate.status_code,
                        getattr(candidate, "headers", None),
                    )
                    await candidate.aclose()
                    await candidate_client.aclose()
                    continue
                client, response, selected = candidate_client, candidate, upstream
                selected_index = index
            else:
                async with httpx.AsyncClient() as candidate_client:
                    candidate = await candidate_client.post(upstream["url"], json=upstream_payload, headers=headers, timeout=float(upstream["timeout"]))
                if candidate.status_code >= 400:
                    failures.append(f"{upstream['name']}: HTTP {candidate.status_code}")
                    record_upstream_http_result(settings, upstream["name"], candidate.status_code)
                    continue
                response, selected = candidate, upstream
                selected_index = index
            break
        except httpx.RequestError as exc:
            record_upstream_failure(settings, upstream["name"], str(exc))
            failures.append(f"{upstream['name']}: {exc}")
    if response is None:
        detail = "所有上游均不可用：" + "；".join(failures)
        update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error=detail, termination_reason="no_upstream_available")
        return JSONResponse(status_code=502, content={"error": {"message": detail, "type": "upstream_error"}})
    logger.info("Model %s routed to %s", payload.get("model", ""), selected["name"])
    response_settings = {**settings, "clean_patterns": selected.get("clean_patterns", settings.get("clean_patterns", []))}
    if streaming:
        stream_started = proxy_started
        async def stream_response():
            nonlocal attempts
            current_client, current_response, current_upstream = client, response, selected
            current_index = selected_index
            emitted_output = False
            failures_before_output = []
            while current_upstream is not None:
                completed = False
                meaningful = False
                buffered = []
                buffered_size = 0
                stream_error = ""
                try:
                    async for frame in iter_sse_frames(current_response):
                        if frame is None:
                            yield b": cleanllm-keepalive\n\n"
                            continue
                        if not frame.strip():
                            continue
                        raw, chunk = sse_frame_payload(frame)
                        if chunk is not None:
                            update_usage_record_safely(getattr(request.state, "usage_id", None), **usage_values(chunk.get("usage")))
                        failed, has_output, terminal = chat_event_state(raw, chunk)
                        if failed:
                            stream_error = "上游返回错误事件"
                            break
                        cleaned = frame
                        if chunk is not None and raw != "[DONE]":
                            text_frame = frame.decode("utf-8", errors="replace")
                            cleaned = b"\n".join(
                                (clean_stream_line(line, response_settings)).encode("utf-8")
                                for line in text_frame.rstrip("\n").split("\n")
                            ) + b"\n\n"
                        if has_output:
                            meaningful = True
                            emitted_output = True
                            for pending_frame in buffered:
                                yield pending_frame
                            buffered.clear()
                        if emitted_output:
                            yield cleaned
                        else:
                            buffered.append(cleaned)
                            buffered_size += len(cleaned)
                            if buffered_size > SSE_PREOUTPUT_LIMIT:
                                stream_error = "业务输出前事件超过缓冲上限"
                                break
                        if terminal:
                            completed = True
                    if not stream_error and not completed:
                        stream_error = "上游响应流未正常结束"
                except httpx.RequestError as exc:
                    stream_error = f"上游响应流意外中断：{exc}"
                finally:
                    await current_response.aclose()
                    await current_client.aclose()
                if completed:
                    record_upstream_success(current_upstream["name"])
                    remember_route(settings, requested_model, current_upstream["name"])
                    update_usage_record_safely(getattr(request.state, "usage_id", None), termination_reason="stream_completed", latency_ms=round((time.perf_counter() - stream_started) * 1000, 1))
                    return
                record_typed_upstream_failure(settings, current_upstream["name"], stream_error, "stream")
                failures_before_output.append(f"{current_upstream['name']}: {stream_error}")
                if emitted_output:
                    update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error=stream_error, termination_reason="stream_disconnected")
                    yield chat_stream_failure_event(stream_error)
                    return
                next_connection = None
                for next_index in range(current_index + 1, len(candidates)):
                    candidate_upstream = candidates[next_index]
                    if not acquire_upstream(candidate_upstream["name"]):
                        continue
                    attempts += 1
                    headers = {"Content-Type": "application/json"}
                    if candidate_upstream["api_key"]:
                        headers["Authorization"] = f"Bearer {candidate_upstream['api_key']}"
                    candidate_client = httpx.AsyncClient(timeout=upstream_stream_timeout(candidate_upstream))
                    try:
                        candidate_response = await candidate_client.send(candidate_client.build_request("POST", candidate_upstream["url"], json=upstream_payload, headers=headers), stream=True)
                    except httpx.RequestError as exc:
                        await candidate_client.aclose()
                        record_typed_upstream_failure(settings, candidate_upstream["name"], str(exc), "transient")
                        failures_before_output.append(f"{candidate_upstream['name']}: {exc}")
                        continue
                    if candidate_response.status_code >= 400:
                        record_upstream_http_result(settings, candidate_upstream["name"], candidate_response.status_code, getattr(candidate_response, "headers", None))
                        failures_before_output.append(f"{candidate_upstream['name']}: HTTP {candidate_response.status_code}")
                        await candidate_response.aclose(); await candidate_client.aclose()
                        continue
                    current_client, current_response, current_upstream, current_index = candidate_client, candidate_response, candidate_upstream, next_index
                    update_usage_record_safely(getattr(request.state, "usage_id", None), upstream=current_upstream["name"], attempts=attempts)
                    next_connection = True
                    break
                if next_connection:
                    continue
                detail = "所有上游均不可用：" + "；".join(failures_before_output)
                update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error=detail, termination_reason="no_upstream_available")
                yield chat_stream_failure_event(detail)
                return
        return StreamingResponse(stream_response(), status_code=response.status_code, media_type=response.headers.get("content-type", "text/event-stream"))
    try:
        data = response.json()
    except ValueError:
        record_upstream_failure(settings, selected["name"], "上游返回非 JSON 内容")
        update_usage_record_safely(getattr(request.state, "usage_id", None), status_code=502, error="上游返回了非 JSON 内容", termination_reason="invalid_upstream_response")
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
    record_upstream_success(selected["name"])
    remember_route(settings, requested_model, selected["name"])
    update_usage_record_safely(getattr(request.state, "usage_id", None), termination_reason="http_completed")
    return JSONResponse(status_code=response.status_code, content=data)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=11515)
