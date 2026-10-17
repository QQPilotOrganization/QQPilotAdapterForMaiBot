"""QQPilot 与 OpenAI ChatCompletion 协议之间的纯数据处理。

本模块不依赖 MaiBot SDK，便于单元测试。它负责：
- 解析 QQPilot 塞进 user 消息里的 ``[time] [username] [content]`` 文本；
- 从 OpenAI 多模态 content 数组中提取文本与图片；
- 生成稳定的消息指纹、用户 ID 与时间戳；
- 构造 OpenAI 兼容的响应与流式响应。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import hashlib
import json
import re
import time
import uuid

PLATFORM = "QQPilot"
REPLY_SEPARATOR = "[[NEXT]]"

# QQPilot 的 ChatContent.to_string() 会插入换行，因此标签之间必须允许任意空白。
_MESSAGE_PATTERN = re.compile(
    r"\[\s*time\s*\]\s*(?P<time>.*?)\s*"
    r"\[\s*username\s*\]\s*(?P<username>.*?)\s*"
    r"\[\s*content\s*\]\s*(?P<content>.*?)"
    r"(?=\s*\[\s*time\s*\]|$)",
    re.DOTALL | re.IGNORECASE,
)

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%m-%d %H:%M:%S",
    "%m-%d %H:%M",
    "%m月-%d日 %H:%M:%S",
    "%m月%d日 %H:%M:%S",
    "%m月-%d日 %H:%M",
    "%m月%d日 %H:%M",
    "%H:%M:%S",
    "%H:%M",
)


class RequestError(ValueError):
    """可以直接映射为 HTTP 错误的请求异常。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class InboundMessage:
    """一条已经解析完成的 QQPilot 入站消息。"""

    timestamp_text: str
    username: str
    content: str
    timestamp_seconds: float
    images: List[str] = field(default_factory=list)
    emojis: List[str] = field(default_factory=list)
    fingerprint: str = ""
    user_id: str = ""

    @property
    def display_username(self) -> str:
        """返回用于展示的用户名。"""

        return self.username.strip() or "未知用户"

    @property
    def plain_text(self) -> str:
        """返回可送入 Host 的纯文本，纯图片消息使用占位文本。"""

        text = self.content.strip()
        if text:
            return text
        if self.images or self.emojis:
            return "[图片]"
        return ""


@dataclass
class ParsedChatRequest:
    """QQPilot ChatCompletion 请求的解析结果。"""

    model: str
    stream: bool
    messages: List[InboundMessage]


def stable_user_id(username: str) -> str:
    """根据用户名生成跨进程稳定的用户 ID。"""

    normalized = (username or "").strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"qqpilot_user_{digest}"


def message_fingerprint(
    timestamp_text: str,
    username: str,
    content: str,
    images: Sequence[str] = (),
    emojis: Sequence[str] = (),
) -> str:
    """生成跨请求稳定的消息指纹，用于去重。"""

    media = [*images, *emojis]
    raw = "\x1f".join(
        [
            (timestamp_text or "").strip(),
            (username or "").strip(),
            (content or "").strip(),
            *media,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_timestamp(timestamp_text: str, *, now: Optional[datetime] = None) -> float:
    """把 QQPilot 的多种时间格式解析成 Unix 秒。

    无法解析时返回当前时间，保证 Host 侧不会因为时间格式异常而丢消息。
    """

    text = (timestamp_text or "").strip()
    if not text:
        return time.time()

    current = now or datetime.now()
    normalized = text.replace("年", "-").replace("月", "-").replace("日", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    candidates = [normalized]
    # 兼容 ISO 8601（带 T、时区）以及末尾的 Z。
    candidates.append(normalized.replace("Z", "+00:00"))

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.year == 1900:
            parsed = parsed.replace(year=current.year)
        return parsed.timestamp()

    cleaned = normalized
    for fmt in TIME_FORMATS:
        try:
            if fmt in ("%H:%M:%S", "%H:%M"):
                parsed = datetime.strptime(f"{current:%Y-%m-%d} {cleaned}", f"%Y-%m-%d {fmt}")
            elif "%Y" not in fmt:
                parsed = datetime.strptime(f"{current.year}-{cleaned}", f"%Y-{fmt}")
            else:
                parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return parsed.timestamp()

    return time.time()


def parse_message_entries(text: str, *, now: Optional[datetime] = None) -> List[Tuple[str, str, str, float]]:
    """从一段文本中提取所有 ``[time] [username] [content]`` 条目。"""

    if not text:
        return []

    entries: List[Tuple[str, str, str, float]] = []
    for match in _MESSAGE_PATTERN.finditer(text):
        timestamp_text = match.group("time").strip()
        username = match.group("username").strip()
        content = match.group("content").strip()
        if not username:
            # 用户名是 Host 消息模型必填字段，缺失条目直接跳过。
            continue
        entries.append((timestamp_text, username, content, parse_timestamp(timestamp_text, now=now)))
    return entries


def _extract_content_parts(content: Any) -> Tuple[List[str], List[str]]:
    """拆分 OpenAI content 字段中的文本段和图片 URL。"""

    if isinstance(content, str):
        return [content], []

    texts: List[str] = []
    images: List[str] = []
    if not isinstance(content, list):
        return texts, images

    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text":
            texts.append(str(part.get("text") or ""))
            continue
        if part_type != "image_url":
            continue
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "").strip()
        else:
            url = str(image_url or "").strip()
        if url:
            images.append(url)
    return texts, images


def parse_chat_request(body: Dict[str, Any], *, now: Optional[datetime] = None) -> ParsedChatRequest:
    """解析 QQPilot 的 ``/v1/chat/completions`` 请求体。"""

    if not isinstance(body, dict):
        raise RequestError(400, "请求体必须是 JSON 对象")
    model = str(body.get("model") or "").strip()
    if not model:
        raise RequestError(400, "缺少 model 字段")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise RequestError(400, "缺少非空的 messages 字段")

    messages: List[InboundMessage] = []
    seen: set[str] = set()

    for raw_message in raw_messages:
        if not isinstance(raw_message, dict) or str(raw_message.get("role") or "").strip() != "user":
            continue
        texts, images = _extract_content_parts(raw_message.get("content"))

        records: List[InboundMessage] = []
        for text in texts:
            for timestamp_text, username, content, timestamp_seconds in parse_message_entries(text, now=now):
                record = InboundMessage(
                    timestamp_text=timestamp_text,
                    username=username,
                    content=content,
                    timestamp_seconds=timestamp_seconds,
                    images=list(images),
                    emojis=[],
                )
                # 同一条 user message 的图片属于最后一个文本条目。
                if records:
                    records[-1].images = []
                records.append(record)

        for record in records:
            fingerprint = message_fingerprint(
                record.timestamp_text,
                record.username,
                record.content,
                record.images,
                record.emojis,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            record.fingerprint = fingerprint
            record.user_id = stable_user_id(record.username)
            messages.append(record)

    if not messages:
        raise RequestError(400, "messages 中没有可识别的 [time] [username] [content] 聊天记录")

    return ParsedChatRequest(
        model=model,
        stream=bool(body.get("stream", False)),
        messages=messages,
    )


def build_empty_completion(model: str, *, created: Optional[int] = None) -> Dict[str, Any]:
    """构造内容为空但结构合法的 ChatCompletion 响应。"""

    return build_completion(model, "", [], created=created)


def build_completion(
    model: str,
    text: str,
    images: Sequence[str],
    *,
    created: Optional[int] = None,
    completion_id: Optional[str] = None,
) -> Dict[str, Any]:
    """构造非流式的 ChatCompletion 响应。"""

    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if images:
        message["images"] = [
            {"type": "image_url", "image_url": {"url": image_url}}
            for image_url in images
        ]

    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": model,
        "system_fingerprint": "fp_qqpilot_mai",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(text),
            "total_tokens": len(text),
        },
    }


def build_stream_chunks(completion: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把完整响应拆成足够兼容的 SSE chunk 列表。"""

    completion_id = str(completion.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}")
    created = int(completion.get("created") or time.time())
    model = str(completion.get("model") or "")
    message = completion.get("choices", [{}])[0].get("message", {})
    content = str(message.get("content") or "")
    images = message.get("images") or []

    chunks: List[Dict[str, Any]] = [
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content, **({"images": images} if images else {})},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        },
    ]
    return chunks


def encode_sse(chunks: Sequence[Dict[str, Any]]) -> bytes:
    """把 chunk 列表编码为 SSE 响应体。"""

    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")
