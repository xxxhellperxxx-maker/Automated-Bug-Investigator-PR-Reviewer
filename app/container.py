"""Сборка зависимостей. Тесты подменяют github/llms/checkpointer двойниками."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.graph import AgentDeps, build_graph
from app.agent.llm import LLMs, build_llms
from app.clients.github import GitHubClient
from app.clients.local_repo import LocalRepoClient, RepoRouter
from app.clients.telegram import TelegramClient, TelegramNotifier
from app.config import Settings
from app.runner import RunManager
from app.sandbox import DockerSandbox
from app.store import Store
from app.watchers.issues import IssuePoller
from app.watchers.telegram import TelegramPoller

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    store: Store
    github: Any
    runner: RunManager
    telegram: TelegramClient | None = None
    issue_poller: IssuePoller | None = None
    telegram_poller: TelegramPoller | None = None
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)
    background: list[asyncio.Task] = field(default_factory=list)
    telegram_bot_username: str | None = None
    sandbox: Any | None = None
    local_repos: Any | None = None

    async def start(self, *, recover: bool = True, pollers: bool = True) -> None:
        if self.telegram is not None and pollers:
            try:
                self.telegram_bot_username = (await self.telegram.get_me()).get("username")
            except Exception as exc:  # бот недоступен — сервис всё равно работает, одобрение на сайте
                log.warning("telegram getMe failed: %s", exc)
        if recover:
            await self.runner.recover()
        if pollers and self.issue_poller:
            self.background.append(asyncio.create_task(self.issue_poller.run_forever(), name="issue-poller"))
        if pollers and self.telegram_poller:
            self.background.append(asyncio.create_task(self.telegram_poller.run_forever(), name="tg-poller"))

    async def stop(self) -> None:
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        await self.runner.shutdown()
        await self.stack.aclose()


async def build_container(
    settings: Settings,
    *,
    github: Any | None = None,
    llms: LLMs | None = None,
    telegram: TelegramClient | None = None,
    checkpointer: Any | None = None,
    agent_overrides: dict[str, Any] | None = None,
) -> Container:
    stack = contextlib.AsyncExitStack()
    settings.export_langsmith_env()

    store = Store(settings.runs_db)
    await store.init()
    stack.push_async_callback(store.close)

    local_repos = None
    if github is None:
        if not settings.github_token:
            log.warning("GITHUB_TOKEN is not set: GitHub calls will fail")
        remote = GitHubClient(settings.github_token, settings.github_api_url)
        stack.push_async_callback(remote.aclose)
        if Path(settings.local_projects_dir).is_dir():  # noqa: ASYNC240 — один раз при старте
            local_repos = LocalRepoClient(settings.local_projects_dir, settings.local_projects_host_dir)
        github = RepoRouter(remote, local_repos)

    if telegram is None and settings.telegram_enabled:
        telegram = TelegramClient(settings.telegram_bot_token)
        stack.push_async_callback(telegram.aclose)
    notifier = TelegramNotifier(telegram) if telegram else None

    if checkpointer is None:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        checkpointer = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(settings.checkpoint_db)
        )

    sandbox = None
    # Проверка один раз при старте; блокирующий stat здесь допустим.
    if settings.sandbox_enabled and Path(settings.docker_socket).exists():  # noqa: ASYNC240
        sandbox = DockerSandbox(settings.docker_socket, settings.sandbox_image, settings.sandbox_test_timeout)
        stack.push_async_callback(sandbox.aclose)
    elif settings.sandbox_enabled:
        log.warning(
            "Docker socket %s not found: sandbox disabled, tests only via GitHub Actions",
            settings.docker_socket,
        )

    deps = AgentDeps(
        settings=settings,
        github=github,
        llms=llms or build_llms(settings),
        store=store,
        notifier=notifier,
        **({"sandbox": sandbox} | (agent_overrides or {})),
    )
    graph = build_graph(deps, checkpointer)
    sandbox = deps.sandbox
    runner = RunManager(graph, store, settings, github, notifier)

    issue_poller = IssuePoller(github, store, runner, settings) if settings.watched_repo_list else None
    telegram_poller = TelegramPoller(telegram, store, runner) if telegram else None
    return Container(
        settings=settings,
        store=store,
        github=github,
        runner=runner,
        telegram=telegram,
        issue_poller=issue_poller,
        telegram_poller=telegram_poller,
        stack=stack,
        sandbox=sandbox,
        local_repos=getattr(github, "local", None),
    )
