import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Source(StrEnum):
    WEB = "web"
    ISSUE = "issue"


class Channel(StrEnum):
    """Куда отправляется запрос на одобрение."""

    WEB = "web"
    TELEGRAM = "telegram"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class Outcome(StrEnum):
    PR_OK = "pr_ok"
    PR_NEEDS_WORK = "pr_needs_work"
    REJECTED = "rejected"
    GAVE_UP = "gave_up"
    NOT_REPRODUCED = "not_reproduced"
    DIAGNOSED = "diagnosed"
    ERROR = "error"


class RunCreate(BaseModel):
    repo: str | None = Field(
        default=None,
        max_length=200,
        examples=["octocat/bug-demo"],
        description="owner/name, local:<папка> или пусто — тогда только диагноз по тексту ошибки",
    )
    error_text: str = Field(min_length=10, max_length=20000)

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    telegram_chat_id: str | None = Field(
        default=None, max_length=40, description="Chat ID или @username того, кто написал боту /start"
    )

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if value.lstrip("-").isdigit():
            return value
        if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", value):
            return "@" + value.lstrip("@")
        raise ValueError("Укажите Telegram chat ID (число) или @username")


class Incident(BaseModel):
    """Единый вид входных данных независимо от источника (форма, issue)."""

    repo: str = ""  # пусто — режим диагноза по тексту ошибки, без репозитория
    error_text: str
    chat_id: str | None = None
    source: Source = Source.WEB
    issue_number: int | None = None


class Decision(BaseModel):
    approved: bool
    comment: str | None = Field(default=None, max_length=1000)


class Finding(BaseModel):
    """Результат расследования. Это же схема аргументов инструмента submit_finding."""

    file_path: str = Field(description="Path of the file to change, exactly as in the repository file tree")
    root_cause: str = Field(description="Root cause in 2-3 sentences, in Russian")
    fix_plan: str = Field(description="Minimal change that fixes the bug, 1-2 sentences, in Russian")


class RunEvent(BaseModel):
    ts: str
    node: str
    level: str
    message: str


class RunOut(BaseModel):
    id: str
    repo: str
    source: str
    channel: str
    status: str
    outcome: str | None = None
    issue_number: int | None = None
    pr_url: str | None = None
    error: str | None = None
    tokens: int = 0
    created_at: str
    updated_at: str


class RunDetail(RunOut):
    error_text: str
    approval: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    events: list[RunEvent] = []
