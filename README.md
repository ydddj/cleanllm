# CleanLLM

带网页设置界面的 OpenAI 兼容代理，可连接 Ollama 或其他兼容服务，并清理模型响应中的思考和系统标签。

## 启动

1. 复制 `.env.example` 为 `.env`，至少修改 `ADMIN_PASSWORD`。
2. 执行 `docker compose up -d --build`。
3. 打开 `http://localhost:11515`，输入管理密码后设置上游服务。

客户端请求地址为 `http://你的主机:11515/v1/chat/completions`。设置保存在 Docker 数据卷中，升级容器不会丢失。

常用环境变量：`ADMIN_PASSWORD`（管理密码）、`HOST_PORT`（映射端口）、`TARGET_API_URL`、`UPSTREAM_API_KEY`、`REQUEST_TIMEOUT` 和 `DOCKER_IMAGE`。环境变量作为首次默认值，网页保存后以数据卷设置为准。

## 自动发布到 Docker Hub

项目包含 GitHub Actions 自动发布流程。请在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名
- `DOCKERHUB_TOKEN`：Docker Hub Access Token（不要使用账户密码）

推送到 `main` 后会发布 `用户名/cleanllm:latest`，推送 `v1.0.0` 形式的 Git 标签还会发布对应版本号。镜像同时支持 `linux/amd64` 和 `linux/arm64`。
