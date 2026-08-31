# CleanLLM

带网页登录设置界面的 OpenAI 兼容代理，可连接 Ollama 或其他兼容服务，并使用自定义正则表达式清理模型响应。

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
    volumes:
      - cleanllm-data:/data
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

如果需要从源码构建，请复制 `.env.example` 为 `.env`，然后执行：

```bash
docker compose up -d --build
```

## 自动发布到 Docker Hub

项目包含 GitHub Actions 自动发布流程。请在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名
- `DOCKERHUB_TOKEN`：Docker Hub Access Token（不要使用账户密码）

推送到 `main` 后会发布 `用户名/cleanllm:latest`，推送 `v1.0.0` 形式的 Git 标签还会发布对应版本号。镜像同时支持 `linux/amd64` 和 `linux/arm64`。
