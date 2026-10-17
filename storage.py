"""Atomic JSON conversations, compatible with the original data directory."""

import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("openzxf.storage")
DATA_DIR = Path(__file__).resolve().parent / "data"
CONVERSATIONS_DIR = DATA_DIR / "conversations"
INDEX_PATH = DATA_DIR / "conversations_index.json"
_ID = re.compile(r"(?:[0-9a-f]{12}|[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z", re.I)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class InvalidConversationID(ValueError):
    pass


class StorageError(RuntimeError):
    pass


def validate_conversation_id(conv_id: str) -> str:
    if not isinstance(conv_id, str) or not _ID.fullmatch(conv_id):
        raise InvalidConversationID("Invalid conversation ID")
    return conv_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    """Serialize read/modify/write transactions across threads and server workers.

    Conversation files are authoritative; sidebar metadata can be recovered.
    Existing 12-character IDs, timestamps and additional message fields survive.
    """

    def __init__(self, data_dir: str | Path = DATA_DIR):
        self.data_dir = Path(data_dir).resolve()
        self.conversations_dir = self.data_dir / "conversations"
        self.index_path = self.data_dir / "conversations_index.json"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.data_dir).casefold(), threading.RLock())

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.conversations_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / ".storage.lock").open("a+b") as lock_file:
                if os.name == "nt":
                    import msvcrt
                    lock_file.seek(0, os.SEEK_END)
                    if lock_file.tell() == 0:
                        lock_file.write(b"\0")
                        lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _path(self, conv_id: str) -> Path:
        return self.conversations_dir / f"{validate_conversation_id(conv_id)}.json"

    @staticmethod
    def _write(path: Path, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.stem}.", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                json.dump(value, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read(self, conv_id: str) -> dict | None:
        path = self._path(conv_id)
        if not path.exists():
            return None
        try:
            conversation = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("Conversation could not be read; its original file was preserved") from exc
        if (not isinstance(conversation, dict) or conversation.get("id") != conv_id
                or not isinstance(conversation.get("messages"), list)):
            raise StorageError("Conversation has an invalid format; its original file was preserved")
        if any(not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "system"}
               or not isinstance(message.get("content"), str) for message in conversation["messages"]):
            raise StorageError("Conversation messages have an invalid format; its original file was preserved")
        return conversation

    def _index(self) -> list[dict]:
        """Reconcile complete files, recovering missing or interrupted index writes."""
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            previous = value if isinstance(value, list) else []
        except (OSError, ValueError):
            previous = []
        metadata = {item["id"]: item for item in previous if isinstance(item, dict)
                    and isinstance(item.get("id"), str) and _ID.fullmatch(item["id"])}
        result = []
        for path in self.conversations_dir.glob("*.json"):
            if not _ID.fullmatch(path.stem):
                continue
            try:
                conversation = self._read(path.stem)
            except StorageError:
                logger.warning("Skipping unreadable conversation %s", path.stem)
                if path.stem in metadata:
                    result.append(metadata[path.stem])
                continue
            item = dict(metadata.get(path.stem, {}))
            for key in ("id", "title", "created_at", "updated_at"):
                item[key] = conversation.get(key, "新对话" if key == "title" else "")
            result.append(item)
        result.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        if result != previous:
            self._write(self.index_path, result)
        return result

    def create_conversation(self, title: str = "新对话") -> dict:
        with self._transaction():
            now = _now()
            conversation = {"id": uuid.uuid4().hex, "title": title or "新对话",
                            "created_at": now, "updated_at": now, "messages": []}
            self._write(self._path(conversation["id"]), conversation)
            self._index()
            return copy.deepcopy(conversation)

    def get_conversation(self, conv_id: str) -> dict | None:
        validate_conversation_id(conv_id)
        with self._transaction():
            return copy.deepcopy(self._read(conv_id))

    def list_conversations(self) -> list[dict]:
        with self._transaction():
            return copy.deepcopy(self._index())

    def _save(self, conversation: dict):
        if conversation.get("title", "新对话") == "新对话":
            first_user = next((m["content"] for m in conversation["messages"] if m["role"] == "user"), "")
            if first_user.strip():
                conversation["title"] = first_user.strip()[:30]
        conversation["updated_at"] = _now()
        self._write(self._path(conversation["id"]), conversation)
        self._index()

    def update_conversation(self, conv_id: str, messages: list[dict] | None = None,
                            title: str | None = None):
        validate_conversation_id(conv_id)
        with self._transaction():
            conversation = self._read(conv_id)
            if conversation is None:
                return None
            if messages is not None:
                if any(not isinstance(m, dict) or m.get("role") not in {"user", "assistant", "system"}
                       or not isinstance(m.get("content"), str) for m in messages):
                    raise ValueError("Invalid conversation messages")
                conversation["messages"] = copy.deepcopy(messages)
            if title is not None:
                conversation["title"] = title.strip()[:100] or "新对话"
            self._save(conversation)
            return copy.deepcopy(conversation)

    def add_message(self, conv_id: str, role: str, content: str, citations: list | None = None,
                    *, interrupted: bool = False, request_id: str | None = None):
        validate_conversation_id(conv_id)
        if role not in {"user", "assistant", "system"} or not isinstance(content, str):
            raise ValueError("Invalid message")
        with self._transaction():
            conversation = self._read(conv_id)
            if conversation is None:
                raise FileNotFoundError("Conversation not found")
            message = {"role": role, "content": content, "timestamp": _now()}
            if citations:
                message["citations"] = copy.deepcopy(citations)
            if interrupted:
                message["interrupted"] = True
            if request_id:
                message["request_id"] = request_id
            conversation["messages"].append(message)
            self._save(conversation)
            return copy.deepcopy(message)

    def delete_conversation(self, conv_id: str) -> bool:
        validate_conversation_id(conv_id)
        with self._transaction():
            path = self._path(conv_id)
            existed = path.exists()
            path.unlink(missing_ok=True)
            self._index()
            return existed


def _default_store() -> ConversationStore:
    return ConversationStore(DATA_DIR)


def create_conversation(title="新对话"):
    return _default_store().create_conversation(title)


def get_conversation(conv_id):
    return _default_store().get_conversation(conv_id)


def list_conversations():
    return _default_store().list_conversations()


def update_conversation(conv_id, messages=None, title=None):
    return _default_store().update_conversation(conv_id, messages, title)


def add_message(conv_id, role, content, citations=None):
    return _default_store().add_message(conv_id, role, content, citations)


def delete_conversation(conv_id):
    return _default_store().delete_conversation(conv_id)
