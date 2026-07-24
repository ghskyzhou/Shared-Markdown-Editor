from __future__ import annotations

import hmac
import os
import sqlite3
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

LEGACY_DATA_FILE = BASE_DIR / "data.txt"
DEFAULT_DATABASE_FILE = BASE_DIR / "documents.db"
MAX_DOCUMENT_SIZE = 2 * 1024 * 1024
MAX_TITLE_LENGTH = 160
HISTORY_LIMIT = 500
APP_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def required_environment_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"缺少环境变量 {name}，请复制 .env.example 为 .env 并完成配置。"
        )
    return value


LOGIN_PASSWORD = required_environment_value("MARKDOWN_EDITOR_PASSWORD")
SESSION_SECRET_KEY = required_environment_value("MARKDOWN_EDITOR_SECRET_KEY")


app = Flask(__name__)
app.config.update(
    SECRET_KEY=SESSION_SECRET_KEY,
    DATABASE=str(DEFAULT_DATABASE_FILE),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
socketio = SocketIO(app, cors_allowed_origins=None)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connect_database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(app.config["DATABASE"], timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def infer_title(content: str) -> str:
    for line in content.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        candidate = candidate.lstrip("#").strip()
        if candidate:
            return candidate[:MAX_TITLE_LENGTH]
    return "无标题文档"


def initialize_database(*, migrate_legacy: bool = True) -> None:
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        document_count = connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]

        if document_count == 0 and migrate_legacy and LEGACY_DATA_FILE.exists():
            legacy_content = LEGACY_DATA_FILE.read_text(encoding="utf-8")
            if legacy_content:
                connection.execute(
                    """
                    INSERT INTO documents (title, content, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (infer_title(legacy_content), legacy_content, utc_now_iso()),
                )


def create_document(title: str = "无标题文档") -> int:
    now = utc_now_iso()
    with connect_database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO documents (title, content, updated_at)
            VALUES (?, '', ?)
            """,
            (title, now),
        )
        return int(cursor.lastrowid)


def get_document_record(document_id: int) -> sqlite3.Row | None:
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT id, title, content, updated_at
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()


def list_document_records() -> list[sqlite3.Row]:
    with connect_database() as connection:
        return connection.execute(
            """
            SELECT id, title, updated_at
            FROM documents
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()


def save_document_content(document_id: int, content: str) -> None:
    with connect_database() as connection:
        cursor = connection.execute(
            """
            UPDATE documents
            SET content = ?, updated_at = ?
            WHERE id = ?
            """,
            (content, utc_now_iso(), document_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("文档不存在")


def rename_document(document_id: int, title: str) -> str:
    cleaned_title = " ".join(title.split()).strip()[:MAX_TITLE_LENGTH]
    if not cleaned_title:
        cleaned_title = "无标题文档"

    with connect_database() as connection:
        cursor = connection.execute(
            """
            UPDATE documents
            SET title = ?, updated_at = ?
            WHERE id = ?
            """,
            (cleaned_title, utc_now_iso(), document_id),
        )
        if cursor.rowcount != 1:
            raise LookupError("文档不存在")
    return cleaned_title


def display_document_time(value: str, now: datetime | None = None) -> tuple[str, str]:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local_time = parsed.astimezone(APP_TIMEZONE)
    local_now = now.astimezone(APP_TIMEZONE) if now else datetime.now(APP_TIMEZONE)

    if local_time.date() == local_now.date():
        return "今天", local_time.strftime("%H:%M")
    if local_time.date() == (local_now.date() - timedelta(days=1)):
        return "更早", f"昨天 {local_time:%H:%M}"
    if local_time.year == local_now.year:
        return "更早", f"{local_time.month}月{local_time.day}日"
    return "更早", f"{local_time.year}年{local_time.month}月{local_time.day}日"


def single_splice(before: str, after: str) -> dict[str, Any] | None:
    """Return the smallest single replacement that turns before into after."""
    if before == after:
        return None

    start = 0
    shared_length = min(len(before), len(after))
    while start < shared_length and before[start] == after[start]:
        start += 1

    before_end = len(before)
    after_end = len(after)
    while (
        before_end > start
        and after_end > start
        and before[before_end - 1] == after[after_end - 1]
    ):
        before_end -= 1
        after_end -= 1

    return {
        "start": start,
        "delete_count": before_end - start,
        "insert": after[start:after_end],
    }


class CollaborativeDocument:
    """A server-authoritative document that preserves concurrent character edits."""

    def __init__(self, document_id: int, content: str) -> None:
        self.document_id = document_id
        self._lock = threading.RLock()
        self.content = content
        self.version = 0
        self._next_atom_id = len(content)
        self._atoms: list[tuple[int, str]] = list(enumerate(content))
        self._snapshots: dict[int, tuple[str, tuple[int, ...]]] = {
            0: (content, tuple(range(len(content))))
        }
        self._snapshot_order: deque[int] = deque([0])

    def snapshot(self) -> tuple[str, int]:
        with self._lock:
            return self.content, self.version

    def _remember_snapshot(self) -> None:
        self._snapshots[self.version] = (
            self.content,
            tuple(atom_id for atom_id, _ in self._atoms),
        )
        self._snapshot_order.append(self.version)
        while len(self._snapshot_order) > HISTORY_LIMIT + 1:
            expired = self._snapshot_order.popleft()
            self._snapshots.pop(expired, None)

    def merge(self, proposed_content: str, base_version: int) -> dict[str, Any]:
        if not isinstance(proposed_content, str):
            raise ValueError("文档内容格式无效")
        if len(proposed_content.encode("utf-8")) > MAX_DOCUMENT_SIZE:
            raise ValueError("文档不能超过 2 MB")

        with self._lock:
            base_snapshot = self._snapshots.get(base_version)
            if base_snapshot is None:
                raise LookupError("当前页面版本过旧，请先同步最新内容")
            base_content, base_atom_ids = base_snapshot

            edit = single_splice(base_content, proposed_content)
            if edit is None:
                return {
                    "content": self.content,
                    "version": self.version,
                    "operation": None,
                }

            start = int(edit["start"])
            end = start + int(edit["delete_count"])
            before_merge = self.content

            deleted_atom_ids = set(base_atom_ids[start:end])
            if deleted_atom_ids:
                self._atoms = [
                    atom
                    for atom in self._atoms
                    if atom[0] not in deleted_atom_ids
                ]

            current_positions = {
                atom_id: index
                for index, (atom_id, _) in enumerate(self._atoms)
            }
            insertion_index = len(self._atoms)
            for right_atom_id in base_atom_ids[end:]:
                if right_atom_id in current_positions:
                    insertion_index = current_positions[right_atom_id]
                    break

            inserted_atoms = []
            for character in str(edit["insert"]):
                inserted_atoms.append((self._next_atom_id, character))
                self._next_atom_id += 1
            self._atoms[insertion_index:insertion_index] = inserted_atoms
            self.content = "".join(character for _, character in self._atoms)
            self.version += 1
            self._remember_snapshot()
            save_document_content(self.document_id, self.content)

            return {
                "content": self.content,
                "version": self.version,
                "operation": single_splice(before_merge, self.content),
            }

    def persist(self) -> tuple[str, int]:
        with self._lock:
            save_document_content(self.document_id, self.content)
            return self.content, self.version


document_states: dict[int, CollaborativeDocument] = {}
document_states_lock = threading.RLock()


def get_document_state(document_id: int) -> CollaborativeDocument:
    with document_states_lock:
        existing = document_states.get(document_id)
        if existing:
            return existing

        record = get_document_record(document_id)
        if record is None:
            raise LookupError("文档不存在")
        state = CollaborativeDocument(document_id, record["content"])
        document_states[document_id] = state
        return state


def document_room(document_id: int) -> str:
    return f"document:{document_id}"


def is_safe_next_url(target: str | None) -> bool:
    if not target:
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")


@app.before_request
def require_login():
    if request.endpoint in {"login", "static"}:
        return None
    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, LOGIN_PASSWORD):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            next_url = request.form.get("next")
            return redirect(next_url if is_safe_next_url(next_url) else url_for("index"))
        error = "密钥不正确，请重试。"

    next_url = request.args.get("next", "")
    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    grouped_documents: dict[str, list[dict[str, Any]]] = {"今天": [], "更早": []}
    for record in list_document_records():
        group, display_time = display_document_time(record["updated_at"])
        grouped_documents[group].append(
            {
                "id": record["id"],
                "title": record["title"],
                "display_time": display_time,
            }
        )
    return render_template("home.html", grouped_documents=grouped_documents)


@app.route("/documents", methods=["POST"])
def new_document():
    document_id = create_document()
    return redirect(url_for("edit_document", document_id=document_id))


@app.route("/documents/<int:document_id>")
def edit_document(document_id: int):
    record = get_document_record(document_id)
    if record is None:
        abort(404)
    content, version = get_document_state(document_id).snapshot()
    return render_template(
        "index.html",
        document_id=document_id,
        document_title=record["title"],
        content=content,
        version=version,
    )


@app.route("/documents/<int:document_id>/title", methods=["POST"])
def update_document_title(document_id: int):
    data = request.get_json(silent=True) or {}
    try:
        title = rename_document(document_id, str(data.get("title", "")))
    except LookupError:
        abort(404)
    return jsonify({"status": "ok", "title": title})


@app.route("/documents/<int:document_id>/save", methods=["POST"])
def save_document(document_id: int):
    try:
        _, version = get_document_state(document_id).persist()
    except LookupError:
        abort(404)
    return jsonify({"status": "ok", "version": version})


@socketio.on("connect")
def socket_connect(auth=None):
    if not session.get("authenticated"):
        return False
    return None


@socketio.on("document:join")
def document_join(data):
    if not session.get("authenticated"):
        return False
    try:
        document_id = int((data or {}).get("document_id", -1))
        state = get_document_state(document_id)
    except (TypeError, ValueError, LookupError):
        emit("document:error", {"message": "文档不存在"})
        return None

    join_room(document_room(document_id))
    content, version = state.snapshot()
    emit(
        "document:init",
        {"document_id": document_id, "content": content, "version": version},
    )
    return None


@socketio.on("document:update")
def document_update(data):
    if not session.get("authenticated"):
        return False

    try:
        data = data or {}
        document_id = int(data.get("document_id", -1))
        state_handler = get_document_state(document_id)
        proposed_content = data.get("content")
        base_version = int(data.get("base_version", -1))
        state = state_handler.merge(proposed_content, base_version)
    except (TypeError, ValueError, LookupError) as error:
        try:
            content, version = state_handler.snapshot()
        except (NameError, UnboundLocalError):
            content, version = "", 0
        emit(
            "document:error",
            {
                "document_id": data.get("document_id"),
                "message": str(error),
                "content": content,
                "version": version,
            },
        )
        return None

    socketio.emit(
        "document:state",
        {
            **state,
            "document_id": document_id,
            "origin": request.sid,
        },
        room=document_room(document_id),
    )
    return None


@socketio.on("document:save")
def socket_document_save(data):
    if not session.get("authenticated"):
        return False
    try:
        document_id = int((data or {}).get("document_id", -1))
        _, version = get_document_state(document_id).persist()
    except (TypeError, ValueError, LookupError):
        emit("document:error", {"message": "文档不存在"})
        return None
    emit("document:saved", {"document_id": document_id, "version": version})
    return None


initialize_database()


if __name__ == "__main__":
    socketio.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
        allow_unsafe_werkzeug=True,
    )
