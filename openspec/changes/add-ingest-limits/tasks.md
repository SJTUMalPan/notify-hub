# 任务

- [x] 1. 规格：`specs/message-ingest/spec.md` 新增「接入请求体上限」需求与场景
- [x] 2. 配置：`Settings.max_body_bytes` / `Settings.max_batch_items` + 严格解析（正整数）
- [x] 3. 测试（先红）：配置非法值报错；缺省值存在
- [x] 4. 中间件：`BodyLimit`（纯 ASGI，包装 `receive` 计字节）
- [x] 5. 测试（先红）：超限 413 且不写库；边界值（恰好等于上限）通过；分块传输超限也被拦
- [x] 6. 路由：`post_batch` 条数上限 → 413，整批拒绝
- [x] 7. 测试（先红）：超条数 413 且一条都没写；恰好等于上限返回 207
- [x] 8. 装配：`create_app` 里按 D5 的顺序注册两个中间件
- [x] 9. 文档：`config.example.yaml` + `docs/configuration.md` 补两个键与语义
- [x] 10. 文档契约测试：两个键必须同时出现在示例配置与配置文档里
- [x] 11. 全量测试（含既有 476 条）通过；`openspec validate` 通过（若可用）
