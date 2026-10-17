"""HTTP + 网关的端到端集成测试（不启动完整 MaiBot）。"""

from pathlib import Path
from typing import Any, Dict, List

import asyncio
import http.client
import json
import logging
import time
import urllib.error
import urllib.request

import pytest

from QQPilotAdapter.plugin import QQPilotPlugin

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
)


class FakePaths:
    def __init__(self, root: Path) -> None:
        self.data_dir = root / "data"
        self.runtime_dir = root / "runtime"


class FakeContext:
    def __init__(self, root: Path, gateway: "FakeGateway") -> None:
        self.paths = FakePaths(root)
        self.gateway = gateway
        self.logger = logging.getLogger("test.qqpilot")


class FakeGateway:
    """记录入站消息，并模拟 MaiBot 异步产生两段回复。"""

    def __init__(self, plugin: QQPilotPlugin, *, auto_reply: bool = True) -> None:
        self.plugin = plugin
        self.auto_reply = auto_reply
        self.ready = False
        self.messages: List[Dict[str, Any]] = []
        self._reply_tasks: List[asyncio.Task[Any]] = []
        self._reply_scheduled = False

    async def update_state(self, **kwargs: Any) -> bool:
        self.ready = bool(kwargs.get("ready"))
        return True

    async def route_message(self, **kwargs: Any) -> bool:
        message = kwargs["message"]
        self.messages.append(message)
        if self.auto_reply and not self._reply_scheduled:
            self._reply_scheduled = True
            self._reply_tasks.append(asyncio.create_task(self._reply_later(message)))
        return True

    async def _reply_later(self, inbound: Dict[str, Any]) -> None:
        await asyncio.sleep(0.05)
        message_info = inbound["message_info"]
        await self.plugin.send_to_platform(
            {
                "message_info": message_info,
                "raw_message": [{"type": "text", "data": {"text": "你好呀"}}],
            }
        )
        await asyncio.sleep(0.1)
        await self.plugin.send_to_platform(
            {
                "message_info": message_info,
                "raw_message": [
                    {"type": "text", "data": {"text": "第二条"}},
                    {
                        "type": "image",
                        "data": "",
                        "binary_data_base64": PNG_1X1,
                    },
                ],
            }
        )


def build_plugin(
    tmp_path: Path,
    *,
    reply_timeout: float = 2.0,
    idle_timeout: float = 0.5,
    auth_token: str = "",
    auto_reply: bool = True,
) -> tuple[QQPilotPlugin, FakeGateway]:
    """构造插件、假上下文与假网关。"""

    plugin = QQPilotPlugin()
    gateway = FakeGateway(plugin, auto_reply=auto_reply)
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "account_id": "test", "config_version": "2.1.0"},
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "auth_token": auth_token,
                "max_body_mb": 8,
            },
            "reply": {"total_timeout_sec": reply_timeout, "idle_timeout_sec": idle_timeout},
            "image": {"max_reply_images": 4},
        }
    )
    plugin._set_context(FakeContext(tmp_path, gateway))  # type: ignore[arg-type]
    return plugin, gateway


def _post_json(port: int, payload: Dict[str, Any], *, timeout: float = 5.0) -> tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get_json(port: int, path: str) -> tuple[int, Dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5.0) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _group_payload(timestamp: str = "2026-10-17 10:00") -> Dict[str, Any]:
    return {
        "model": "qqpilot-mai",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"[time] {timestamp} [username] Alice [content] 大家好\n"
                    f"[time] {timestamp} [username] Bob [content] 你好"
                ),
            }
        ],
    }


def test_http_gateway_returns_text_and_images(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin, gateway = build_plugin(tmp_path)
        await plugin.on_load()
        try:
            port = plugin._bound_port
            status, body = await asyncio.to_thread(_post_json, port, _group_payload())

            assert status == 200
            assert body["choices"][0]["message"]["content"] == "你好呀[[NEXT]]第二条"
            images = body["choices"][0]["message"]["images"]
            assert len(images) == 1
            assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")

            assert len(gateway.messages) == 2
            first_message = gateway.messages[0]
            assert first_message["message_info"]["group_info"]["group_id"].startswith("qqpilot_group_")
            assert first_message["raw_message"][0]["data"]["text"] == "大家好"

            status, health = await asyncio.to_thread(_get_json, port, "/health")
            assert status == 200
            assert health["status"] == "ok"
            assert health["listen"]["port"] == port

            # 同一份上下文再次请求：全部消息已去重，立即返回空回复。
            started = time.monotonic()
            status, duplicate = await asyncio.to_thread(_post_json, port, _group_payload())
            elapsed = time.monotonic() - started
            assert status == 200
            assert duplicate["choices"][0]["message"]["content"] == ""
            assert elapsed < 1.0
            assert len(gateway.messages) == 2
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_http_gateway_private_conversation_mapping(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin, gateway = build_plugin(tmp_path, idle_timeout=0.5)
        await plugin.on_load()
        try:
            payload = {
                "model": "qqpilot-mai",
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "[time] 2026-10-17 10:00 [username] Alice [content] 在吗",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{PNG_1X1}"},
                            },
                        ],
                    }
                ],
            }
            status, body = await asyncio.to_thread(_post_json, plugin._bound_port, payload)

            assert status == 200
            assert body["choices"][0]["message"]["content"] == "你好呀[[NEXT]]第二条"
            assert "group_info" not in gateway.messages[0]["message_info"]
            assert gateway.messages[0]["message_info"]["user_info"]["user_nickname"] == "Alice"
            segment_types = [segment["type"] for segment in gateway.messages[0]["raw_message"]]
            assert segment_types == ["text", "emoji"]
            assert gateway.messages[0]["is_emoji"] is True
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_http_bearer_token_is_enforced(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin, _ = build_plugin(tmp_path, auth_token="secret")
        await plugin.on_load()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{plugin._bound_port}/v1/chat/completions",
                data=json.dumps(_group_payload()).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(request, timeout=5.0)
            assert exc_info.value.code == 401
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_client_disconnect_stops_waiting(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin, gateway = build_plugin(
            tmp_path,
            reply_timeout=10.0,
            idle_timeout=5.0,
            auto_reply=False,
        )
        await plugin.on_load()
        try:
            body = json.dumps(_group_payload()).encode("utf-8")
            connection = http.client.HTTPConnection("127.0.0.1", plugin._bound_port, timeout=2.0)
            connection.request(
                "POST",
                "/v1/chat/completions",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            # 不读取响应直接断开，模拟 QQPilot 客户端超时或用户关闭窗口。
            connection.close()
            await asyncio.sleep(1.2)

            _, health = await asyncio.to_thread(_get_json, plugin._bound_port, "/health")
            assert health["active_requests"] == 0
            assert len(gateway.messages) == 2
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_config_update_disables_and_reenables_server(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin, _ = build_plugin(tmp_path, auto_reply=False)
        await plugin.on_load()
        try:
            assert plugin.is_ready

            disabled = plugin.get_plugin_config_data()
            disabled["plugin"]["enabled"] = False
            await plugin.on_config_update("self", disabled, "2.1.0")

            assert not plugin.is_ready
            assert plugin._httpd is None

            enabled = plugin.get_plugin_config_data()
            enabled["plugin"]["enabled"] = True
            enabled["server"]["port"] = 0
            await plugin.on_config_update("self", enabled, "2.1.0")

            assert plugin.is_ready
            assert plugin._httpd is not None
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())
