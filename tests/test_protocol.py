"""协议解析与响应构造的单元测试。"""

from datetime import datetime

import pytest

from QQPilotAdapter.protocol import (
    RequestError,
    build_completion,
    build_stream_chunks,
    encode_sse,
    message_fingerprint,
    parse_chat_request,
    parse_message_entries,
    parse_timestamp,
    stable_user_id,
)

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
)


def test_parse_qqpilot_multiline_format() -> None:
    text = (
        "[time]\n2023-10-17 10:00 \n\n [username] \n Alice \n\n [content] \n 你好，请问在吗？\n"
        "[time]\n2023-10-17 10:01 \n\n [username] \n Bob \n\n [content] \n 在的，有什么可以帮你？\n"
    )

    entries = parse_message_entries(text)

    assert [entry[1] for entry in entries] == ["Alice", "Bob"]
    assert entries[0][2] == "你好，请问在吗？"
    assert entries[1][2] == "在的，有什么可以帮你？"


def test_parse_qqpilot_inline_format() -> None:
    entries = parse_message_entries(
        "[time] 2023-10-17 10:00 [username] Alice [content] 你好\n"
        "[time] 2023-10-17 10:01 [username] Bob [content] 在的"
    )

    assert len(entries) == 2
    assert entries[0][0] == "2023-10-17 10:00"
    assert entries[1][2] == "在的"


def test_parse_timestamp_formats() -> None:
    now = datetime(2026, 10, 17, 16, 0, 0)

    assert parse_timestamp("2023-10-17 10:00", now=now) == datetime(2023, 10, 17, 10, 0).timestamp()
    assert parse_timestamp("10-17 16:00:00", now=now) == datetime(2026, 10, 17, 16, 0).timestamp()
    assert parse_timestamp("10月17日 16:00", now=now) == datetime(2026, 10, 17, 16, 0).timestamp()
    assert parse_timestamp("", now=now) > 0


def test_parse_chat_request_multimodal_content() -> None:
    image_url = f"data:image/png;base64,{PNG_1X1}"
    body = {
        "model": "qqpilot-mai",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[time] 2023-10-17 10:00 [username] Alice [content] 看这张图",
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }

    parsed = parse_chat_request(body, now=datetime(2026, 10, 17, 16, 0, 0))

    assert parsed.model == "qqpilot-mai"
    assert len(parsed.messages) == 1
    assert parsed.messages[0].images == [image_url]
    assert parsed.messages[0].content == "看这张图"


def test_parse_chat_request_rejects_missing_messages() -> None:
    with pytest.raises(RequestError) as exc_info:
        parse_chat_request({"model": "m", "messages": []})

    assert exc_info.value.status_code == 400


def test_stable_ids_and_fingerprints() -> None:
    assert stable_user_id("Alice") == stable_user_id(" alice ")
    assert stable_user_id("Alice") != stable_user_id("Bob")

    first = message_fingerprint("2023-10-17 10:00", "Alice", "你好")
    second = message_fingerprint("2023-10-17 10:00", "Alice", "你好")
    with_image = message_fingerprint("2023-10-17 10:00", "Alice", "你好", images=["data:image/png;base64,AA"])

    assert first == second
    assert first != with_image


def test_build_completion_with_images() -> None:
    image_url = f"data:image/png;base64,{PNG_1X1}"

    completion = build_completion("m", "回复", [image_url])

    message = completion["choices"][0]["message"]
    assert message["content"] == "回复"
    assert message["images"][0]["image_url"]["url"] == image_url


def test_stream_chunks_end_with_done() -> None:
    chunks = build_stream_chunks(build_completion("m", "回复", []))

    text = encode_sse(chunks).decode("utf-8")

    assert text.rstrip().endswith("data: [DONE]")
    assert "chat.completion.chunk" in text
