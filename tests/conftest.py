from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.llm import LLMs
from app.config import Settings
from app.container import Container, build_container
from app.schemas import Incident
from tests.fakes import (
    FIXED_CART,
    TRACE,
    FakeChat,
    FakeClock,
    FakeGitHub,
    critic_responder,
    make_main_responder,
)


def make_settings(tmp_path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": str(tmp_path),
        "groq_api_key": "test",
        "github_token": "test",
        "ci_poll_interval": 1,
        "ci_no_runs_grace": 30,
        "ci_timeout": 300,
        "sandbox_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Harness:
    def __init__(self, container: Container, github: FakeGitHub, main: FakeChat, fast: FakeChat) -> None:
        self.c = container
        self.github = github
        self.main = main
        self.fast = fast

    async def start(self, chat_id: str | None = None, **kw: Any) -> str:
        incident = Incident(repo="acme/shop", error_text=TRACE, chat_id=chat_id, **kw)
        run_id = await self.c.runner.start(incident)
        await self.c.runner.wait_idle()
        return run_id

    async def decide(self, run_id: str, approved: bool) -> None:
        await self.c.runner.resume(run_id, approved, actor="test")
        await self.c.runner.wait_idle()

    async def status(self, run_id: str) -> str:
        run = await self.c.store.get_run(run_id)
        return run.status


@pytest.fixture
async def build_harness(tmp_path):
    created: list[Container] = []

    async def factory(
        *,
        fixes: list[str] | None = None,
        github: FakeGitHub | None = None,
        main: FakeChat | None = None,
        fallback: FakeChat | None = None,
        telegram: Any = None,
        sandbox: Any = None,
        **settings: Any,
    ) -> Harness:
        gh = github or FakeGitHub()
        main = main or FakeChat(make_main_responder(fixes or [FIXED_CART]))
        fast = FakeChat(critic_responder)
        clock = FakeClock()
        container = await build_container(
            make_settings(tmp_path, **settings),
            github=gh,
            llms=LLMs(main=main, fast=fast, fallback=fallback),
            telegram=telegram,
            checkpointer=InMemorySaver(),
            agent_overrides={"sleep": clock.sleep, "clock": clock, "sandbox": sandbox},
        )
        created.append(container)
        return Harness(container, gh, main, fast)

    yield factory
    for container in created:
        await container.stop()
