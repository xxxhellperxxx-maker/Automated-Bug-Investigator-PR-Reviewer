"""Общие типы песочницы (отдельно, чтобы languages.py и sandbox.py не импортировали друг друга)."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Status = Literal["passed", "failed", "no_tests", "timeout", "error"]


@dataclass
class TestRun:
    __test__ = False  # не тестовый класс pytest, несмотря на имя

    status: Status
    failed: list[str] = field(default_factory=list)
    summary: str = ""
    output: str = ""
    exit_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
