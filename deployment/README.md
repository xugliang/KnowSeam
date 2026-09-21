# 独立部署

Compose 为新环境模板，不接管已有服务。不安装 PostgreSQL/模型、不修改宿主网络、不挂载 Docker socket。先准备专用数据库、模型、私有配置，勿在存量服务器直接整体重建。

- `secrets/core.json`：从核心示例配置；DB/模型 URL 必须容器可达，容器回环不是宿主。`file_root=/app/data`，非回环模型需 `allow_online=true`。
- `secrets/mcp.json`：网关示例，`upstream_url=http://api:18790`，`host=0.0.0.0`，端口 18793，真实 Host/Origin、专用后端凭据与个人身份。
- UID 10001 需读取私有配置。只给本项目 `data` 写权限，`imports` 只读；不递归改服务器现有目录权限。
- 管理员显式执行 `docker compose -f deployment/compose.yaml run --rm api --config /private/core.json init-db` 初始化专用库，再启动服务。
- 端口仅发布回环。跨电脑由已有 HTTPS 代理/隧道提供；不自动改防火墙。
- 数据库/原文需独立备份；容器回滚不是数据库回滚。

已有知识服务可只新增 MCP 网关，接原 REST，不改 A、不移动旧 DB/模型。正式迁移整个核心前先验证备份和回滚，不同时运行两个写入相同来源的 worker。
