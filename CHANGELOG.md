# Changelog

## 2.3.0

### 主要功能

- 重写为 maibot-plugin-sdk 适配器，使用 `on_load` / `on_unload` / `on_config_update` / `create_plugin` 生命周期。
- 修复 `No module named 'plugins.QQPilotAdapter'`：改用包内相对导入，兼容新版插件 Runner 的动态包名。
- 使用 `@MessageGateway` 同时处理 QQPilot 入站消息与 MaiBot 出站回复。
- 回复图片改为写入 `choices[0].message.images` 的 Data URL，适配 QQPilot 1.5.14+。
- 支持非流式 JSON 与 SSE 流式响应。

### 细节

- HTTP 服务改用 `ThreadingHTTPServer`，支持并发请求，并增加 `/health`、`/v1/models`、CORS 与可选 Bearer 鉴权。
- 增加配置项：监听地址/端口、访问令牌、请求体上限、回复总超时/空闲超时、图片落盘与保留策略。
- 会话归类改为“消息重叠度 + 参与者重合度”，避免旧关键词算法把不同群聊混在一起。
- 消息 ID、用户 ID 改为稳定哈希，支持跨进程去重，并修复 Python `hash()` 随机化导致用户 ID 变化的问题。
- 时间解析支持 ISO、`MM-DD HH:MM[:SS]`、`MM月DD日` 等 QQPilot 常见格式。
- 移除对 `colorama` 和全局 `print` 猴补丁的依赖，统一使用插件日志。
- 增加协议、存储与 HTTP 集成测试。
