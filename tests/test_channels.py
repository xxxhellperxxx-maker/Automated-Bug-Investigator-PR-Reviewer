"""Telegram-одобрение и опрос GitHub Issues."""

from datetime import UTC, datetime, timedelta

import pytest

from app.watchers.issues import IssuePoller
from tests.fakes import TRACE, FakeTelegram


def _callback(run_id: str, action: str, chat_id: str) -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "data": f"{action}:{run_id}",
            "from": {"id": 1, "username": "dev"},
            "message": {"message_id": 1, "chat": {"id": int(chat_id)}, "text": "approval"},
        },
    }


async def test_telegram_approval_flow(build_harness):
    tg = FakeTelegram()
    h = await build_harness(telegram=tg, telegram_bot_token="123:abc")
    run_id = await h.start(chat_id="555")

    run = await h.c.store.get_run(run_id)
    assert run.channel == "telegram" and run.status == "awaiting_approval"
    request = tg.sent[0]
    assert request["chat_id"] == "555"
    assert request["buttons"][0][0][1] == f"approve:{run_id}"
    assert "app/cart.py" in request["html"]

    # Сайт не может принять решение за Telegram-запуск.
    poller = h.c.telegram_poller
    await poller.handle_update(_callback(run_id, "approve", "999"))
    assert tg.answers[-1] == "Нет прав на это решение"
    assert await h.status(run_id) == "awaiting_approval"

    await poller.handle_update(_callback(run_id, "approve", "555"))
    await h.c.runner.wait_idle()
    assert tg.answers[-1] == "Одобрено"
    run = await h.c.store.get_run(run_id)
    assert run.status == "completed" and run.outcome == "pr_ok"
    assert "PR" in tg.sent[-1]["html"] or "Pull Request" in tg.sent[-1]["html"]

    await poller.handle_update(_callback(run_id, "reject", "555"))
    assert tg.answers[-1] == "Решение уже принято"


async def test_without_bot_chat_id_falls_back_to_web(build_harness):
    h = await build_harness()
    run_id = await h.start(chat_id="555")
    run = await h.c.store.get_run(run_id)
    assert run.channel == "web"
    assert any("Telegram-бот не настроен" in e.message for e in run.events)


async def test_issue_poller_starts_run_once_and_comments_issue(build_harness):
    h = await build_harness(watched_repos="acme/shop")
    now = datetime.now(UTC)
    h.github.issues = [
        {
            "number": 3,
            "title": "Crash",
            "body": TRACE,
            "created_at": (now + timedelta(seconds=5)).isoformat(),
        },
        {"number": 1, "title": "Old", "body": TRACE, "created_at": (now - timedelta(days=1)).isoformat()},
    ]
    poller: IssuePoller = h.c.issue_poller
    started = await poller.poll_once()
    assert len(started) == 1
    assert await poller.poll_once() == []  # повторно не запускается

    await h.c.runner.wait_idle()
    await h.decide(started[0], approved=False)
    run = await h.c.store.get_run(started[0])
    assert run.source == "issue" and run.outcome == "rejected"
    issue_comments = [body for number, body in h.github.comments if number == 3]
    assert issue_comments and "отклонено" in issue_comments[0]


def _start_message(chat_id: int, username: str | None, chat_type: str = "private") -> dict:
    return {
        "update_id": 2,
        "message": {
            "message_id": 10,
            "text": "/start",
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": chat_id, "username": username, "first_name": "Dev"},
        },
    }


async def test_start_command_registers_chat_and_replies_with_id(build_harness):
    tg = FakeTelegram()
    h = await build_harness(telegram=tg, telegram_bot_token="123:abc")
    poller = h.c.telegram_poller

    await poller.handle_update(_start_message(777, "MiniDev"))
    assert await h.c.store.find_telegram_chat("@minidev") == "777"
    assert "<code>777</code>" in tg.sent[-1]["html"] and "@MiniDev" in tg.sent[-1]["html"]

    await poller.handle_update(_start_message(-100, "group", chat_type="group"))
    assert await h.c.store.find_telegram_chat("group") is None


async def test_username_from_form_is_resolved_to_chat(build_harness):
    tg = FakeTelegram()
    h = await build_harness(telegram=tg, telegram_bot_token="123:abc")
    await h.c.telegram_poller.handle_update(_start_message(777, "minidev"))

    assert await h.c.runner.resolve_chat("@MiniDev") == "777"
    assert await h.c.runner.resolve_chat("555") == "555"
    with pytest.raises(LookupError):
        await h.c.runner.resolve_chat("@stranger")

    run_id = await h.start(chat_id=await h.c.runner.resolve_chat("@minidev"))
    run = await h.c.store.get_run(run_id)
    assert run.channel == "telegram"
    assert tg.sent[-1]["chat_id"] == "777" and tg.sent[-1]["buttons"]


async def test_issue_runs_use_last_registered_chat_when_no_default(build_harness):
    tg = FakeTelegram()
    h = await build_harness(telegram=tg, telegram_bot_token="123:abc")
    assert await h.c.runner.default_chat_id() is None
    await h.c.telegram_poller.handle_update(_start_message(777, "minidev"))
    assert await h.c.runner.default_chat_id() == "777"

    h2 = await build_harness(telegram=FakeTelegram(), telegram_bot_token="1:a", telegram_default_chat_id="42")
    assert await h2.c.runner.default_chat_id() == "42"
