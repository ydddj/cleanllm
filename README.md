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
      LOG_MAX_BYTES: "5242880"
      CIRCUIT_BREAKER_FAILURES: "3"
      CIRCUIT_BREAKER_COOLDOWN_SECONDS: "60"
      ALERT_WEBHOOK_URL: ""
      ALERT_UPSTREAM_FAILURES: "3"
      ALERT_ERROR_RATE_PERCENT: "30"
      ALERT_BUDGET_PERCENT: "80"
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

打开 `http://localhost:11515`，使用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。登录后可在“系统设置”中修改用户名和密码；密码以带随机盐的 scrypt 单向哈希保存在数据卷中。登录状态保存在 HttpOnly 会话 Cookie 中，有效期为 12 小时。

客户端主要请求地址为 `http://你的主机:11515/v1/chat/completions` 和 `/v1/responses`。此外支持 `/v1/models`、`/v1/embeddings`、`/v1/completions`、`/v1/images/generations`、`/v1/audio/transcriptions`、`/v1/audio/translations`、`/v1/audio/speech`、`/v1/moderations` 和 `/v1/rerank`，这些地址会从所选上游的 `/v1` 基础地址自动推导，无需逐项配置。设置保存在 Docker 数据卷中，升级容器不会丢失。

常用环境变量：`ADMIN_USERNAME`（初始用户名）、`ADMIN_PASSWORD`（初始密码）、`SESSION_SECRET`（会话签名密钥）、`COOKIE_SECURE`（使用 HTTPS 时设为 `true`）、`HOST_PORT`（映射端口）、`TARGET_API_URL`、`UPSTREAM_API_KEY`、`REQUEST_TIMEOUT`、`LOG_MAX_BYTES`、`CIRCUIT_BREAKER_FAILURES`、`CIRCUIT_BREAKER_COOLDOWN_SECONDS`、`ALERT_WEBHOOK_URL`、`ALERT_UPSTREAM_FAILURES`、`ALERT_ERROR_RATE_PERCENT`、`ALERT_BUDGET_PERCENT` 和 `DOCKER_IMAGE`。环境变量作为首次默认值，网页保存账户后以数据卷中的用户名和密码哈希为准。

`extra_hosts` 仅用于让 Linux 容器通过 `host.docker.internal` 访问宿主机。如果上游使用局域网 IP、公网地址或同一 Compose 中的服务名，可以删除这段配置；默认上游在宿主机时建议保留。

响应清洗规则在网页中按“每行一条正则表达式”填写，匹配内容会被删除。默认规则兼容原有的 Channel、Think 和孤立标签清理；保存时会自动检查正则语法。

管理中心可通过上游的 OpenAI 兼容 `/v1/models` 接口展示全部可用模型。默认地址由 Chat Completions 地址自动推导；非标准上游可通过网页或 `MODELS_API_URL` 单独指定。

运行日志保存在数据卷的 `/data/cleanllm.log`，仅记录请求状态和系统事件，不记录 API Key 或请求正文。默认上限为 5 MB，可在系统日志页修改；达到上限后自动裁剪最旧内容。

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

根目录 `VERSION` 是唯一发布版本来源。推送到 `main` 或手动运行 workflow 后，会自动读取该文件并发布 `用户名/cleanllm:latest` 和当前版本号标签（当前为 `1.3.0`），不再发布 `sha-*` 标签；推送 `v1.0.0` 形式的 Git 标签还会发布对应版本号。镜像同时支持 `linux/amd64` 和 `linux/arm64`。

模型列表默认缓存 60 秒，可在页面调整或设为 0 关闭缓存；“刷新模型”会强制从上游读取。上游连通性默认每 10 分钟检测一次，可在代理设置页调整，并保存 24 小时与 7 天采样结果。API令牌页支持创建、显示、复制、停用、启用和删除令牌，并可随时修改到期时间及可用模型白名单。模型规则支持精确名称和 `*`、`?` 通配符，留空允许全部模型；受限令牌访问 `/v1/models` 时也只会看到允许的模型。令牌以实例密钥加密保存，启用且未过期的令牌才能通过 `Authorization: Bearer <token>` 调用代理接口。使用日志明细保留 400 天，清除明细不会改变调用次数、周期 Token 统计或令牌累计用量。系统状态通过 SSE `/api/system/events` 实时推送，模型压缩包导出记录会保存在导出历史中。

配置与运行数据均位于 `/data`：`settings.json` 只保存上游、清洗、外观等配置，`cleanllm.db` 使用 Python 内置 SQLite 保存 API令牌、调用用量、连通性采样和导出历史，上传的背景图保存在 `/data/backgrounds`。升级到 `1.0.98` 后不迁移旧运行历史，首次读取配置时会自动删除 `settings.json` 中旧的运行数组，并把已有 Base64 背景拆成图片文件，从而显著缩小配置文件。

“上游与清洗”支持通过选项卡添加多个 OpenAI 兼容上游。常用连接项直接显示，模型列表地址、Ollama 地址、响应清洗规则、模型路由和备用上游 JSON 位于高级设置中；空配置保持留空，不显示无意义的 `[]`。模型列表会聚合所有可用上游，并标明每个模型的来源上游。连通性区域同时展示 24 小时、7 天可用率，以及按实例本地自然日计算的检测和失败次数。

上游选项卡支持复制、拖动排序、单项启停和勾选后批量启停。上游导出文件默认清空 API Key，导入支持追加并自动处理重名；至少需要保留一个启用上游。模型列表同时展示上游返回的上下文长度、能力和支持接口；元数据缺失时仅按模型名称和类型做保守推断。

API令牌可填写备注和来源 IP 白名单，白名单支持单个 IPv4/IPv6 或 CIDR，每行一项；留空允许全部来源。白名单按 CleanLLM 看到的直连客户端地址校验，反向代理部署时应确保代理网络地址与规则一致。令牌列表会显示最后使用时间。诊断中心的操作审计保留最近 10000 条或 400 天，记录管理员、来源 IP、操作类别、资源与 HTTP 结果，不记录敏感值。

虚拟模型可将客户端使用的稳定别名映射到真实模型，并指定上游优先顺序。上游连续失败达到阈值后会自动熔断，冷却结束后重新参与路由；默认阈值为 3 次、冷却 60 秒，可通过诊断中心或 `CIRCUIT_BREAKER_FAILURES`、`CIRCUIT_BREAKER_COOLDOWN_SECONDS` 调整。API令牌支持 RPM、TPM、自然日及自然月 Token 限额，`0` 表示不限制。诊断中心可查看请求 ID、模型映射、实际上游、尝试次数、状态和耗时，并可手动测试模型列表、Chat Completions 与 Responses 兼容性；追踪不会保存提示词或模型响应。

“分析与成本”按自然日汇总请求量、Token、错误率、延迟分位数和估算费用，可按上游、模型或 API令牌分组并导出 CSV。模型价格以 USD / 1M Token 填写，支持模型通配规则、指定上游以及输入、输出和缓存 Token 分别计价。费用在请求发生时写入 SQLite，之后修改价格不会重算历史。API令牌还可设置月度费用预算，并选择达到预算时仅告警或自动停用。

诊断中心可配置通用 JSON Webhook，用于上游连续失败、最近 5 分钟错误率和预算告警。同类告警有 15 分钟冷却，内容不包含 API Key、令牌、请求正文或响应正文。系统设置会在每次保存配置前自动创建快照，也可手动创建和恢复，最多保留 30 个；“下载完整备份”会生成包含 `settings.json`、`cleanllm.db`、背景图库和版本清单的 ZIP。

监控系统可采集公开的 Prometheus 端点 `/metrics`，其中只包含聚合指标和上游名称，不暴露令牌名或敏感配置。容器编排可分别使用 `/health/live` 检查进程存活、使用 `/health/ready` 检查配置与 SQLite 是否就绪。
Web 重启依赖 Compose 的 `restart: unless-stopped`，不需要挂载 Docker Socket。查看 Docker 日志时可使用 `docker compose logs -t` 显示时间戳。
