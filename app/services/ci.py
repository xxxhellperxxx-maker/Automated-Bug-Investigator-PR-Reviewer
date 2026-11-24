"""Оценка результатов GitHub Actions. В прототипе Actions — песочница для тестов."""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

GOOD = {"success", "neutral", "skipped"}
Status = Literal["passed", "failed", "none", "timeout", "pending"]

LABELS: dict[str, str] = {
    "passed": "тесты прошли",
    "failed": "тесты упали",
    "none": "тесты не запускались (в репозитории нет GitHub Actions на push)",
    "timeout": "тесты не завершились вовремя",
    "pending": "тесты выполняются",
}


@dataclass
class CiResult:
    status: Status
    label: str
    failed_run_ids: list[int] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def finished(self) -> bool:
        return self.status != "pending"

    @property
    def ok(self) -> bool:
        # Отсутствие тестов не блокирует сценарий, но явно показывается человеку при одобрении.
        return self.status in ("passed", "none")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


def _short(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "event": r.get("event"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "url": r.get("html_url"),
        }
        for r in runs
    ]


def evaluate_runs(
    runs: list[dict[str, Any]],
    elapsed: float,
    *,
    no_runs_grace: float,
    timeout: float,
    prefer_event: str | None = None,
) -> CiResult:
    note = ""
    if prefer_event:
        preferred = [r for r in runs if str(r.get("event", "")).startswith(prefer_event)]
        if preferred:
            runs = preferred
        elif runs and elapsed > no_runs_grace:
            note = f"Отдельного запуска на {prefer_event} нет, использован результат запуска на push."
        elif runs:
            return CiResult("pending", LABELS["pending"], runs=_short(runs))

    if not runs:
        status: Status = "none" if elapsed > no_runs_grace else "pending"
        return CiResult(status, LABELS[status])
    if any(r.get("status") != "completed" for r in runs):
        status = "timeout" if elapsed > timeout else "pending"
        return CiResult(status, LABELS[status], runs=_short(runs), note=note)

    failed = [r["id"] for r in runs if r.get("conclusion") not in GOOD]
    status = "failed" if failed else "passed"
    return CiResult(status, LABELS[status], failed_run_ids=failed, runs=_short(runs), note=note)


_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?", re.M)


def extract_failure_log(raw: str, limit: int = 3000) -> str:
    """Из полного лога job оставляет самое полезное для модели: секцию FAILURES pytest или хвост."""
    log = _TS_RE.sub("", raw or "")
    for marker in ("= FAILURES =", "short test summary", "Traceback", "Error:"):
        pos = log.find(marker)
        if pos >= 0:
            return log[pos : pos + limit]
    return log[-limit:]
