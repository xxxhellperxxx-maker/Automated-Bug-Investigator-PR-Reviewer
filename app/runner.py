"""Запуск графа в фоне, пауза на одобрение и возобновление.

thread_id в LangGraph = id запуска, поэтому состояние переживает перезапуск процесса:
незавершённые запуски продолжаются с последнего чекпойнта (recover).
"""

import asyncio
import logging
import uuid
from typing import Any

from langgraph.types import Command

from app.agent.graph import branch_name
from app.clients.github import GitHubError
from app.clients.telegram import Notifier
from app.config import Settings
from app.schemas import Channel, Incident, RunStatus
from app.store import Store

log = logging.getLogger(__name__)


class RunConflictError(Exception):
    pass


class RunManager:
    def __init__(
        self, graph: Any, store: Store, settings: Settings, github: Any, notifier: Notifier | None = None
    ) -> None:
        self.graph = graph
        self.store = store
        self.settings = settings
        self.github = github
        self.notifier = notifier
        self._tasks: set[asyncio.Task] = set()

    def _config(self, run_id: str) -> dict[str, Any]:
        # run_name/tags/metadata попадают в LangSmith и позволяют найти трейс по id запуска.
        return {
            "configurable": {"thread_id": run_id},
            "recursion_limit": 100,
            "run_name": "bug-investigation",
            "tags": ["ai-bug-investigator"],
            "metadata": {"run_id": run_id},
        }

    async def default_chat_id(self) -> str | None:
        """Чат для запусков из issue: chat ID из .env или последний чат, написавший боту /start."""
        if self.notifier is None:
            return None
        return self.settings.default_chat_id or await self.store.latest_telegram_chat()

    async def resolve_chat(self, value: str | None) -> str | None:
        """chat ID как есть; @username — по чатам, написавшим боту /start. LookupError, если не найден."""
        if not value:
            return None
        if value.lstrip("-").isdigit():
            return value
        chat_id = await self.store.find_telegram_chat(value)
        if chat_id is None:
            raise LookupError(value)
        return chat_id

    async def start(self, incident: Incident) -> str:
        run_id = uuid.uuid4().hex
        use_telegram = bool(incident.chat_id) and self.notifier is not None
        channel = Channel.TELEGRAM if use_telegram else Channel.WEB
        await self.store.create_run(run_id, incident, channel)
        if incident.chat_id and not use_telegram:
            await self.store.add_event(
                run_id, "intake", "Telegram-бот не настроен, подтверждение будет на сайте", "warning"
            )
        payload = {"run_id": run_id, "incident": incident.model_dump(mode="json"), "channel": channel.value}
        self._spawn(run_id, payload)
        return run_id

    async def resume(self, run_id: str, approved: bool, actor: str, comment: str | None = None) -> None:
        if not await self.store.transition(run_id, RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING):
            raise RunConflictError("Запуск не ожидает решения (уже обработан или ещё выполняется)")
        self._spawn(run_id, Command(resume={"approved": approved, "actor": actor, "comment": comment}))

    async def recover(self) -> None:
        for run_id in await self.store.runs_with_status(RunStatus.RUNNING, RunStatus.QUEUED):
            await self.store.add_event(
                run_id, "runner", "Сервис перезапущен, продолжаю с последнего чекпойнта", "warning"
            )
            self._spawn(run_id, None)

    def _spawn(self, run_id: str, payload: Any) -> None:
        task = asyncio.create_task(self._execute(run_id, payload), name=f"run-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute(self, run_id: str, payload: Any) -> None:
        config = self._config(run_id)
        try:
            await self.store.update_run(run_id, status=RunStatus.RUNNING)
            result = await self.graph.ainvoke(payload, config)
            snapshot = await self.graph.aget_state(config)
            if snapshot.next:
                await self.store.update_run(run_id, status=RunStatus.AWAITING_APPROVAL)
            else:
                pr = result.get("pr") or {}
                await self.store.update_run(
                    run_id,
                    status=RunStatus.COMPLETED,
                    outcome=result.get("outcome"),
                    pr_url=pr.get("url"),
                    result=result.get("report"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("run %s failed", run_id)
            await self.store.add_event(run_id, "runner", f"Ошибка: {exc}", "error")
            await self.store.update_run(
                run_id, status=RunStatus.FAILED, outcome="error", error=str(exc)[:500]
            )
            await self._cleanup_failed(run_id)

    async def _cleanup_failed(self, run_id: str) -> None:
        """После сбоя не оставляем мусорную ветку и сообщаем человеку."""
        row = await self.store.get_row(run_id)
        if row is None:
            return
        if not row.get("pr_url"):
            try:
                await self.github.delete_branch(row["repo"], branch_name(self.settings, run_id))
            except GitHubError:
                pass  # ветки могло и не быть
            except Exception:
                log.warning("branch cleanup failed for %s", run_id)
        if row["channel"] == Channel.TELEGRAM and self.notifier and row.get("chat_id"):
            try:
                await self.notifier.send(
                    row["chat_id"],
                    f"<b>Запуск завершился ошибкой</b>\n{self.settings.public_base_url}/runs/{run_id}",
                )
            except Exception:
                log.warning("telegram notification failed for %s", run_id)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Для тестов: дождаться завершения всех фоновых задач."""
        async with asyncio.timeout(timeout):
            while self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
