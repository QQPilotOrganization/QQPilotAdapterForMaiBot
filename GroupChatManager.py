import sqlite3
import hashlib
import os
import threading
import time
from typing import Any, List, Tuple, Optional, Dict
def buildText(username,content):
    return f"{username}:{content}"

class GroupChatManager:
    def __init__(self, db_path: str = None): # type: ignore
        if db_path is None:
            db_path = os.path.join(os.path.dirname(__file__), "groupChat.db")
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS group_chats (
                        group_id TEXT PRIMARY KEY,
                        group_name TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        keywords TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS group_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        FOREIGN KEY (group_id) REFERENCES group_chats(group_id)
                    )
                """)
                conn.commit()
                

    def _generate_group_id(self) -> str:
        timestamp = str(int(time.time() * 1000))
        return f"qqpilot_group_{hashlib.md5(timestamp.encode()).hexdigest()[:16]}"

    def _extract_keywords(self, content: str) -> List[str]:
        words = content.replace("，", " ").replace("。", " ").replace("！", " ").replace("？", " ").split()
        return [word for word in words if len(word) >= 2]

    def _get_group_keywords(self, group_id: str) -> List[str]:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT keywords FROM group_chats WHERE group_id = ?", (group_id,))
                result = cursor.fetchone()
                if result:
                    return result[0].split(",") if result[0] else []
                return []

    def _calculate_match_score(self, message_keywords: List[str], group_keywords: List[str]) -> float:
        if not message_keywords or not group_keywords:
            return 0.0
        matched = len(set(message_keywords) & set(group_keywords))
        return matched / len(message_keywords)

    def _update_group_keywords(self, group_id: str, new_keywords: List[str]) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT keywords FROM group_chats WHERE group_id = ?", (group_id,))
                result = cursor.fetchone()
                if result:
                    existing = result[0].split(",") if result[0] else []
                    all_keywords = list(set(existing + new_keywords))
                else:
                    all_keywords = new_keywords
                cursor.execute("UPDATE group_chats SET keywords = ? WHERE group_id = ?",
                               (",".join(all_keywords), group_id))
                conn.commit()

    def _add_message(self, group_id: str, content: str) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO group_messages (group_id, content) VALUES (?, ?)",
                               (group_id, content))
                conn.commit()

    def _get_all_groups(self) -> List[Tuple[str, str]]:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT group_id, group_name FROM group_chats")
                return cursor.fetchall()

    def classify_group(self, messages: List[List[str]], threshold: float = 0.2) -> Tuple[str, str]:
        if not messages:
            return self.create_new_group()

        all_keywords: List[str] = []
        for username, content in messages:
            all_keywords.extend(self._extract_keywords(buildText(username,content)))

        if not all_keywords:
            return self.create_new_group()

        groups = self._get_all_groups()
        if not groups:
            return self.create_new_group()

        best_group_id = None
        best_score = 0.0
        best_group_name = ""

        for group_id, group_name in groups:
            group_keywords = self._get_group_keywords(group_id)
            score = self._calculate_match_score(all_keywords, group_keywords)
            if score > best_score:
                best_score = score
                best_group_id = group_id
                best_group_name = group_name

        if best_score >= threshold:
            return best_group_id, best_group_name
        return self.create_new_group()

    def add_messages_to_group(self, group_id: str, messages: List[List[str]]) -> None:
        for username, content in messages:
            text = buildText(username, content)
            if not self.message_exists(text):
                self._add_message(group_id, text)
                self._update_group_keywords(group_id, self._extract_keywords(text))

    def create_new_group(self, group_name: str = "") -> Tuple[str, str]:
        """创建新群组，元组为（组ID，组名）"""
        group_id = self._generate_group_id()
        if not group_name:
            group_name = f"QQPilot Group {group_id[-8:]}"
        created_at = int(time.time())

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO group_chats (group_id, group_name, created_at, keywords) VALUES (?, ?, ?, ?)",
                               (group_id, group_name, created_at, ""))
                conn.commit()

        return group_id, group_name

    def get_group_info(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT group_id, group_name, created_at, keywords FROM group_chats WHERE group_id = ?",
                               (group_id,))
                result = cursor.fetchone()
                if result:
                    return {
                        "group_id": result[0],
                        "group_name": result[1],
                        "created_at": result[2],
                        "keywords": result[3].split(",") if result[3] else []
                    }
                return None

    def list_groups(self) -> List[Dict[str, Any]]:
        groups = []
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT group_id, group_name, created_at, keywords FROM group_chats")
                for row in cursor.fetchall():
                    groups.append({
                        "group_id": row[0],
                        "group_name": row[1],
                        "created_at": row[2],
                        "keywords": row[3].split(",") if row[3] else []
                    })
        return groups

    def delete_group(self, group_id: str) -> bool:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM group_messages WHERE group_id = ?", (group_id,))
                cursor.execute("DELETE FROM group_chats WHERE group_id = ?", (group_id,))
                conn.commit()
                return cursor.rowcount > 0

    def message_exists(self, content: str) -> bool:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM group_messages WHERE content = ?", (content,))
                result = cursor.fetchone()
                return result[0] > 0
