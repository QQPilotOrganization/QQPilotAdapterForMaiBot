"""会话归类与去重存储测试。"""

from pathlib import Path
from typing import Sequence

from QQPilotAdapter.conversation_store import ConversationStore
from QQPilotAdapter.protocol import (
    InboundMessage,
    message_fingerprint,
    parse_timestamp,
    stable_user_id,
)


def make_message(
    username: str,
    content: str,
    timestamp_text: str = "2026-10-17 10:00",
    images: Sequence[str] = (),
) -> InboundMessage:
    """构造一条测试消息。"""

    media = list(images)
    return InboundMessage(
        timestamp_text=timestamp_text,
        username=username,
        content=content,
        timestamp_seconds=parse_timestamp(timestamp_text),
        images=media,
        emojis=[],
        fingerprint=message_fingerprint(timestamp_text, username, content, media),
        user_id=stable_user_id(username),
    )


def make_store(tmp_path: Path) -> ConversationStore:
    store = ConversationStore(tmp_path / "qqpilot.sqlite3")
    store.initialize()
    return store


def test_private_conversation_is_stable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.resolve([make_message("Alice", "你好")])
    second = store.resolve([make_message("Alice", "再见")])

    assert first.kind == "private"
    assert first.conversation_id == second.conversation_id
    assert store.private_conversation_for_user(stable_user_id("Alice")) == first.conversation_id


def test_group_reuses_conversation_by_message_overlap(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    history = [
        make_message("Alice", "大家好", "2026-10-17 10:00"),
        make_message("Bob", "你好", "2026-10-17 10:01"),
    ]

    conversation = store.resolve(history)
    store.record_messages(conversation.conversation_id, history)

    next_messages = [
        *history,
        make_message("Alice", "今天吃什么", "2026-10-17 10:02"),
    ]
    reused = store.resolve(next_messages)

    assert conversation.kind == "group"
    assert reused.conversation_id == conversation.conversation_id


def test_group_creates_new_conversation_for_new_participants(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.resolve(
        [
            make_message("Alice", "大家好", "2026-10-17 10:00"),
            make_message("Bob", "你好", "2026-10-17 10:01"),
        ]
    )
    store.record_messages(
        first.conversation_id,
        [
            make_message("Alice", "大家好", "2026-10-17 10:00"),
            make_message("Bob", "你好", "2026-10-17 10:01"),
        ],
    )

    second = store.resolve(
        [
            make_message("Charlie", "另一个群", "2026-10-17 11:00"),
            make_message("Dave", "确实", "2026-10-17 11:01"),
        ]
    )

    assert first.conversation_id != second.conversation_id


def test_known_fingerprints_dedupes_per_conversation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    message = make_message("Alice", "你好")
    conversation = store.resolve([make_message("Alice", "上下文"), make_message("Bob", "在")])
    store.record_messages(conversation.conversation_id, [message])

    assert message.fingerprint in store.known_fingerprints(
        conversation.conversation_id,
        [message.fingerprint],
    )
    assert not store.known_fingerprints("another-conversation", [message.fingerprint])


def test_global_fingerprints_survive_conversation_changes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    message = make_message("Alice", "你好")
    conversation = store.resolve([make_message("Alice", "上下文"), make_message("Bob", "在")])
    store.record_messages(conversation.conversation_id, [message])

    assert message.fingerprint in store.known_fingerprints_global([message.fingerprint])
