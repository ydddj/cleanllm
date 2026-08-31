import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
STATIC_DIR = BASE_DIR / "static"
DEFAULT_SETTINGS = {
    "target_api_url": os.getenv("TARGET_API_URL", "http://host.docker.internal:11434/v1/chat/completions"),
    "api_key": os.getenv("UPSTREAM_API_KEY", ""),
    "timeout_seconds": int(os.getenv("REQUEST_TIMEOUT", "120")),
    "strip_channel_tags": True,
    "strip_think_tags": True,
    "strip_html_tags": True,
}

app = FastAPI(title="CleanLLM", version="2.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBasic(auto_error=False)

class SettingsUpdate(BaseModel):
    target_api_url: HttpUrl
    api_key: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    strip_channel_tags: bool = True
    strip_think_tags: bool = True
    strip_html_tags: bool = True

def require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    password = os.getenv("ADMIN_PASSWORD", "")
    if not password:
        return
    valid = credentials is not None and secrets.compare_digest(credentials.password.encode(), password.encode())
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要管理员密码", headers={"WWW-Authenticate": 'Basic realm="CleanLLM"'})

def load_settings() -> dict[str, Any]:
    settings = DEFAULT_SETTINGS.copy()
    try:
        if SETTINGS_FILE.exists():
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return settings

def save_settings(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_FILE)

def clean_content(text: str, settings: dict[str, Any]) -> str:
    if settings["strip_channel_tags"]:
        text = re.sub(r'[<＜]\s*[\|｜]*\s*channel\s*[>＞].*?[<＜]\s*[\|｜/]*\s*channel\s*[\|｜]*\s*[>＞]', '', text, flags=re.DOTALL | re.IGNORECASE)
    if settings["strip_think_tags"]:
        text = re.sub(r'[<＜]\s*think\s*[>＞].*?[<＜]\s*[\|｜/]*\s*think\s*[>＞]', '', text, flags=re.DOTALL | re.IGNORECASE)
    if settings["strip_html_tags"]:
        text = re.sub(r'[<＜]\s*[\|｜/]*\s*[a-zA-Z0-9_]+\s*[\|｜/]*\s*[>＞]', '', text, flags=re.IGNORECASE)
    return text.strip()

@app.get("/", include_in_schema=False)
async def index(_: None = Depends(require_admin)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/settings")
async def get_settings(_: None = Depends(require_admin)) -> dict[str, Any]:
    return load_settings()

@app.put("/api/settings")
async def update_settings(update: SettingsUpdate, _: None = Depends(require_admin)) -> dict[str, str]:
    save_settings(update.model_dump(mode="json"))
    return {"message": "设置已保存"}

@app.post("/v1/chat/completions")
async def proxy_api(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求体不是有效 JSON") from exc
    settings = load_settings()
    payload["stream"] = False
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(settings["target_api_url"], json=payload, headers=headers, timeout=float(settings["timeout_seconds"]))
    except httpx.RequestError as exc:
        return JSONResponse(status_code=502, content={"error": {"message": f"无法连接上游服务：{exc}", "type": "upstream_error"}})
    try:
        data = response.json()
    except ValueError:
        return JSONResponse(status_code=502, content={"error": {"message": "上游返回了非 JSON 内容", "type": "upstream_error"}})
    choices = data.get("choices", []) if isinstance(data, dict) else []
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = clean_content(content, settings)
    return JSONResponse(status_code=response.status_code, content=data)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=11515)
