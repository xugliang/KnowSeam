# 来源权限与独立同步

开发中新增的可选能力，默认关闭。迁移与配置启用不是自动执行的操作。飞书私有用户/群组/Wiki 继承权限的完整自动同步尚未实现，真实租户变更也未验收；不能将本文件理解为所有飞书权限类型均已支持。

## 权限交集

可信调用身份 → 集合权限 → 来源启用、当前文档状态 → 新鲜且完整的来源权限 → 员工/组可信映射。

- public_verified：仅放行来源明确公开给全公司的资料，保留原行为。
- source_acl：允许完整核实的私有来源授权与集合授权取交集。内部员工需有效，映射需未过期，不能由模型提供 actor 或 group。
- unknown、revoked、过期、来源配置不匹配均拒读。空授权清单不等于未知清单。
- 私有文档的旧 public 字段保持 false。旧版本服务不会因新增私有资料而获得访问权。

## 顺序与发布

在网络请求前分配单调观察编号，结果应用时通过短事务、文档锁和编号比较拒绝迟到观察。独立 worker 不获取正文同步的长时间来源锁。

观察顺序与有效权限版本是两个概念：前者拦截迟到响应；后者用于发布时核验授权语义。定期核实相同权限不应导致长时间解析/模型工作永远无法提交。

配置切换后，旧 worker 的 expected_config_hash 与数据库不匹配，禁止借用新配置的观察编号。正文扫描、前后权限检查、独立同步、配置撤销及文档删除共用权威状态；普通读取、上下文、表格与图证据均在当前权限下验证。

## 独立命令

完成显式迁移并配置 permissions.enabled 后：

```bash
python -m knowseam --config /absolute/private-config.json permissions-sync
python -m knowseam --config /absolute/private-config.json permissions-worker
```

两者不初始化向量模型、重排序模型、分词器或解析器，也不下载文档正文。间隔默认 300 秒，范围 30–3600 秒，必须严格小于 acl_ttl_seconds。

权限 worker 读取已配置的来源，不负责替换员工目录或重新启用已禁用来源。配置仍由管理员维护，授权更改需经过明确配置流程。

## 适配器实际边界

| 来源 | 权限探测 | 私有授权 |
| --- | --- | --- |
| filesystem | 明确 public_files、路径安全校验、文件存在性，不读取文件内容 | 不推断操作系统 ACL |
| 飞书 Wiki 中 docx/sheet/file | 延续现有底层文档公开链接检查，可独立撤权 | 完整继承未证明时返回 unknown/unsupported |
| 其他 connector | 需实现 PermissionConnector，并提供完整来源语义 | 可复用通用用户/组授权存储与可信映射 |

飞书官方说明直接协作者列表不代表完整继承链，参见[官方权限治理说明](https://github.com/larksuite/cli/blob/main/skills/lark-drive/references/lark-drive-workflow-permission-governance.md)。因此不能把“成功枚举直接成员”包装成 Wiki 私有权限全量同步。文档类型、继承与目录映射仍需真实测试资料逐项验收。

## 回退

先停止独立权限写入与内容写入，禁用所有 source_acl 私有来源，核验 public=false，再切回旧代码；不能通过把私有文档改为公开实现兼容。关闭能力不删除历史版本或授权审计数据。迁移只在经授权的数据库上显式执行。
