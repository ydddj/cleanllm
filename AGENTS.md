# CleanLLM development guide

## Project scope

CleanLLM is a small FastAPI gateway with a browser-based admin UI. It proxies OpenAI-compatible chat-completion requests, cleans response text with user-defined regular expressions, discovers upstream models, and manages models when the upstream is Ollama.

Keep the application lightweight and suitable for a single Docker container. Avoid adding a database, frontend build toolchain, or large framework unless a requested feature clearly requires one.

## Important files

- `proxy.py`: FastAPI routes, authentication, settings, proxy logic, logging, and Ollama integration.
- `static/index.html`, `static/app.js`, `static/style.css`: dependency-free admin UI.
- `static/login.html`, `static/login.js`: login page and Web session creation.
- `tests/test_proxy.py`: backend and integration tests.
- `docker-compose.yml`, `Dockerfile`, `.env.example`: container configuration.
- `.github/workflows/docker-publish.yml`: tests and multi-architecture Docker Hub publishing.

## Compatibility and behavior

- Preserve the public OpenAI-compatible endpoint at `POST /v1/chat/completions`.
- Keep Web administration behind the signed HttpOnly session cookie. Management APIs must use `Depends(require_admin)`.
- Never log API keys, passwords, session tokens, complete request bodies, or model responses.
- Store passwords only as salted scrypt hashes. Never write plaintext passwords to `settings.json`.
- Treat `settings.json` as persistent user data. New settings must have safe defaults and remain compatible with older files.
- Model discovery must continue working for non-Ollama OpenAI-compatible upstreams.
- Ollama-only operations must fail gracefully when Ollama is unavailable; they must not break the normal model list or proxy.
- Long-running Ollama pulls must remain streamed. Do not replace them with a short fixed timeout.
- Format Unix timestamps in the browser as local date/time and support both seconds and milliseconds.
- Keep the log file capped at 5 MB and preserve `/data` volume compatibility.
- Ollama model definition exports should remain compatible with Open WebUI metadata; full weight exports use `.ollama.tar.gz` archives from the mounted models directory.
- Keep the admin layout fluid on wide screens and horizontally scrollable for dense tables on narrow screens.
- Keep navigation state in the URL hash so refresh and direct links restore the same page; release notes are a separate page from runtime logs.
- The UI provides approximately ten persisted Morandi color palettes; new palette changes must remain CSS-variable based and work in light/dark mode.
- The UI language is Simplified Chinese and follows the visual style established by `notify-router`.

## Configuration rules

- `TARGET_API_URL` is the complete chat-completions endpoint.
- `MODELS_API_URL` is optional and otherwise derived from `TARGET_API_URL`.
- `OLLAMA_API_URL` is optional and otherwise derived as the scheme and authority of `TARGET_API_URL`.
- Environment variables are initial defaults; settings saved from the Web UI take precedence through the persistent data volume.
- `extra_hosts: host.docker.internal:host-gateway` is needed only when a Linux container connects to a service on its host. Do not make it mandatory for LAN, public, or same-Compose upstreams.

When adding a setting, update all applicable locations: defaults and validation in `proxy.py`, Web load/save logic, `.env.example`, `docker-compose.yml`, README documentation, and tests.

## Editing guidelines

- Keep API failures as valid JSON with a useful Chinese `detail` message.
- Validate user input on the backend even if the UI also validates it.
- Escape upstream-provided values before inserting them into HTML.
- Require explicit confirmation for destructive operations such as deleting an Ollama model.
- Preserve unrelated user changes and do not replace persistent configuration files.
- Prefer focused changes and tests over broad rewrites.

## Required verification

Run these checks before committing:

```powershell
python -m py_compile proxy.py tests/test_proxy.py
node --check static/app.js
node --check static/login.js
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

If Docker is installed, also run:

```powershell
docker compose config --quiet
docker build -t cleanllm:test .
```

Add regression tests for changed authentication, settings persistence, response cleaning, model normalization, Ollama operations, error JSON, or log-size behavior.

## Release workflow

- The primary branch is `main`.
- Pushes to `main` run tests and publish `ydddj/cleanllm:latest` plus the current semantic version tag for `linux/amd64` and `linux/arm64`; do not add commit-based `sha-*` tags.
- Tags matching `v*.*.*` also publish semantic-version tags.
- Every user-visible release change must update the API/UI version, `CHANGELOG.md`, and the README's current version reference together.
- Never place Docker Hub credentials in source files. Publishing uses the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
- After publishing, verify the GitHub Actions job before reporting the Docker image as available.
