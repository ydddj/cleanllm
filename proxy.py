import hashlib
import hmac
import base64
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, field_validator

logger = logging.getLogger("cleanllm")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "cleanllm.log"
MAX_LOG_BYTES = 5 * 1024 * 1024
STATIC_DIR = BASE_DIR / "static"
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
    "clean_patterns": DEFAULT_PATTERNS,
}

app = FastAPI(title="CleanLLM", version="3.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    username: str = "admin"
    password: str


class SettingsUpdate(BaseModel):
    target_api_url: HttpUrl
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    models_api_url: str = ""
    clean_patterns: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("models_api_url")
    @classmethod
    def validate_models_url(cls, value: str) -> str:
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("模型列表地址必须是有效的 HTTP/HTTPS URL")
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
        logger.setLevel(logging.INFO)
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
    return response


@app.get("/", include_in_schema=False)
async def index(request: Request):
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


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
    return {key: settings[key] for key in DEFAULT_SETTINGS}


@app.put("/api/settings")
async def update_settings(
    update: SettingsUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    settings = load_settings()
    settings.update(update.model_dump(mode="json"))
    save_settings(settings)
    return {"message": "设置已保存"}


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


@app.get("/api/models")
async def get_models(_: None = Depends(require_admin)) -> dict[str, Any]:
    settings = load_settings()
    url = models_url(settings)
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
    logger.info("Discovered %d upstream models from %s", len(models), url)
    return {"source": url, "count": len(models), "data": models}


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
        "max_bytes": MAX_LOG_BYTES,
    }


@app.post("/v1/chat/completions")
async def proxy_api(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请求体不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    settings = load_settings()
    payload["stream"] = False
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings["target_api_url"],
                json=payload,
                headers=headers,
                timeout=float(settings["timeout_seconds"]),
            )
    except httpx.RequestError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "message": f"无法连接上游服务：{exc}",
                    "type": "upstream_error",
                }
            },
        )
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
