"""会话归类与消息去重存储。

QQPilot 的 ChatCompletion 请求只包含聊天文本，没有 QQ 群号。这里用
“历史消息重叠度 + 参与者重合度”把同一段上下文归到同一个会话，并按
会话保存已处理消息，避免 QQPilot 每次携带完整上下文时重复注入。
"""

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import hashlib
import sqlite3
import threading
import time
import uuid

from .protocol import InboundMessage


@dataclass(frozen=True)
class Conversation:
    """Host 侧使用的一段聊天会话。"""

    conversation_id: str
    kind: str
    title: str


class ConversationStore:
    """基于 SQLite 的会话与消息去重存储。"""

    def __init__(
        self,
        db_path: Path,
        *,
        overlap_threshold: float = 0.1,
        member_overlap_threshold: float = 0.8,
        single_user_as_private: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.overlap_threshold = overlap_threshold
        self.member_overlap_threshold = member_overlap_threshold
        self.single_user_as_private = single_user_as_private
        self._lock = threading.RLock()

    def initialize(self) -> None:
        """创建数据库目录与表结构。"""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    conversation_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    timestamp_text TEXT NOT NULL,
                    username TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (conversation_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS conversation_users (
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY (conversation_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_fp
                    ON conversation_messages(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_conversation_users_user
                    ON conversation_users(user_id);
                CREATE INDEX IF NOT EXISTS idx_conversations_updated
                    ON conversations(kind, updated_at);
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def resolve(self, messages: Sequence[InboundMessage]) -> Conversation:
        """找出现有会话，找不到时创建新会话。"""

        if not messages:
            raise ValueError("messages 不能为空")

        usernames = {message.username.strip().casefold() for message in messages if message.username.strip()}
        if self.single_user_as_private and len(usernames) == 1:
            return self._ensure_private(messages[0])

        now = int(time.time())
        fingerprints = {message.fingerprint for message in messages}
        incoming_user_ids = {message.user_id for message in messages}
        total = max(len(fingerprints), 1)

        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, updated_at FROM conversations "
                "WHERE kind = 'group' AND updated_at >= ? ORDER BY updated_at DESC LIMIT 200",
                (now - 90 * 86400,),
            ).fetchall()

            best: Optional[Tuple[float, float, int, str, str]] = None
            for row in rows:
                conversation_id = str(row["conversation_id"])
                matched = self._count_matching(conn, conversation_id, fingerprints)
                matched_ratio = matched / total
                member_ratio = self._member_ratio(conn, conversation_id, incoming_user_ids)
                age = now - int(row["updated_at"])
                accepted = matched_ratio >= self.overlap_threshold or (
                    member_ratio >= self.member_overlap_threshold and age <= 7 * 86400
                )
                if not accepted:
                    continue
                score = (matched_ratio, member_ratio, int(row["updated_at"]), conversation_id, str(row["title"]))
                if best is None or score[:3] > best[:3]:
                    best = score

            if best is not None:
                conversation = Conversation(conversation_id=best[3], kind="group", title=best[4])
                self._touch(conn, conversation, messages, now)
                conn.commit()
                return conversation

            conversation_id = f"qqpilot_group_{uuid.uuid4().hex[:16]}"
            title = self._build_group_title(messages)
            conn.execute(
                "INSERT INTO conversations (conversation_id, kind, title, created_at, updated_at) "
                "VALUES (?, 'group', ?, ?, ?)",
                (conversation_id, title, now, now),
            )
            conversation = Conversation(conversation_id=conversation_id, kind="group", title=title)
            self._touch(conn, conversation, messages, now)
            conn.commit()
            return conversation

    def _ensure_private(self, message: InboundMessage) -> Conversation:
        """按用户名生成稳定的私聊会话。"""

        normalized = message.username.strip().casefold()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        conversation_id = f"qqpilot_private_{digest}"
        now = int(time.time())
        title = message.display_username

        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO conversations (conversation_id, kind, title, created_at, updated_at) "
                "VALUES (?, 'private', ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at",
                (conversation_id, title, now, now),
            )
            conn.execute(
                "INSERT INTO conversation_users (conversation_id, user_id, username, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(conversation_id, user_id) DO UPDATE SET "
                "username = excluded.username, last_seen = excluded.last_seen",
                (conversation_id, message.user_id, message.display_username, now),
            )
            conn.commit()
        return Conversation(conversation_id=conversation_id, kind="private", title=title)

    @staticmethod
    def _build_group_title(messages: Sequence[InboundMessage]) -> str:
        names: List[str] = []
        for message in messages:
            name = message.display_username
            if name not in names:
                names.append(name)
        if not names:
            return "QQPilot 群聊"
        visible = "、".join(names[:3])
        if len(names) > 3:
            return f"{visible} 等{len(names)}人"
        return visible

    @staticmethod
    def _count_matching(conn: sqlite3.Connection, conversation_id: str, fingerprints: Set[str]) -> int:
        if not fingerprints:
            return 0
        placeholders = ",".join("?" for _ in fingerprints)
        row = conn.execute(
            f"SELECT COUNT(*) AS matched FROM conversation_messages "
            f"WHERE conversation_id = ? AND fingerprint IN ({placeholders})",
            (conversation_id, *fingerprints),
        ).fetchone()
        return int(row["matched"] if row is not None else 0)

    @staticmethod
    def _member_ratio(conn: sqlite3.Connection, conversation_id: str, user_ids: Set[str]) -> float:
        if not user_ids:
            return 0.0
        rows = conn.execute(
            "SELECT user_id FROM conversation_users WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        existing = {str(row["user_id"]) for row in rows}
        return len(existing & user_ids) / len(user_ids)

    def _touch(
        self,
        conn: sqlite3.Connection,
        conversation: Conversation,
        messages: Sequence[InboundMessage],
        now: int,
    ) -> None:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
            (now, conversation.conversation_id),
        )
        for message in messages:
            conn.execute(
                "INSERT INTO conversation_users (conversation_id, user_id, username, last_seen) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(conversation_id, user_id) DO UPDATE SET "
                "username = excluded.username, last_seen = excluded.last_seen",
                (conversation.conversation_id, message.user_id, message.display_username, now),
            )

    def known_fingerprints(self, conversation_id: str, fingerprints: Iterable[str]) -> Set[str]:
        """返回指定会话中已经处理过的指纹集合。"""

        values = list(fingerprints)
        if not values:
            return set()
        known: Set[str] = set()
        with self._lock, closing(self._connect()) as conn:
            for start in range(0, len(values), 500):
                chunk = values[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT fingerprint FROM conversation_messages "
                    f"WHERE conversation_id = ? AND fingerprint IN ({placeholders})",
                    (conversation_id, *chunk),
                ).fetchall()
                known.update(str(row["fingerprint"]) for row in rows)
        return known

    def known_fingerprints_global(self, fingerprints: Iterable[str]) -> Set[str]:
        """跨会话查询已处理指纹，避免会话归类变化时重放历史消息。"""

        values = list(fingerprints)
        if not values:
            return set()
        known: Set[str] = set()
        with self._lock, closing(self._connect()) as conn:
            for start in range(0, len(values), 500):
                chunk = values[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT DISTINCT fingerprint FROM conversation_messages "
                    f"WHERE fingerprint IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                known.update(str(row["fingerprint"]) for row in rows)
        return known

    def record_messages(self, conversation_id: str, messages: Sequence[InboundMessage]) -> None:
        """记录已经成功注入 Host 的消息。"""

        if not messages:
            return
        now = int(time.time())
        with self._lock, closing(self._connect()) as conn:
            for message in messages:
                conn.execute(
                    "INSERT OR IGNORE INTO conversation_messages "
                    "(conversation_id, fingerprint, message_id, timestamp_text, username, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        message.fingerprint,
                        f"qqpilot-{message.fingerprint[:24]}",
                        message.timestamp_text,
                        message.username,
                        message.content,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO conversation_users (conversation_id, user_id, username, last_seen) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(conversation_id, user_id) DO UPDATE SET "
                    "username = excluded.username, last_seen = excluded.last_seen",
                    (conversation_id, message.user_id, message.display_username, now),
                )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
            conn.commit()

    def private_conversation_for_user(self, user_id: str) -> Optional[str]:
        """根据 Host 用户 ID 反查私聊会话 ID。"""

        if not user_id:
            return None
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT cu.conversation_id AS conversation_id "
                "FROM conversation_users cu "
                "JOIN conversations c ON c.conversation_id = cu.conversation_id "
                "WHERE cu.user_id = ? AND c.kind = 'private' "
                "ORDER BY cu.last_seen DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return str(row["conversation_id"]) if row is not None else None

    def get(self, conversation_id: str) -> Optional[Conversation]:
        """读取会话信息。"""

        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT conversation_id, kind, title FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return Conversation(
            conversation_id=str(row["conversation_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
        )

    def prune(self, retention_days: int) -> int:
        """删除超过保留期的会话及其消息，返回删除的会话数。"""

        if retention_days <= 0:
            return 0
        cutoff = int(time.time()) - retention_days * 86400
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT conversation_id FROM conversations WHERE updated_at < ?",
                (cutoff,),
            ).fetchall()
            conversation_ids = [str(row["conversation_id"]) for row in rows]
            for conversation_id in conversation_ids:
                conn.execute(
                    "DELETE FROM conversation_messages WHERE conversation_id = ?",
                    (conversation_id,),
                )
                conn.execute(
                    "DELETE FROM conversation_users WHERE conversation_id = ?",
                    (conversation_id,),
                )
                conn.execute(
                    "DELETE FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                )
            conn.commit()
        return len(conversation_ids)

    def stats(self) -> Dict[str, int]:
        """返回少量可用于健康检查的统计信息。"""

        with self._lock, closing(self._connect()) as conn:
            conversations = conn.execute("SELECT COUNT(*) AS count FROM conversations").fetchone()
            messages = conn.execute("SELECT COUNT(*) AS count FROM conversation_messages").fetchone()
        return {
            "conversations": int(conversations["count"]) if conversations is not None else 0,
            "messages": int(messages["count"]) if messages is not None else 0,
        }

    def close(self) -> None:
        """预留的关闭钩子；当前每次操作都使用独立连接。"""
