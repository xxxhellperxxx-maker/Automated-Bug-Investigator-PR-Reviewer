"""Хранилище запусков для UI и API.

Состояние самого графа хранит чекпойнтер LangGraph (checkpoints.db). Здесь — только то,
что нужно показать пользователю: статус, события («Агент читает файл...»), данные для одобрения.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.schemas import Incident, RunDetail, RunEvent, RunOut

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    chat_id TEXT,
    issue_number INTEGER,
    error_text TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    pr_url TEXT,
    error TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    approval_json TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    node TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS seen_issues (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS telegram_chats (
    chat_id TEXT PRIMARY KEY,
    username TEXT,
    name TEXT,
    updated_at TEXT NOT NULL
);
"""

_UPDATABLE = {"status", "outcome", "pr_url", "error", "tokens", "approval", "result", "channel"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not initialised")
        return self._db

    async def create_run(self, run_id: str, incident: Incident, channel: str) -> None:
        now = _now()
        await self.db.execute(
            "INSERT INTO runs (id, repo, source, channel, chat_id, issue_number, error_text, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (
                run_id,
                incident.repo,
                incident.source,
                channel,
                incident.chat_id,
                incident.issue_number,
                incident.error_text,
                now,
                now,
            ),
        )
        await self.db.commit()

    async def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Unknown run fields: {unknown}")
        columns, values = [], []
        for key, value in fields.items():
            if key in ("approval", "result"):
                key, value = (
                    f"{key}_json",
                    (json.dumps(value, ensure_ascii=False) if value is not None else None),
                )
            columns.append(f"{key} = ?")
            values.append(value)
        columns.append("updated_at = ?")
        values.extend([_now(), run_id])
        await self.db.execute(f"UPDATE runs SET {', '.join(columns)} WHERE id = ?", values)  # noqa: S608
        await self.db.commit()

    async def transition(self, run_id: str, from_status: str, to_status: str) -> bool:
        """Атомарно меняет статус. Защищает от двойного одобрения (две кнопки, два клика)."""
        cur = await self.db.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (to_status, _now(), run_id, from_status),
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def add_tokens(self, run_id: str, tokens: int) -> None:
        if tokens:
            await self.db.execute("UPDATE runs SET tokens = tokens + ? WHERE id = ?", (tokens, run_id))
            await self.db.commit()

    async def add_event(self, run_id: str, node: str, message: str, level: str = "info") -> None:
        await self.db.execute(
            "INSERT INTO events (run_id, ts, node, level, message) VALUES (?, ?, ?, ?, ?)",
            (run_id, _now(), node, level, message[:2000]),
        )
        await self.db.commit()

    async def get_row(self, run_id: str) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_run(self, run_id: str) -> RunDetail | None:
        row = await self.get_row(run_id)
        if row is None:
            return None
        cur = await self.db.execute(
            "SELECT ts, node, level, message FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        )
        events = [RunEvent(**dict(r)) for r in await cur.fetchall()]
        return RunDetail(
            **_run_fields(row),
            error_text=row["error_text"],
            approval=json.loads(row["approval_json"]) if row["approval_json"] else None,
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            events=events,
        )

    async def list_runs(self, limit: int = 50) -> list[RunOut]:
        cur = await self.db.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [RunOut(**_run_fields(dict(r))) for r in await cur.fetchall()]

    async def runs_with_status(self, *statuses: str) -> list[str]:
        marks = ",".join("?" for _ in statuses)
        cur = await self.db.execute(f"SELECT id FROM runs WHERE status IN ({marks})", statuses)  # noqa: S608
        return [r["id"] for r in await cur.fetchall()]

    async def mark_issue_seen(self, repo: str, number: int) -> bool:
        """True, если issue встретился впервые (чтобы не запускать агента дважды)."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO seen_issues (repo, number) VALUES (?, ?)", (repo.lower(), number)
        )
        await self.db.commit()
        return cur.rowcount == 1

    # --- Telegram: чаты, которые написали боту /start (так бот узнаёт chat ID сам) ---
    async def save_telegram_chat(self, chat_id: str, username: str | None, name: str | None) -> None:
        await self.db.execute(
            "INSERT INTO telegram_chats (chat_id, username, name, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username, name = excluded.name,"
            " updated_at = excluded.updated_at",
            (str(chat_id), (username or "").lower() or None, name, _now()),
        )
        await self.db.commit()

    async def find_telegram_chat(self, username: str) -> str | None:
        cur = await self.db.execute(
            "SELECT chat_id FROM telegram_chats WHERE username = ? ORDER BY updated_at DESC LIMIT 1",
            (username.lstrip("@").lower(),),
        )
        row = await cur.fetchone()
        return row["chat_id"] if row else None

    async def latest_telegram_chat(self) -> str | None:
        cur = await self.db.execute("SELECT chat_id FROM telegram_chats ORDER BY updated_at DESC LIMIT 1")
        row = await cur.fetchone()
        return row["chat_id"] if row else None


def _run_fields(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "repo",
        "source",
        "channel",
        "status",
        "outcome",
        "issue_number",
        "pr_url",
        "error",
        "tokens",
        "created_at",
        "updated_at",
    )
    return {k: row[k] for k in keys}
