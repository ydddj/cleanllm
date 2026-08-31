import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

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
    "clean_patterns": DEFAULT_PATTERNS,
}

app = FastAPI(title="CleanLLM", version="3.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    password: str


class SettingsUpdate(BaseModel):
    target_api_url: HttpUrl
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    clean_patterns: list[str] = Field(default_factory=list, max_length=30)

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


def admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def session_secret() -> bytes:
    value = os.getenv("SESSION_SECRET") or admin_password()
    return value.encode("utf-8")


def create_session_token() -> str:
    timestamp = str(int(time.time()))
    signature = hmac.new(session_secret(), timestamp.encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def valid_session(token: str | None) -> bool:
    if not token or not admin_password():
        return False
    try:
        timestamp_text, signature = token.split(".", 1)
        timestamp = int(timestamp_text)
    except (ValueError, TypeError):
        return False
    if timestamp > time.time() + 60 or time.time() - timestamp > SESSION_MAX_AGE:
        return False
    expected = hmac.new(
        session_secret(), timestamp_text.encode(), hashlib.sha256
    ).hexdigest()
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


@app.exception_handler(Exception)
async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看容器日志"})


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
    password = admin_password()
    if not password:
        raise HTTPException(status_code=503, detail="尚未配置 ADMIN_PASSWORD")
    if not secrets.compare_digest(login_data.password.encode(), password.encode()):
        raise HTTPException(status_code=401, detail="密码错误")
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
    return load_settings()


@app.put("/api/settings")
async def update_settings(
    update: SettingsUpdate, _: None = Depends(require_admin)
) -> dict[str, str]:
    save_settings(update.model_dump(mode="json"))
    return {"message": "设置已保存"}


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
