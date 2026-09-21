# 验证与迁移

1. 核心测试：解析、SHA 增量、模型故障、权限撤回、HTTP 边界。
2. MCP：老版本握手、真实 TCP SDK、身份伪造、并发隔离、非法域名、参数和只读列表。
3. SuiteHarness：设置 `SUITEHARNESS_SOURCE_PATH` 为实际源码，运行 `tests/test_harness_plugin.py`。不改框架/产品。
4. PostgreSQL：测试只允许专用数据库 `knowseam_test`，以测试变量 `SUITE_KNOWLEDGE_TEST_DSN` 传入（不是产品配置渠道）。唯一集合隔离，不 DROP/TRUNCATE，不指向正式库。
5. 替身不等于模型质量：真实召回、引用、增量延迟需另行验收，公司数据不提交仓库。

本次只复制知识服务白名单源码和合成测试，保留 `sk_*` 表名和语义缓存标识。A/B、旧知识 API/worker、数据库、模型、凭据均不自动切换。发布前记录测试结果与旧容器 ID/镜像/启动时间；真正迁移需先备份并验证回滚，不移动运行中的数据目录。

有 Docker 的测试服务器可运行 `python scripts/verify_postgres.py --image <已存在的PostgreSQL17与pgvector镜像>`。只创建带唯一标签的临时容器；不拉镜像、不挂载宿主数据，随机回环端口，内存/CPU 有限额。测试后仅停止本次创建且标签匹配的容器；测试数据在 tmpfs，随容器销毁。
