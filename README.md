# CleanLLM

带 Web 管理中心的 OpenAI 兼容代理，可连接 Ollama 或其他兼容服务，支持自定义正则清洗、上游模型发现和限量运行日志。

## Docker Compose 启动

新建 `compose.yml`：

```yaml
services:
  cleanllm:
    image: ydddj/cleanllm:latest
    container_name: cleanllm
    ports:
      - "11515:11515"
    environment:
      ADMIN_USERNAME: "admin"
      ADMIN_PASSWORD: "请改成强密码"
      SESSION_SECRET: "请改成另一段足够长的随机字符串"
      COOKIE_SECURE: "false"
      TARGET_API_URL: "http://host.docker.internal:11434/v1/chat/completions"
      MODELS_API_URL: ""
      OLLAMA_API_URL: ""
      OLLAMA_MODELS_DIR: "/ollama-models"
      PUID: "10001"
      PGID: "10001"
      UPSTREAM_API_KEY: ""
      REQUEST_TIMEOUT: "120"
    volumes:
      - cleanllm-data:/data
      # 用于“导出压缩包”；请改成宿主机实际 Ollama models 目录
      - /root/.ollama/models:/ollama-models:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

volumes:
  cleanllm-data:
```

启动服务：

```bash
docker compose up -d
```

更新到最新镜像：

```bash
docker compose pull
docker compose up -d
```

查看日志或停止服务：

```bash
docker compose logs -f
docker compose down
```

打开 `http://localhost:11515`，使用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。登录后可在“账户安全”中修改用户名和密码；密码以带随机盐的 scrypt 单向哈希保存在数据卷中。登录状态保存在 HttpOnly 会话 Cookie 中，有效期为 12 小时。

客户端请求地址为 `http://你的主机:11515/v1/chat/completions`。设置保存在 Docker 数据卷中，升级容器不会丢失。

常用环境变量：`ADMIN_USERNAME`（初始用户名）、`ADMIN_PASSWORD`（初始密码）、`SESSION_SECRET`（会话签名密钥）、`COOKIE_SECURE`（使用 HTTPS 时设为 `true`）、`HOST_PORT`（映射端口）、`TARGET_API_URL`、`UPSTREAM_API_KEY`、`REQUEST_TIMEOUT` 和 `DOCKER_IMAGE`。环境变量作为首次默认值，网页保存账户后以数据卷中的用户名和密码哈希为准。

`extra_hosts` 仅用于让 Linux 容器通过 `host.docker.internal` 访问宿主机。如果上游使用局域网 IP、公网地址或同一 Compose 中的服务名，可以删除这段配置；默认上游在宿主机时建议保留。

响应清洗规则在网页中按“每行一条正则表达式”填写，匹配内容会被删除。默认规则兼容原有的 Channel、Think 和孤立标签清理；保存时会自动检查正则语法。

管理中心可通过上游的 OpenAI 兼容 `/v1/models` 接口展示全部可用模型。默认地址由 Chat Completions 地址自动推导；非标准上游可通过网页或 `MODELS_API_URL` 单独指定。

运行日志保存在数据卷的 `/data/cleanllm.log`，仅记录请求状态和系统事件，不记录 API Key 或请求正文。日志文件严格限制为 5 MB，达到上限后自动裁剪最旧内容。

如果需要从源码构建，请复制 `.env.example` 为 `.env`，然后执行：

```bash
docker compose up -d --build
```

## Ollama 模型管理

“导出压缩包”会把 Ollama manifest 与模型 blobs 打包为 `.ollama.tar.gz`。由于 Ollama HTTP API 不提供完整权重导出接口，Compose 需要将宿主机模型目录只读挂载到 `/ollama-models`；通过 `OLLAMA_MODELS_PATH` 指定宿主机路径（Linux 默认 `/root/.ollama/models`）。如果宿主机目录权限受限，请将 `.env` 中的 `PUID`、`PGID` 设置为该目录所有者的 UID/GID（例如 `1000`），然后重建容器。如果未挂载，仍可使用“导出定义”导出 JSON，但不能生成权重压缩包。

进入 Web 管理中心的“模型列表”，可查看 Ollama 状态、拉取模型并查看实时进度，也可删除已安装模型。`OLLAMA_API_URL` 留空时，CleanLLM 会从 `TARGET_API_URL` 自动提取地址；例如 `http://host.docker.internal:11434/v1/chat/completions` 会使用 `http://host.docker.internal:11434`。如果上游不是 Ollama，OpenAI 兼容模型列表仍可正常使用，管理区会提示 Ollama 不可用。

## 自动发布到 Docker Hub

项目包含 GitHub Actions 自动发布流程。请在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名
- `DOCKERHUB_TOKEN`：Docker Hub Access Token（不要使用账户密码）

推送到 `main` 后会发布 `用户名/cleanllm:latest` 和当前版本号标签（当前为 `1.0.69`），不再发布 `sha-*` 标签；推送 `v1.0.0` 形式的 Git 标签还会发布对应版本号。镜像同时支持 `linux/amd64` 和 `linux/arm64`。

模型列表默认缓存 60 秒，可在页面调整或设为 0 关闭缓存；“刷新模型”会强制从上游读取。上游连通性默认每 10 分钟检测一次，可在代理设置页调整，并保存 24 小时与 7 天采样结果。账户安全页支持创建、复制和撤销 API 访问令牌，令牌以实例密钥加密保存，创建令牌后代理接口要求 `Authorization: Bearer <token>`。系统状态通过 SSE `/api/system/events` 实时推送，模型压缩包导出记录会保存在导出历史中。

“上游与清洗”支持通过选项卡添加多个 OpenAI 兼容上游。常用连接项直接显示，模型列表地址、Ollama 地址、响应清洗规则、模型路由和备用上游 JSON 位于高级设置中；空配置保持留空，不显示无意义的 `[]`。模型列表会聚合所有可用上游，并标明每个模型的来源上游。
Web 重启依赖 Compose 的 `restart: unless-stopped`，不需要挂载 Docker Socket。查看 Docker 日志时可使用 `docker compose logs -t` 显示时间戳。
