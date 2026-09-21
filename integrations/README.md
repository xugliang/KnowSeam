# 智能体接入

客户端共用一个服务端知识库。MCP 只暴露 `knowledge_list`、`knowledge_search`、`knowledge_read`；没有 SQL、写文件、同步、发题或抄送工具。

## 管理员

1. 配置核心 REST 与同步 worker。为网关创建专用后端令牌，核心 `clients` 绑定网关公司、产品、集合与员工范围；不复用 A 的令牌。
2. 每个员工生成独立随机令牌（至少 32 字符），网关 `bindings` 存其 SHA-256，绑定身份和集合。原令牌通过公司安全渠道交付，不共享账号。
3. 配置真实 `allowed_hosts`、`allowed_origins`，通过 HTTPS 代理发布 `/mcp`；默认仅回环监听。
4. 撤销/修改凭据后仅重启网关。首版没有自动过期/OAuth 登录；由管理员发放撤销。

模型不能填写身份、令牌、服务器地址。个人身份不取自 `X-Actor-Id`。后端令牌只保存在网关，不交给员工。

可运行 `scripts/create_client_credential.py --directory /private/new-employee --url https://knowledge.example.com/mcp --actor employee-001 --company company --product knowledge-agent --collections company-public` 生成私有客户端配置和服务端绑定。父目录需预先存在；工具拒绝覆盖旧目录，不在终端打印令牌。将 `binding.local.json` 内容加入网关 `bindings`，`client.local.json` 安全交付该员工。Windows 文件访问权限还需通过 ACL 限制为本人可读。

## Codex

`codex/knowseam/` 是按插件规范生成的轻量包。安装前修改 `.mcp.json` 中 Python 和私有配置文件的绝对路径。该 Python 环境安装本仓 `.[mcp]`，仅运行桥接，不部署本地数据库/模型。

也可直接配置：

```toml
[mcp_servers.knowseam]
command = "/absolute/venv/bin/python"
args = ["-m", "knowseam.bridge", "--config", "/absolute/private/knowseam-client.json"]
```

配置参考 `examples/mcp-client.example.json`，真实配置不提交仓库。桥接使凭据留在文件中，不放环境变量或命令参数。支持远程 HTTP 的客户端也可用 URL + 个人 Bearer 认证头直连。

## Claude Code

项目 `.mcp.json` 可使用：

```json
{"mcpServers":{"knowseam":{"command":"/absolute/venv/bin/python","args":["-m","knowseam.bridge","--config","/absolute/private/knowseam-client.json"]}}}
```

也可原生 HTTP 直连，按客户端安全凭据方式配置个人令牌，不提交真实认证头。

## DeepSeek Harness

通过其 MCP 客户端注册 Streamable HTTP 服务（URL + 个人认证头）或上述 stdio 命令。不同版本的配置入口以实际 MCP 客户端为准，不假设统一插件清单。结果同时提供文本与结构化内容。

## SuiteHarness

优先使用底座已有 MCP 接入，无需改框架。原生工具方式可显式导入 `knowseam.harness_plugin`，通过 `KnowledgePluginConfig` 和 `install_knowledge_tools(context, config)` 注册，底座需授予 `knowledge.read`。原生适配器身份来自已验证的执行作用域，服务令牌仅给可信产品后端。

本次不自动修改 A。旧 `suite_knowledge` 继续运行；后续更换产品导入路径时单独验收。不要覆盖旧部署目录。

## 协议依据与验收范围

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP](https://developers.openai.com/zh-Hans/docs/extend/mcp)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [DeepSeek Harness MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)

协议自动化测试不等于四种客户端均已人工安装验收；实际客户端版本和网络需单独验证。
