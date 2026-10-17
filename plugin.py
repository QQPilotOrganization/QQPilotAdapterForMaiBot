"""QQPilot 适配器：把 QQPilot 的 ChatCompletion 请求接到 MaiBot 消息网关。

插件通过 ``@MessageGateway`` 同时承担两个方向：
- 入站：QQPilot POST ``/v1/chat/completions``，解析聊天记录后注入 Host；
- 出站：Host 生成的回复写入当前请求的收集器，最终作为 ChatCompletion 返回。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import asyncio
import base64
import hashlib
import json
import logging
import select
import socket
import threading
import time
import uuid

from maibot_sdk import Field, MaiBotPlugin, MessageGateway, PluginConfigBase

from .conversation_store import Conversation, ConversationStore
from .media import parse_data_url, resolve_image_extension, split_images, to_data_url
from .protocol import (
    PLATFORM,
    REPLY_SEPARATOR,
    InboundMessage,
    RequestError,
    build_completion,
    build_empty_completion,
    build_stream_chunks,
    encode_sse,
    message_fingerprint,
    parse_chat_request,
)

GATEWAY_NAME = "qqpilot_gateway"
PLUGIN_VERSION = "2.3.0"
LOGGER_NAME = "plugin.com.na2cr2o7.qqpilot"


class PluginSectionConfig(PluginConfigBase):
    """插件级开关。"""

    __ui_label__ = "插件设置"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 QQPilot 适配器。",
        json_schema_extra={"label": "启用适配器", "order": 0},
    )
    account_id: str = Field(
        default="qqpilot",
        description="上报给 MaiBot 的账号标识。多个 QQPilot 实例接入时可分别设置。",
        json_schema_extra={"label": "账号标识", "order": 1},
    )
    config_version: str = Field(
        default="2.1.0",
        description="当前配置结构版本。",
        json_schema_extra={"label": "配置版本", "order": 99, "hidden": True, "disabled": True},
    )


class ServerConfig(PluginConfigBase):
    """HTTP 服务配置。"""

    __ui_label__ = "HTTP 服务"
    __ui_order__ = 1

    host: str = Field(
        default="127.0.0.1",
        description="监听地址。只在本机使用 QQPilot 时保持 127.0.0.1；需要局域网访问时改为 0.0.0.0。",
        json_schema_extra={"label": "监听地址", "order": 0},
    )
    port: int = Field(
        default=7749,
        ge=0,
        le=65535,
        description="监听端口，0 表示由系统随机分配。",
        json_schema_extra={"label": "监听端口", "order": 1},
    )
    auth_token: str = Field(
        default="",
        description="可选的 Bearer Token。填写后 QQPilot 的 api_key 必须与之一致。",
        json_schema_extra={"label": "访问令牌", "order": 2},
    )
    max_body_mb: int = Field(
        default=32,
        ge=1,
        le=512,
        description="单个请求体的最大大小（MB）。QQPilot 的图片以 base64 发送，建议不要设置过小。",
        json_schema_extra={"label": "请求体上限 (MB)", "order": 3},
    )


class ReplyConfig(PluginConfigBase):
    """等待回复的时序配置。"""

    __ui_label__ = "回复等待"
    __ui_order__ = 2

    total_timeout_sec: float = Field(
        default=120.0,
        ge=1.0,
        le=1800.0,
        description="一次请求最多等待多久；QQPilot 自身默认超时为 300 秒。",
        json_schema_extra={"label": "总超时 (秒)", "order": 0},
    )
    idle_timeout_sec: float = Field(
        default=15.0,
        ge=0.5,
        le=600.0,
        description="收到第一条回复后，连续多久没有新回复就结束等待。",
        json_schema_extra={"label": "空闲超时 (秒)", "order": 1},
    )


class ImageConfig(PluginConfigBase):
    """图片相关配置。"""

    __ui_label__ = "图片设置"
    __ui_order__ = 3

    path: str = Field(
        default="",
        description="可选：把回复图片额外保存到磁盘的目录。绝对路径或相对插件数据目录的路径。",
        json_schema_extra={"label": "图片保存目录", "order": 0},
    )
    save_to_disk: bool = Field(
        default=False,
        description="是否把回复图片额外保存到磁盘。现代 QQPilot 直接从响应 JSON 读取图片，通常无需开启。",
        json_schema_extra={"label": "保存回复图片到磁盘", "order": 1},
    )
    delete_after_next_request: bool = Field(
        default=False,
        description="下一次请求时删除上一次因保存到磁盘而产生的图片。",
        json_schema_extra={"label": "下次请求时删除旧图片", "order": 2},
    )
    max_reply_images: int = Field(
        default=9,
        ge=0,
        le=100,
        description="单个回复最多携带多少张图片，0 表示不限制。",
        json_schema_extra={"label": "最多回复图片数", "order": 3},
    )
    ImagePath: str = Field(
        default="",
        description="旧版配置项，等价于“图片保存目录”，仅为兼容旧配置保留。",
        json_schema_extra={"label": "旧版图片目录", "order": 90, "hidden": True, "disabled": True},
    )
    deleteImageInTheNextRequest: bool = Field(
        default=False,
        description="旧版配置项，等价于“下次请求时删除旧图片”，仅为兼容旧配置保留。",
        json_schema_extra={"label": "旧版删除图片", "order": 91, "hidden": True, "disabled": True},
    )

    @property
    def resolved_path(self) -> str:
        """兼容旧字段后的图片目录。"""

        return (self.path or self.ImagePath or "").strip()

    @property
    def should_save(self) -> bool:
        """是否应该把图片写盘。"""

        return bool(self.save_to_disk or self.ImagePath)

    @property
    def should_delete(self) -> bool:
        """是否应该在下一次请求时删除旧图片。"""

        return bool(self.delete_after_next_request or self.deleteImageInTheNextRequest)


class ConversationConfig(PluginConfigBase):
    """会话归类配置。"""

    __ui_label__ = "会话归类"
    __ui_order__ = 4

    single_user_as_private: bool = Field(
        default=True,
        description="当前上下文只有一个用户时按私聊处理（与 QQPilot 自身的判断一致）。",
        json_schema_extra={"label": "单用户视为私聊", "order": 0},
    )
    overlap_threshold: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="历史消息重叠比例达到该值时归入已有群聊。",
        json_schema_extra={"label": "消息重叠阈值", "order": 1},
    )
    member_overlap_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="没有足够消息重叠时，参与者重合比例达到该值且会话足够新也可归入已有群聊。",
        json_schema_extra={"label": "参与者重合阈值", "order": 2},
    )
    retention_days: int = Field(
        default=0,
        ge=0,
        le=3650,
        description="会话记录保留天数，0 表示永久保留。",
        json_schema_extra={"label": "会话保留天数", "order": 3},
    )


class DebugConfig(PluginConfigBase):
    """调试日志配置。"""

    __ui_label__ = "调试"
    __ui_order__ = 5

    log_requests: bool = Field(
        default=False,
        description="是否记录每次请求的消息数量与归类结果（不记录完整聊天内容）。",
        json_schema_extra={"label": "记录请求概况", "order": 0},
    )


class QQPilotConfig(PluginConfigBase):
    """QQPilot 适配器完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)


@dataclass
class ReplyPiece:
    """Host 发来的一段回复。"""

    text: str = ""
    images: List[str] = field(default_factory=list)


class ReplyCollector:
    """线程安全地收集一次请求期间 Host 产生的回复。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pieces: List[ReplyPiece] = []
        self._closed = False
        self._last_reply_at = 0.0

    def add(self, text: str, images: Sequence[str]) -> None:
        """追加一段回复。"""

        normalized_text = text or ""
        normalized_images = [image for image in images if image]
        if not normalized_text and not normalized_images:
            return
        with self._condition:
            if self._closed:
                return
            self._pieces.append(ReplyPiece(text=normalized_text, images=normalized_images))
            self._last_reply_at = time.monotonic()
            self._condition.notify_all()

    def wait(
        self,
        total_timeout_sec: float,
        idle_timeout_sec: float,
        is_disconnected: Optional[Callable[[], bool]] = None,
    ) -> List[ReplyPiece]:
        """等待回复：首条前按总超时，首条后按空闲超时。"""

        deadline = time.monotonic() + max(float(total_timeout_sec), 0.1)
        idle_timeout = max(float(idle_timeout_sec), 0.1)

        with self._condition:
            while not self._closed:
                if callable(is_disconnected) and is_disconnected():
                    break
                now = time.monotonic()
                if self._pieces:
                    target = min(deadline, self._last_reply_at + idle_timeout)
                else:
                    target = deadline
                remaining = target - now
                if remaining <= 0:
                    break
                # 分片等待，便于尽早发现客户端断开，而不是空等完整超时。
                self._condition.wait(timeout=min(remaining, 0.5))
            return list(self._pieces)

    @property
    def has_replies(self) -> bool:
        """是否已经收到至少一段回复。"""

        with self._condition:
            return bool(self._pieces)

    @property
    def closed(self) -> bool:
        """是否已经被外部关闭。"""

        with self._condition:
            return self._closed

    def close(self) -> None:
        """唤醒等待者并阻止后续写入。"""

        with self._condition:
            self._closed = True
            self._condition.notify_all()


class _AdapterHTTPServer(ThreadingHTTPServer):
    """携带插件实例的 ThreadingHTTPServer。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], plugin: "QQPilotPlugin") -> None:
        self.plugin = plugin
        super().__init__(address, _QQPilotRequestHandler)


class _QQPilotRequestHandler(BaseHTTPRequestHandler):
    """处理 QQPilot 的 OpenAI 兼容 HTTP 请求。"""

    protocol_version = "HTTP/1.1"
    server_version = f"QQPilotAdapter/{PLUGIN_VERSION}"

    @property
    def plugin(self) -> "QQPilotPlugin":
        return self.server.plugin  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        self.plugin.logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def _common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json_dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, error_type: str = "invalid_request_error") -> None:
        self._send_json(
            status,
            {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": status,
                }
            },
        )

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise RequestError(411, "缺少有效的 Content-Length 请求头")
        if length <= 0:
            raise RequestError(400, "请求体为空")
        max_bytes = self.plugin.max_body_bytes
        if length > max_bytes:
            raise RequestError(413, f"请求体过大：{length} 字节，上限 {max_bytes} 字节")
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError(400, "请求体读取不完整")
        return body

    def _check_auth(self) -> None:
        token = self.plugin.config.server.auth_token.strip()
        if not token:
            return
        authorization = self.headers.get("Authorization", "").strip()
        expected = f"Bearer {token}"
        if authorization != expected:
            raise RequestError(401, "鉴权失败：请在 QQPilot 中填写正确的 api_key")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self._common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, self.plugin.health_payload())
            return
        if path in ("/", ""):
            self._send_text(200, "QQPilot Adapter for MaiBot")
            return
        if path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "qqpilot-mai",
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "MaiBot",
                        }
                    ],
                },
            )
            return
        self._send_error_json(404, f"未找到路径：{path}", "not_found_error")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_error_json(404, f"未找到路径：{path}", "not_found_error")
            return

        try:
            self._check_auth()
            raw_body = self._read_body()
            body = parse_json_body(raw_body)
            completion = self.plugin.handle_completion_request(body, is_disconnected=self._client_disconnected)
            if bool(body.get("stream", False)):
                self._send_sse(completion)
            else:
                self._send_json(200, completion)
        except (BrokenPipeError, ConnectionError) as exc:
            # QQPilot 自身超时或主动断开时非常常见，不应再尝试写 500。
            self.plugin.logger.debug("客户端已断开，停止等待：%s", exc)
        except RequestError as exc:
            self.plugin.logger.warning("请求被拒绝：%s", exc.message)
            self._safe_send_error(exc.status_code, exc.message, "invalid_request_error")
        except Exception as exc:
            self.plugin.logger.exception("处理 ChatCompletion 请求失败")
            self._safe_send_error(500, f"适配器内部错误：{exc}", "server_error")

    def _safe_send_error(self, status: int, message: str, error_type: str) -> None:
        """客户端可能已经断开，错误响应失败时只记调试日志。"""

        try:
            self._send_error_json(status, message, error_type)
        except (BrokenPipeError, ConnectionError) as exc:
            self.plugin.logger.debug("客户端已断开，无法返回错误响应：%s", exc)

    def _client_disconnected(self) -> bool:
        """非阻塞检查客户端是否已经关闭连接。"""

        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            if self.connection.recv(1, socket.MSG_PEEK) == b"":
                self.close_connection = True
                return True
            return False
        except (OSError, ValueError):
            self.close_connection = True
            return True

    def _send_sse(self, completion: Dict[str, Any]) -> None:
        body = encode_sse(build_stream_chunks(completion))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def json_dumps(payload: Dict[str, Any]) -> bytes:
    """以紧凑、保留中文的方式序列化 JSON。"""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_json_body(raw_body: bytes) -> Dict[str, Any]:
    """解析请求 JSON 并给出中文错误。"""

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RequestError(400, "请求体不是 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise RequestError(400, f"请求体不是合法 JSON：{exc.msg}") from exc
    if not isinstance(body, dict):
        raise RequestError(400, "请求体必须是 JSON 对象")
    return body


class QQPilotPlugin(MaiBotPlugin):
    """QQPilot OpenAI ChatCompletion 适配器。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = QQPilotConfig
    config_reload_subscriptions: ClassVar[Sequence[str]] = ()

    def __init__(self) -> None:
        super().__init__()
        self._logger = logging.getLogger(LOGGER_NAME)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store: Optional[ConversationStore] = None
        self._httpd: Optional[_AdapterHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._collectors: Dict[str, ReplyCollector] = {}
        self._collectors_lock = threading.Lock()
        self._images_lock = threading.Lock()
        self._saved_images: List[str] = []
        self._ready = False
        self._bound_host = ""
        self._bound_port = 0
        self._active_network: Optional[Tuple[str, int, str, int]] = None

    # ------------------------------------------------------------------ 生命周期

    async def on_load(self) -> None:
        """初始化存储，启动 HTTP 服务并上报网关就绪。"""

        self._loop = asyncio.get_running_loop()
        self._initialize_store()
        if self.config.plugin.enabled:
            started = await self._start_server()
            await self._set_ready(started)
            if started:
                self._logger.info(
                    "QQPilot 适配器已就绪：在 QQPilot 中填写 http://%s:%s/v1",
                    self._display_host,
                    self._bound_port,
                )
        else:
            self._logger.info("QQPilot 适配器已在配置中禁用，未启动 HTTP 服务")
            await self._set_ready(False)

    async def on_unload(self) -> None:
        """停止服务、唤醒等待中的请求并上报离线。"""

        with self._collectors_lock:
            collectors = list(self._collectors.values())
            self._collectors.clear()
        for collector in collectors:
            collector.close()

        await self._stop_server()
        await self._set_ready(False)
        if self._store is not None:
            self._store.close()
            self._store = None
        self._logger.info("QQPilot 适配器已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：必要时重启 HTTP 服务。"""

        del version
        if scope != "self":
            return

        old_network = self._active_network
        self.set_plugin_config(config_data)
        self._initialize_store()

        new_network = self._network_signature()
        enabled = self.config.plugin.enabled
        running = self._httpd is not None

        if not enabled:
            if running:
                await self._stop_server()
            await self._set_ready(False)
            self._logger.info("QQPilot 适配器已禁用")
            return

        if running and new_network == old_network:
            self._logger.info("QQPilot 配置已更新，HTTP 监听参数未变化，无需重启服务")
            await self._set_ready(True)
            return

        if running:
            await self._stop_server()
        started = await self._start_server()
        await self._set_ready(started)
        if started:
            self._logger.info(
                "QQPilot 适配器已按新配置启动：http://%s:%s/v1",
                self._display_host,
                self._bound_port,
            )

    # ------------------------------------------------------------------ 内部状态

    @property
    def logger(self) -> logging.Logger:
        """优先使用 Runner 注入的上下文日志。"""

        if self._ctx is not None:
            return self.ctx.logger
        return self._logger

    @property
    def max_body_bytes(self) -> int:
        return int(self.config.server.max_body_mb) * 1024 * 1024

    @property
    def _display_host(self) -> str:
        if self._bound_host in ("0.0.0.0", "::", ""):
            return "127.0.0.1"
        return self._bound_host

    def _network_signature(self) -> Tuple[str, int, str, int]:
        server = self.config.server
        return (
            server.host.strip() or "127.0.0.1",
            int(server.port),
            server.auth_token.strip(),
            int(server.max_body_mb),
        )

    def _initialize_store(self) -> None:
        """按当前配置初始化或更新存储。"""

        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        if self._store is None:
            self._store = ConversationStore(
                data_dir / "qqpilot.sqlite3",
                overlap_threshold=self.config.conversation.overlap_threshold,
                member_overlap_threshold=self.config.conversation.member_overlap_threshold,
                single_user_as_private=self.config.conversation.single_user_as_private,
            )
            self._store.initialize()
        else:
            self._store.overlap_threshold = self.config.conversation.overlap_threshold
            self._store.member_overlap_threshold = self.config.conversation.member_overlap_threshold
            self._store.single_user_as_private = self.config.conversation.single_user_as_private

        if self.config.conversation.retention_days > 0:
            removed = self._store.prune(self.config.conversation.retention_days)
            if removed:
                self._logger.info("已清理 %s 个超过保留期的 QQPilot 会话", removed)

    async def _set_ready(self, ready: bool) -> None:
        """向 Host 上报网关状态。"""

        self._ready = False
        try:
            accepted = await self.ctx.gateway.update_state(
                gateway_name=GATEWAY_NAME,
                ready=ready,
                platform=PLATFORM,
                account_id=self.config.plugin.account_id.strip() or "qqpilot",
                scope="primary",
                metadata={"protocol": "qqpilot", "version": PLUGIN_VERSION},
            )
        except Exception:
            self.logger.exception("上报 QQPilot 网关状态失败")
            return
        self._ready = bool(ready and accepted)
        if ready and not accepted:
            self.logger.error("Host 未接受 QQPilot 网关注册，入站消息将无法注入")

    async def _start_server(self) -> bool:
        """在线程中启动 HTTP 服务，返回是否成功。"""

        if self._httpd is not None:
            return True
        try:
            host, port = await asyncio.to_thread(self._start_server_sync)
        except OSError as exc:
            self.logger.error(
                "启动 QQPilot HTTP 服务失败：%s:%s 无法监听（%s）。"
                "请检查端口是否被占用，或在插件配置中修改 server.port。",
                self.config.server.host,
                self.config.server.port,
                exc,
            )
            return False
        except Exception:
            self.logger.exception("启动 QQPilot HTTP 服务时发生未知错误")
            return False
        self._bound_host = host
        self._bound_port = port
        self._active_network = self._network_signature()
        return True

    def _start_server_sync(self) -> Tuple[str, int]:
        host = self.config.server.host.strip() or "127.0.0.1"
        port = int(self.config.server.port)
        httpd = _AdapterHTTPServer((host, port), self)
        thread = threading.Thread(target=httpd.serve_forever, name="qqpilot-http", daemon=True)
        thread.start()
        self._httpd = httpd
        self._server_thread = thread
        return host, int(httpd.server_address[1])

    async def _stop_server(self) -> None:
        """停止 HTTP 服务。"""

        if self._httpd is None:
            return
        await asyncio.to_thread(self._stop_server_sync)

    def _stop_server_sync(self) -> None:
        httpd = self._httpd
        thread = self._server_thread
        self._httpd = None
        self._server_thread = None
        self._bound_host = ""
        self._bound_port = 0
        self._active_network = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    # ------------------------------------------------------------------ 入站

    def handle_completion_request(
        self,
        body: Dict[str, Any],
        *,
        is_disconnected: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """处理一次 ChatCompletion 请求，返回 OpenAI 兼容响应。"""

        if not self._ready:
            raise RequestError(503, "QQPilot 适配器尚未就绪，请查看 MaiBot 日志")
        if self._loop is None or self._store is None:
            raise RequestError(503, "QQPilot 适配器内部状态未初始化")

        parsed = parse_chat_request(body)
        self._classify_inbound_media(parsed.messages)
        if self.config.debug.log_requests:
            self.logger.info(
                "收到 ChatCompletion 请求：model=%s，消息 %s 条，stream=%s",
                parsed.model,
                len(parsed.messages),
                parsed.stream,
            )

        self._delete_previous_images()
        conversation = self._store.resolve(parsed.messages)
        fingerprints = [message.fingerprint for message in parsed.messages]
        known = self._store.known_fingerprints(conversation.conversation_id, fingerprints)
        # 会话归类可能因保留期或用户改名而变化；全局去重保证历史消息不会被重放。
        known.update(self._store.known_fingerprints_global(fingerprints))
        new_messages = [message for message in parsed.messages if message.fingerprint not in known]

        if not new_messages:
            if self.config.debug.log_requests:
                self.logger.info("会话 %s 没有新消息，返回空回复", conversation.conversation_id)
            return build_empty_completion(parsed.model)

        collector = ReplyCollector()
        with self._collectors_lock:
            if conversation.conversation_id in self._collectors:
                raise RequestError(409, "该会话正在处理上一条请求，请稍后重试")
            self._collectors[conversation.conversation_id] = collector

        try:
            for message in new_messages:
                self._route_inbound(message, conversation)
            self._store.record_messages(conversation.conversation_id, new_messages)
            pieces = collector.wait(
                self.config.reply.total_timeout_sec,
                self.config.reply.idle_timeout_sec,
                is_disconnected=is_disconnected,
            )
        finally:
            with self._collectors_lock:
                self._collectors.pop(conversation.conversation_id, None)

        if not pieces:
            if collector.closed:
                self.logger.debug("等待中的 ChatCompletion 请求因插件卸载而结束")
            elif callable(is_disconnected) and is_disconnected():
                self.logger.debug("客户端已断开，停止等待 MaiBot 回复")
            else:
                self.logger.warning(
                    "等待 MaiBot 回复超时：会话=%s，消息=%s，总超时=%.1fs",
                    conversation.conversation_id,
                    len(new_messages),
                    self.config.reply.total_timeout_sec,
                )
            return build_empty_completion(parsed.model)

        text = REPLY_SEPARATOR.join(piece.text for piece in pieces if piece.text)
        images: List[str] = []
        for piece in pieces:
            images.extend(piece.images)
        limit = int(self.config.image.max_reply_images)
        if limit > 0 and len(images) > limit:
            self.logger.warning("回复图片 %s 张，超过上限 %s，已截断", len(images), limit)
            images = images[:limit]

        if text or images:
            self.logger.info(
                "会话 %s 收到回复：文本 %s 字，图片 %s 张",
                conversation.conversation_id,
                len(text),
                len(images),
            )
        return build_completion(parsed.model, text, images)

    def _route_inbound(self, message: InboundMessage, conversation: Conversation) -> None:
        """把一条消息提交到 Host 事件循环并等待注入结果。"""

        if self._loop is None:
            raise RequestError(503, "插件事件循环未就绪")
        future = asyncio.run_coroutine_threadsafe(self._route_inbound_async(message, conversation), self._loop)
        try:
            accepted = future.result(timeout=30.0)
        except Exception as exc:
            raise RequestError(502, f"注入 MaiBot 失败：{exc}") from exc
        if not accepted:
            raise RequestError(502, "MaiBot 未接收入站消息，请检查平台与账号配置")

    @staticmethod
    def _classify_inbound_media(messages: Sequence[InboundMessage]) -> None:
        """把普通图片与表情包分开，并按最终媒体集合重算指纹。"""

        for message in messages:
            if not message.images:
                continue
            images, emojis = split_images(message.images)
            if not emojis:
                continue
            message.images = images
            message.emojis = emojis
            message.fingerprint = message_fingerprint(
                message.timestamp_text,
                message.username,
                message.content,
                message.images,
                message.emojis,
            )

    async def _route_inbound_async(self, message: InboundMessage, conversation: Conversation) -> bool:
        """构造标准消息字典并调用网关。"""

        raw_message: List[Dict[str, Any]] = [{"type": "text", "data": {"text": message.plain_text}}]
        for image_url in message.images:
            raw_message.append(self._build_media_segment("image", image_url))
        for emoji_url in message.emojis:
            raw_message.append(self._build_media_segment("emoji", emoji_url))

        message_info: Dict[str, Any] = {
            "platform": PLATFORM,
            "message_id": f"qqpilot-{message.fingerprint[:24]}",
            "time": int(message.timestamp_seconds),
            "user_info": {
                "user_id": message.user_id,
                "user_nickname": message.display_username,
            },
            "additional_config": {"qqpilot_conversation_id": conversation.conversation_id},
        }
        if conversation.kind == "group":
            message_info["group_info"] = {
                "group_id": conversation.conversation_id,
                "group_name": conversation.title,
            }

        accepted = await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message={
                "message_id": f"qqpilot-{message.fingerprint[:24]}",
                "timestamp": str(float(message.timestamp_seconds)),
                "platform": PLATFORM,
                "account_id": self.config.plugin.account_id.strip() or "qqpilot",
                "scope": "primary",
                "message_info": message_info,
                "raw_message": raw_message,
                "processed_plain_text": message.plain_text,
                "display_message": message.plain_text,
                "is_mentioned": False,
                "is_at": False,
                "is_emoji": bool(message.emojis),
                "is_picture": bool(message.images),
                "is_command": message.plain_text.startswith("/"),
                "is_notify": False,
                "session_id": "",
            },
            route_metadata={
                "self_id": self.config.plugin.account_id.strip() or "qqpilot",
                "connection_id": "primary",
            },
            external_message_id=f"qqpilot-{message.fingerprint[:24]}",
            dedupe_key=f"qqpilot-{message.fingerprint[:24]}",
        )
        if not accepted:
            self.logger.warning("Host 未接收入站消息：%s", message.fingerprint[:16])
        return bool(accepted)

    @staticmethod
    def _build_media_segment(segment_type: str, data_url: str) -> Dict[str, Any]:
        """把 Data URL 转换为 Host 的二进制消息段。"""

        try:
            _, binary = parse_data_url(data_url)
        except ValueError:
            return {"type": "text", "data": {"text": f"[无法解析的{segment_type}]"}}
        digest = hashlib.sha256(binary).hexdigest()
        return {
            "type": segment_type,
            "data": "",
            "hash": digest,
            "binary_data_base64": base64.b64encode(binary).decode("ascii"),
        }

    # ------------------------------------------------------------------ 出站

    @MessageGateway(
        route_type="duplex",
        name=GATEWAY_NAME,
        platform=PLATFORM,
        protocol="qqpilot",
        description="QQPilot ChatCompletion 双工消息网关",
    )
    async def send_to_platform(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """接收 Host 出站消息，写入等待中的 ChatCompletion 请求。"""

        del route, metadata, kwargs
        text, images = self._extract_outbound(message)
        if not text and not images:
            return {"success": True, "external_message_id": None, "metadata": {"empty": True}}

        conversation_id = self._outbound_conversation_id(message)
        collector = self._pick_collector(conversation_id)
        if collector is None:
            self.logger.warning("收到 MaiBot 回复但当前没有等待中的 QQPilot 请求，已丢弃")
            return {
                "success": True,
                "external_message_id": None,
                "metadata": {"dropped": True, "reason": "no_active_request"},
            }

        if images and self.config.image.should_save:
            for image_url in images:
                self._save_image(image_url)
        collector.add(text, images)
        external_id = f"qqpilot-reply-{uuid.uuid4().hex[:16]}"
        if self.config.debug.log_requests:
            self.logger.info(
                "投递回复到会话 %s：文本 %s 字，图片 %s 张",
                conversation_id or "<fallback>",
                len(text),
                len(images),
            )
        return {"success": True, "external_message_id": external_id}

    def _extract_outbound(self, message: Dict[str, Any]) -> Tuple[str, List[str]]:
        """从 Host 消息中提取文本与图片 Data URL。"""

        texts: List[str] = []
        images: List[str] = []

        raw_message = message.get("raw_message")
        if isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, dict):
                    continue
                segment_type = str(segment.get("type") or "").strip().lower()
                data = segment.get("data")
                if segment_type == "text":
                    if isinstance(data, str):
                        texts.append(data)
                    elif isinstance(data, dict):
                        texts.append(str(data.get("text") or ""))
                    continue
                if segment_type == "at" and isinstance(data, dict):
                    nickname = str(data.get("target_user_nickname") or "").strip()
                    if nickname:
                        texts.append(f"@{nickname}")
                    continue
                if segment_type not in ("image", "emoji"):
                    continue
                image_url = self._segment_to_data_url(segment)
                if image_url:
                    images.append(image_url)

        if not texts and isinstance(message.get("processed_plain_text"), str):
            texts.append(message["processed_plain_text"])

        return "".join(texts).strip(), images

    @staticmethod
    def _segment_to_data_url(segment: Dict[str, Any]) -> str:
        """把图片消息段转换成 QQPilot 可读取的 Data URL。"""

        encoded = segment.get("binary_data_base64")
        if isinstance(encoded, str) and encoded:
            try:
                binary = base64.b64decode(encoded)
            except Exception:
                return ""
            if binary:
                return to_data_url(binary)

        data = segment.get("data")
        if isinstance(data, str) and data.startswith("data:image/"):
            return data
        return ""

    def _outbound_conversation_id(self, message: Dict[str, Any]) -> Optional[str]:
        """根据 Host 消息中的群/用户信息定位等待中的会话。"""

        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return None
        group_info = message_info.get("group_info")
        if isinstance(group_info, dict):
            group_id = str(group_info.get("group_id") or "").strip()
            if group_id:
                return group_id
        user_info = message_info.get("user_info")
        if isinstance(user_info, dict) and self._store is not None:
            user_id = str(user_info.get("user_id") or "").strip()
            if user_id:
                return self._store.private_conversation_for_user(user_id)
        return None

    def _pick_collector(self, conversation_id: Optional[str]) -> Optional[ReplyCollector]:
        """优先精确匹配会话，其次在唯一活跃请求时回退。"""

        with self._collectors_lock:
            if conversation_id and conversation_id in self._collectors:
                return self._collectors[conversation_id]
            if len(self._collectors) == 1:
                return next(iter(self._collectors.values()))
            if conversation_id:
                self.logger.debug("未找到会话 %s 对应的等待请求", conversation_id)
            return None

    # ------------------------------------------------------------------ 图片落盘

    def _image_directory(self) -> Optional[Path]:
        configured = self.config.image.resolved_path
        if not configured:
            return None
        path = Path(configured)
        if not path.is_absolute():
            path = Path(self.ctx.paths.data_dir) / path
        return path

    def _save_image(self, data_url: str) -> None:
        """按配置把图片额外保存到磁盘。"""

        directory = self._image_directory()
        if directory is None:
            return
        try:
            mime, binary = parse_data_url(data_url)
            directory.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{resolve_image_extension(mime)}"
            filepath = directory / filename
            filepath.write_bytes(binary)
            if self.config.image.should_delete:
                with self._images_lock:
                    self._saved_images.append(str(filepath))
            self.logger.debug("回复图片已保存：%s", filepath)
        except Exception:
            self.logger.exception("保存回复图片失败")

    def _delete_previous_images(self) -> None:
        """删除上一次请求保存的图片（可选功能）。"""

        if not self.config.image.should_delete:
            return
        with self._images_lock:
            saved = list(self._saved_images)
            self._saved_images.clear()
        for filepath in saved:
            try:
                Path(filepath).unlink(missing_ok=True)
            except OSError as exc:
                self.logger.warning("删除旧图片失败：%s（%s）", filepath, exc)

    # ------------------------------------------------------------------ 健康检查

    def health_payload(self) -> Dict[str, Any]:
        """返回可直接用于排障的健康状态。"""

        stats = self._store.stats() if self._store is not None else {}
        return {
            "status": "ok" if self._ready else "not_ready",
            "plugin": "QQPilotAdapter",
            "version": PLUGIN_VERSION,
            "gateway": GATEWAY_NAME,
            "listen": {"host": self._bound_host, "port": self._bound_port},
            "active_requests": len(self._collectors),
            "store": stats,
        }

    @property
    def is_ready(self) -> bool:
        """当前网关是否已就绪。"""

        return self._ready


def create_plugin() -> QQPilotPlugin:
    """SDK 插件工厂函数。"""

    return QQPilotPlugin()
