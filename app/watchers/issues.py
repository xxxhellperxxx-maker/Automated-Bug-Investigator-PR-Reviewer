"""Опрос GitHub Issues: публичный адрес и вебхук не нужны.

Каждые ISSUE_POLL_INTERVAL секунд проверяет WATCHED_REPOS на новые открытые issue с меткой ISSUE_LABEL.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.clients.github import GitHubError
from app.config import Settings
from app.runner import RunManager
from app.schemas import Incident, Source
from app.store import Store

log = logging.getLogger(__name__)


def incident_from_issue(repo: str, issue: dict, chat_id: str | None) -> Incident:
    return Incident(
        repo=repo,
        error_text=f"{issue.get('title', '')}\n\n{issue.get('body') or ''}"[:20000],
        chat_id=chat_id,
        source=Source.ISSUE,
        issue_number=issue["number"],
    )


class IssuePoller:
    def __init__(self, github, store: Store, runner: RunManager, settings: Settings) -> None:
        self.github = github
        self.store = store
        self.runner = runner
        self.settings = settings
        backfill = timedelta(days=7) if settings.issue_backfill else timedelta(0)
        # По умолчанию только issue, созданные после старта сервиса: не запускаем агента по старым.
        self.started_at = datetime.now(UTC) - backfill

    async def poll_once(self) -> list[str]:
        started: list[str] = []
        since = self.started_at.isoformat(timespec="seconds").replace("+00:00", "Z")
        for repo in self.settings.watched_repo_list:
            try:
                issues = await self.github.list_issues(repo, label=self.settings.issue_label, since=since)
            except GitHubError as exc:
                log.warning("issue poll failed for %s: %s", repo, exc)
                continue
            for issue in issues:
                created = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
                if created < self.started_at:
                    continue
                if await self.store.mark_issue_seen(repo, issue["number"]):
                    run_id = await self.runner.start(
                        incident_from_issue(repo, issue, await self.runner.default_chat_id())
                    )
                    log.info("started run %s for %s#%s", run_id, repo, issue["number"])
                    started.append(run_id)
        return started

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("issue poller error")
            await asyncio.sleep(self.settings.issue_poll_interval)
