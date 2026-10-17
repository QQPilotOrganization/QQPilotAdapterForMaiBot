# QQPilotAdapter

把 [QQPilot](https://github.com/Na2Cr2O7/QQPilot) 的 OpenAI ChatCompletion 请求接入 [MaiBot](https://docs.mai-mai.org/) 的第三方适配器。

QQPilot 会把聊天记录拼成：

```text
[time] 2023-10-17 10:00 [username] Alice [content] 你好，请问在吗？
[time] 2023-10-17 10:01 [username] Bob [content] 在的，有什么可以帮你？
```

本插件解析这些消息并注入 MaiBot，再把 MaiBot 的回复作为 ChatCompletion 响应返回给 QQPilot。旧版本使用 `plugins.QQPilotAdapter.*` 绝对导入，在 MaiBot 新版插件 Runner 中会报 `No module named 'plugins.QQPilotAdapter'`；2.3.0 已改为 SDK 相对导入并重写启动链路。

## 安装

1. 将整个目录放到 `MaiBot/plugins/QQPilotAdapter/`（目录名必须是 `QQPilotAdapter`）。
2. 确认 MaiBot 的 `pyproject.toml` / 虚拟环境中有 `maibot-plugin-sdk >= 2.10.0` 和 `pillow`。
3. 启动 MaiBot，确认日志出现：
   ```text
   QQPilot 适配器已就绪：在 QQPilot 中填写 http://127.0.0.1:7749/v1
   ```

## QQPilot 设置

在 QQPilot 的服务器地址中填写：

```text
http://127.0.0.1:7749/v1
```

- `model` 随便填，插件会原样回显。
- `stream` 为 `false` 时返回普通 JSON；为 `true` 时返回 SSE。
- 如果配置了 `server.auth_token`，QQPilot 的 `api_key` 必须填写相同的值。
- QQPilot 的 `remote_server_timeout` 建议不低于插件的 `reply.total_timeout_sec`。

## 插件配置

配置文件为插件目录下的 `config.toml`：

```toml
[plugin]
enabled = true
account_id = "qqpilot"
config_version = "2.1.0"

[server]
host = "127.0.0.1"       # 只在本机使用时保持 127.0.0.1
port = 7749
auth_token = ""          # 可选，填写后必须与 QQPilot 的 api_key 一致
max_body_mb = 32

[reply]
total_timeout_sec = 120.0 # 一次请求最多等多久
idle_timeout_sec = 15.0   # 首条回复后多久没有新回复就结束

[image]
path = ""                 # 可选：额外把图片保存到磁盘
save_to_disk = false      # 现代 QQPilot 直接从响应 JSON 读图，通常无需开启
delete_after_next_request = false
max_reply_images = 9

[conversation]
single_user_as_private = true
overlap_threshold = 0.1
member_overlap_threshold = 0.8
retention_days = 0

[debug]
log_requests = false
```

旧版 `[image] ImagePath` 与 `deleteImageInTheNextRequest` 仍然保留兼容；如果只配置了旧字段，插件会按旧字段工作。

## 图片

- 入站：QQPilot 以 `data:image/...;base64,...` 发送图片，插件会按尺寸区分普通图片和表情包，并注入 MaiBot。
- 出站：插件把 MaiBot 回复中的图片放入：

  ```json
  {
    "choices": [
      {
        "message": {
          "role": "assistant",
          "content": "回复文本",
          "images": [
            { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }
          ]
        }
      }
    ]
  }
  ```

  QQPilot 1.5.14+ 会从 `choices[0].message.images` 读取图片，因此不再依赖 `data/Images` 目录。

## 会话归类与去重

QQPilot 的请求没有群号，插件会用历史消息指纹和参与者重合度判断是否属于同一个会话：

- 只有一个用户时按私聊处理，私聊 ID 由用户名稳定生成；
- 多个用户时优先匹配历史消息重叠度 ≥ `overlap_threshold` 的群聊；
- 没有足够重叠时，如果参与者重合度 ≥ `member_overlap_threshold` 且会话足够新，也归入该群；
- 已注入的消息按 `(会话, 消息指纹)` 去重，QQPilot 每次携带完整上下文也不会重复触发 MaiBot 回复。

## 排障

- `No module named 'plugins.QQPilotAdapter'`：确认使用 2.3.0，且插件目录名为 `QQPilotAdapter`。
- `启动 QQPilot HTTP 服务失败`：端口被占用，修改 `server.port` 后重启或热更新配置。
- `QQPilot 适配器尚未就绪`：查看 MaiBot 日志中是否有网关注册失败或商店/模型初始化错误。
- `等待 MaiBot 回复超时`：MaiBot 没有产生回复，或 `total_timeout_sec` 太短。QQPilot 会得到空回复。
- 健康检查：`GET http://127.0.0.1:7749/health`。

## 测试

在仓库根目录执行：

```powershell
.\Test\MaiBot\.venv\Scripts\python.exe -m pytest tests -q
```

联调时可启动 MaiBot 后发送：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:7749/v1/chat/completions `
  -ContentType 'application/json' -Body (@{
    model = 'qqpilot-mai'
    stream = $false
    messages = @(@{
      role = 'user'
      content = "[time] $(Get-Date -Format 'yyyy-MM-dd HH:mm') [username] 测试用户 [content] 你好"
    })
  } | ConvertTo-Json -Depth 5)
```

## 限制

- 与主动回复类插件不兼容：没有等待中的 ChatCompletion 请求时，MaiBot 的主动消息没有可返回的通道。
- 群号由插件推断，QQPilot 请求中不含真实 QQ 群号。
- 纯图片且没有文本的 QQPilot 消息不携带用户名，插件会跳过并记录日志。
