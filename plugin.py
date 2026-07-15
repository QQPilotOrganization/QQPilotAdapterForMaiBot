import threading
import os
import base64
from typing import Any, List, ClassVar
import socketserver
from concurrent.futures import Future
from datetime import datetime
import hashlib
from maibot_sdk import MaiBotPlugin, MessageGateway, Field, PluginConfigBase
import asyncio
import time

from plugins.QQPilotAdapter.GroupChatManager import GroupChatManager, buildText
from plugins.QQPilotAdapter.isEmoji import base64ToImage, isEmoji
# from plugins.QQPilotAdapter.plugin import gateway, groupChatManager, prin

import asyncio
import http.server
import json
import re
import time
from typing import List
from uuid import uuid4

from colorama import Fore

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'QQPilot Plugin For MaiBot')
    def _send_json(self, status_code: int, data: dict):
        """统一发送 JSON 响应"""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        global print
        print=gateway.ctx.logger.info
        warn=gateway.ctx.logger.warning
        error=gateway.ctx.logger.error
        """处理 /v1/chat/completions POST 请求"""
        if self.path != '/v1/chat/completions':
            self._send_json(404, {"error": f"Not Found: {self.path}"})
            return

        # 读取并解析请求体
        content_length = int(self.headers.get('Content-Length', 0))
        try:
            raw_body = self.rfile.read(content_length)
            body = json.loads(raw_body.decode('utf-8'))
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON: {str(e)}"})
            return

        # 调试输出
        try:

            print("\n" + "=" * 70)
            print("收到请求")
            print("=" * 70)
            print("Headers:")
            for k, v in self.headers.items():
                print(f"  {k}: {v}")
            print("\nRequest Body (JSON):")
            rbody=json.dumps(body, indent=2, ensure_ascii=False)
            print(rbody[:50]+('...' if len(rbody)>=50 else '') )
            print("=" * 70)
        except:
            pass

        PATTERN = re.compile(
            r'\[\s*time\s*\]\s*(.*?)\s*\[\s*username\s*\]\s*(.*?)\s*\[\s*content\s*\]\s*(.*?)\s*(?=\[\s*time\s*\]|$)',
            re.DOTALL
        )

        if not body or 'model' not in body:
            self._send_json(400, {"error": "Missing 'model' in request body"})
            return
        assert(isinstance(body,dict))
        ctr:List=body.get('messages') # type: ignore
        reply_text = "......"

        try:
            if gateway._delete_image_in_next_request and gateway._last_saved_images:
                deleted_count = 0
                for img_path in gateway._last_saved_images:
                    try:
                        os.remove(img_path)
                        deleted_count += 1
                        print(f"🗑️ 删除上一次回复的图片: {img_path}")
                    except Exception as e:
                        print(f"⚠️ 删除图片失败: {img_path}, 错误: {e}")
                gateway._last_saved_images.clear()
                print(f"✅ 已删除 {deleted_count} 张图片")
            
            chatlist=[]
            messages_to_send=[]
            all_usernames = set()
            containsNewMassage=False
            for i in ctr:
                if i['role'] != 'user':
                    continue

                content_field = i["content"]
                text_content = ""
                image_urls = []
                emoji_urls = []

                if isinstance(content_field, list):
                    for item in content_field:
                        if isinstance(item, dict):
                            item_type = item.get("type", "")
                            if item_type == "text":
                                text_content = item.get("text", "")
                            elif item_type == "image_url":
                                image_url = item.get("image_url", {})
                                if isinstance(image_url, dict):
                                    url = image_url.get("url", "")
                                    if url.startswith("data:image"):
                                        # 解析 base64 图片数据
                                        base64_data = url.split(",", 1)[1]
                                        try:
                                            # 使用 isEmoji 模块判断是否为表情包
                                            img = base64ToImage(base64_data)
                                            if isEmoji(img):
                                                emoji_urls.append(url)
                                            else:
                                                image_urls.append(url)
                                        except Exception as e:
                                            # 解析失败时默认当作普通图片处理
                                            image_urls.append(url)
                elif isinstance(content_field, str):
                    text_content = content_field

                match = PATTERN.search(text_content)

                if match:
                    timestamp_str = match.group(1).strip()
                    username = match.group(2).strip()
                    content = match.group(3).strip()

                    print(f"✅ time: {timestamp_str}")
                    print(f"✅ username: {username}")
                    print(f"✅ content: {content}")
                    print(f"✅ images: {len(image_urls)}, emojis: {len(emoji_urls)}")
                    chatlist.append([username,content])
                    all_usernames.add(username)
                    if not groupChatManager.message_exists(buildText(username, content)):
                        messages_to_send.append({
                            "timestamp": timestamp_str,
                            "username": username,
                            "content": content,
                            "images": image_urls,
                            "emojis": emoji_urls
                        })
                        containsNewMassage=True
                    else:
                        warn(f"⚠️ 消息已存在，跳过: {username}:{content[:30]}...")

            group_id, group_name =groupChatManager.classify_group(chatlist)
            print(f'GroupID:{group_id},groupName={group_name}')
            if not containsNewMassage:
                response = {
                    "id": f"chatcmpl-{uuid4().hex[:8]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body["model"],
                    "system_fingerprint": "fp_mai_bot",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": ""
                            },
                            "finish_reason": "stop"
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens":0
                    }
                }
                self._send_json(500, response)
                return
            with gateway._reply_lock:
                gateway._replies = []

            is_private_chat = len(all_usernames) == 1
            
            print(f"🔍 检测到 {len(all_usernames)} 个不同用户（包括已存在的消息），私聊模式: {is_private_chat}")
            
            for msg in messages_to_send:
                timestamp_str = msg["timestamp"]
                username = msg["username"]
                content = msg["content"]
                images = msg.get("images", [])
                emojis = msg.get("emojis", [])

                print(f"✅ time: {timestamp_str}")
                print(f"✅ username: {username}")
                print(f"✅ content: {content}")
                print(f"✅ images: {len(images)}, emojis: {len(emojis)}")
                resultDict={}
                resultDict['message_id']=f"qqpilot-{uuid4().hex[:16]}"
                resultDict['user_id']=f"qqpilot_user_{hash(username) & 0xFFFFFFFFFFFFFFFF}"
                resultDict['nickname']=username
                resultDict['message']=content
                resultDict['timestamp']=timestamp_str
                resultDict['images']=images
                resultDict['emojis']=emojis
                
                if is_private_chat:
                    resultDict['groupID'] = ""
                    resultDict['groupName'] = ""
                    print(f"📩 私聊消息，不包含群信息")
                else:
                    resultDict['groupID']=group_id
                    resultDict['groupName']=group_name

                asyncio.run_coroutine_threadsafe(gateway.handle_inbound(resultDict), gateway._loop)

            new_messages = [[m["username"], m["content"]] for m in messages_to_send]
            groupChatManager.add_messages_to_group(group_id, new_messages)

            try:
                total_timeout = 180
                idle_timeout = 30
                last_count = -1
                wait_time = 0
                total_wait_time = 0

                while total_wait_time < total_timeout:
                    with gateway._reply_lock:
                        current_count = len(gateway._replies)

                    if current_count > last_count:
                        last_count = current_count
                        wait_time = 0
                        print(f"🔄 收到新回复，当前共 {current_count} 条")
                    else:
                        wait_time += 0.5

                    total_wait_time += 0.5
                    time.sleep(0.5)

                    with gateway._reply_lock:
                        if len(gateway._replies) > 0 and wait_time >= idle_timeout:
                            print(f"⏰ 连续 {idle_timeout} 秒无新回复，结束等待")
                            break

                with gateway._reply_lock:
                    if gateway._replies:
                        reply_text = "[[NEXT]]".join(gateway._replies)
                        print(f"✅ 收到 {len(gateway._replies)} 条回复，总等待时间: {total_wait_time:.1f}秒")
                        print(f"✅ 回复内容: {reply_text}")
                    else:
                        reply_text = ""

            except Exception as e:
                warn(f"⚠️ 获取回复失败: {e}")
                reply_text = ""

        except Exception as e:
            print(e)

        response = {
            "id": f"chatcmpl-{uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body["model"],
            "system_fingerprint": "fp_mai_bot",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": body.get("max_tokens", 2048),
                "completion_tokens": len(reply_text),
                "total_tokens": body.get("max_tokens", 2048) + len(reply_text)
            }
        }
        self._send_json(200, response)
        
        

ACCOUNT_ID="2134542354565646"
GATEWAY_NAME="qqpilot_gateway2"
PLATFORM="QQPilot"
httpd=None
print=None
warn=None
error=None
groupChatManager=GroupChatManager()

class QQPilotPluginOptions(PluginConfigBase):
    """插件级配置。"""
    __ui_label__ = "插件设置"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用 QQPilot 适配器。",
        json_schema_extra={
            "label": "启用适配器",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="2.0.0",
        description="当前配置结构版本。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "order": 99,
        },
    )

class QQPilotImageOptions(PluginConfigBase):
    """图片相关配置。"""
    __ui_label__ = "图片设置"
    __ui_order__ = 1

    ImagePath: str = Field(
        default="images",
        description="图片保存路径。",
        json_schema_extra={
            "label": "图片保存路径",
            "order": 0,
        },
    )
    
    deleteImageInTheNextRequest: bool = Field(
        default=False,
        description="是否在下一次请求时删除上一次回复生成的图片。",
        json_schema_extra={
            "label": "下一次请求时删除图片",
            "order": 1,
        },
    )

class Gateway(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase] | None] = None
    
    def __init__(self) -> None:
        super().__init__()
        self._image_path = "images"
        self._delete_image_in_next_request = False
        self._last_saved_images: List[str] = []
    
    async def on_load(self) -> None:
        # 上报网关就绪状态
        await self.ctx.gateway.update_state(
            gateway_name=GATEWAY_NAME,
            ready=True,
            platform=PLATFORM,
            account_id=ACCOUNT_ID,
            scope="primary",
            metadata={"protocol": "qqpilot"},
        )
        self._loop = asyncio.get_running_loop()
        self._replies: List[str] = []
        self._reply_lock = threading.Lock()
        self._last_reply_time = 0
        self.server_thread = threading.Thread(target=self.run_server, args=(7749,), daemon=True)
        self.server_thread.start()
        self.ctx.logger.info("就绪！")
        self.ctx.logger.info(f"{Fore.LIGHTYELLOW_EX}在设置的服务器那一栏填写http://localhost:7749/v1")

    
    async def on_unload(self) -> None:
        # 上报网关离线
        await self.ctx.gateway.update_state(
            gateway_name="qqpilot_gateway2",
            ready=False,
        )
        self.httpd.shutdown()
        self.server_thread.join()
        self.ctx.logger.info(f"{Fore.CYAN}离线。")

    async def __del__(self):
        await self.on_unload()
    def run_server(self,port=7749):
        """在子线程中启动服务器"""
        
        self.httpd:socketserver.TCPServer  = socketserver.TCPServer(("", port), Handler)
        # self.ctx.logger.info(f"Server started at http://localhost:{port}/")
        self.httpd.serve_forever()
    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if "image" in config_data:
            if "ImagePath" in config_data["image"]:
                self._image_path = config_data["image"]["ImagePath"]
                self.ctx.logger.info(f'图片保存路径已更新: {self._image_path}')
            if "deleteImageInTheNextRequest" in config_data["image"]:
                self._delete_image_in_next_request = config_data["image"]["deleteImageInTheNextRequest"]
                self.ctx.logger.info(f'下一次请求删除图片: {self._delete_image_in_next_request}')

    def Print(self,content):
        self.ctx.logger.info(content)
    @MessageGateway(
        route_type="duplex",
        name=GATEWAY_NAME,
        platform=PLATFORM,
        protocol="qqpilot",
        account_id=ACCOUNT_ID,
        scope="primary",
    )
    async def send_to_platform(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """出站：将 Host 消息转发到外部平台。"""
        # 调试日志：查看完整消息结构
        raw_message = message.get("raw_message", [])
        print(f"\n📤 send_to_platform 被调用")
        print(f"📤 raw_message 段数: {len(raw_message)}")
        for i, segment in enumerate(raw_message):
            seg_type = segment.get("type", "")
            if seg_type == "text":
                data = segment.get("data", "")
                text_content = ""
                if isinstance(data, str):
                    text_content = data
                elif isinstance(data, dict):
                    text_content = data.get("text", "")
                print(f"📤 段 {i}: type={seg_type}, 长度={len(text_content)}, 内容前50字='{text_content[:50]}...'")
            else:
                print(f"📤 段 {i}: type={seg_type}")
        
        reply_text = ""
        for segment in raw_message:
            segment_type = segment.get("type", "")
            
            if segment_type == "text":
                data = segment.get("data", {})
                if isinstance(data, str):
                    reply_text += data
                elif isinstance(data, dict):
                    reply_text += data.get("text", "")
            
            elif segment_type in ("emoji", "image"):
                base64_data = segment.get("binary_data_base64", "")
                if base64_data:
                    self._save_image(base64_data)
        
        print(f"📤 合并后回复文本长度: {len(reply_text)}")
        
        if reply_text:
            with self._reply_lock:
                self._replies.append(reply_text)
                self._last_reply_time = time.time()
        
        return {"success": True, "external_message_id": 1}
    
    def _save_image(self, base64_data: str) -> None:
        """将 base64 图片数据保存到配置指定的路径。"""
        image_path = self._image_path
        if not os.path.isabs(image_path):
            images_dir = os.path.join(os.path.dirname(__file__), image_path)
        else:
            images_dir = image_path
        os.makedirs(images_dir, exist_ok=True)
        
        try:
            image_data = base64.b64decode(base64_data)
            timestamp = int(time.time() * 1000)
            filename = f"{timestamp}_{uuid4().hex[:8]}.png"
            filepath = os.path.join(images_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(image_data)
            
            self._last_saved_images.append(filepath)
            self.ctx.logger.info(f"图片已保存: {filepath}")
        except Exception as e:
            self.ctx.logger.error(f"保存图片失败: {e}")

    async def handle_inbound(self, payload: dict[str, Any]) -> None:
        """入站：将外部平台消息注入 Host。"""
        msg_id = payload["message_id"]
        content = payload["message"]
        
        # 解析时间戳
        timestamp_str = payload.get("timestamp", "")
        if timestamp_str:
            try:
                timestamp_str = timestamp_str.strip()
                if "月" in timestamp_str and "日" in timestamp_str:
                    dt = datetime.strptime(timestamp_str, "%m月-%d日 %H:%M:%S")
                    current_year = datetime.now().year
                    dt = dt.replace(year=current_year)
                    timestamp_seconds = dt.timestamp()
                elif "+08:00" in timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace("+08:00", "+0800"))
                    timestamp_seconds = dt.timestamp()
                else:
                    dt = datetime.fromisoformat(timestamp_str)
                    timestamp_seconds = dt.timestamp()
            except:
                timestamp_seconds = time.time()
        else:
            timestamp_seconds = time.time()
        
        # 构建 raw_message 列表，包含文本和多媒体内容
        raw_message = [{"type": "text", "data": {"text": content}}]
        
        # 处理普通图片（非表情包）
        image_urls = payload.get("images", [])
        for image_url in image_urls:
            if image_url.startswith("data:image"):
                try:
                    base64_data = image_url.split(",", 1)[1]
                    binary_data = base64_data.encode("utf-8")
                    image_hash = hashlib.sha256(binary_data).hexdigest()
                    raw_message.append({
                        "type": "image",
                        "data": "",
                        "hash": image_hash,
                        "binary_data_base64": base64_data,
                    })
                except:
                    pass
        
        # 处理表情包图片
        emoji_urls = payload.get("emojis", [])
        for emoji_url in emoji_urls:
            if emoji_url.startswith("data:image"):
                try:
                    base64_data = emoji_url.split(",", 1)[1]
                    binary_data = base64_data.encode("utf-8")
                    emoji_hash = hashlib.sha256(binary_data).hexdigest()
                    raw_message.append({
                        "type": "emoji",
                        "data": "",
                        "hash": emoji_hash,
                        "binary_data_base64": base64_data,
                    })
                except:
                    pass
        
        # 判断消息类型标志
        is_emoji = len(emoji_urls) > 0
        is_picture = len(image_urls) > 0
        
        # 构建消息信息，根据是否为私聊决定是否包含群信息
        message_info = {
            "platform": PLATFORM,
            "message_id": msg_id,
            "time": int(timestamp_seconds),
            "user_info": {
                "user_id": payload["user_id"],
                "user_nickname": payload["nickname"],
            },
            "additional_config": {},
        }
        
        group_id = payload.get("groupID", "")
        group_name = payload.get("groupName", "")
        
        if group_id and group_name:
            message_info["group_info"] = {
                "group_id": group_id,
                "group_name": group_name,
            }
        
        accepted = await self.ctx.gateway.route_message(
            gateway_name=GATEWAY_NAME,
            message={
                "message_id": msg_id,
                "timestamp": str(float(timestamp_seconds)),
                "platform": PLATFORM,
                "message_info": message_info,
                "raw_message": raw_message,
                "processed_plain_text": content,
                "display_message": content,
                "is_mentioned": False,
                "is_at": False,
                "is_emoji": is_emoji,
                "is_picture": is_picture,
                "is_command": content.startswith("/"),
                "is_notify": False,
                "session_id": "",
            },
            route_metadata={
                "self_id": ACCOUNT_ID,
                "connection_id": "primary",
            },
            external_message_id=msg_id,
            dedupe_key=msg_id,
        )
        if not accepted:
            self.ctx.logger.warning(
                "Host 未接收入站消息: %s", msg_id
            )

gateway=Gateway()

def create_plugin():
    global gateway
    return gateway