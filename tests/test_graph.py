"""Полный прогон графа LangGraph с двойниками GitHub и LLM: все ветки сценария."""

from langchain_core.messages import ToolMessage

from tests.fakes import BUGGY_CART, FIXED_CART, FakeChat, make_main_responder, tool_call


async def test_happy_path_waits_for_human_then_opens_pr(build_harness):
    h = await build_harness()
    run_id = await h.start()

    assert await h.status(run_id) == "awaiting_approval"
    assert h.github.prs == [], "PR не должен создаваться до одобрения"
    run = await h.c.store.get_run(run_id)
    assert run.approval["file_path"] == "app/cart.py"
    assert "+    if not items:" in run.approval["diff"]
    assert run.approval["ci_label"] == "баг исправлен: CI падал на исходном коде, теперь проходит"

    await h.decide(run_id, approved=True)

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed"
    assert run.outcome == "pr_ok"
    assert run.pr_url == "https://github.com/acme/shop/pull/7"
    assert run.tokens > 0
    pr = h.github.prs[0]
    assert pr["base"] == "main" and pr["head"].startswith("ai-fix/")
    assert "## Проблема" in pr["body"]
    # Проверка после PR оставлена комментарием в самом PR.
    assert h.github.comments[0][0] == 7
    assert "новых ошибок нет" in h.github.comments[0][1]
    nodes = [e.node for e in run.events]
    for node in (
        "intake",
        "load_repo",
        "investigate",
        "fix",
        "commit",
        "verify",
        "reflect",
        "review",
        "approval",
        "open_pr",
        "post_pr_check",
        "report",
    ):
        assert node in nodes


async def test_rejection_deletes_branch_and_creates_no_pr(build_harness):
    h = await build_harness()
    run_id = await h.start()
    await h.decide(run_id, approved=False)

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed"
    assert run.outcome == "rejected"
    assert h.github.prs == []
    assert len(h.github.deleted) == 1


async def test_reflexion_retries_with_test_log_then_gives_up(build_harness):
    still_broken = BUGGY_CART.replace("total /", "total * 1 /")
    h = await build_harness(fixes=[still_broken, still_broken + "\n", still_broken + "\n\n"])
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed"
    assert run.outcome == "gave_up"
    assert h.github.prs == []
    assert len(h.github.deleted) == 1
    fixer_calls = [m for m, tools in h.main.calls if not tools]
    assert len(fixer_calls) == 3
    # Во вторую попытку модели передан лог упавших тестов (петля самопроверки).
    assert "FAILURES" in fixer_calls[1][-1].content
    assert "ZeroDivisionError" in fixer_calls[1][-1].content


async def test_syntax_error_is_caught_before_commit(build_harness):
    broken = FIXED_CART.replace("if not items:", "if not items")
    h = await build_harness(fixes=[broken, FIXED_CART])
    run_id = await h.start()

    assert await h.status(run_id) == "awaiting_approval"
    # Сломанная версия не коммитилась: в ветке ровно один коммит.
    assert [s for s in h.github.commits if s.startswith("commit")] == ["commit1"]
    run = await h.c.store.get_run(run_id)
    assert run.approval["attempt"] == 2
    assert any("синтаксическая ошибка" in e.message for e in run.events)


async def test_agent_replans_after_tool_error(build_harness):
    investigation = [
        tool_call("read_file", {"path": "src/cart.py"}, "c1"),
        tool_call("read_file", {"path": "app/cart.py"}, "c2"),
        tool_call(
            "submit_finding",
            {"file_path": "cart.py", "root_cause": "Деление на ноль.", "fix_plan": "Проверка."},
            "c3",
        ),
    ]
    main = FakeChat(make_main_responder([FIXED_CART], investigation))
    h = await build_harness(main=main)
    run_id = await h.start()

    assert await h.status(run_id) == "awaiting_approval"
    agent_calls = [m for m, tools in main.calls if tools]
    first_tool_result = [m for m in agent_calls[1] if isinstance(m, ToolMessage)][0]
    assert first_tool_result.content.startswith("ERROR")
    assert "app/cart.py" in first_tool_result.content  # подсказка похожего файла
    run = await h.c.store.get_run(run_id)
    assert run.approval["file_path"] == "app/cart.py"  # путь нормализован по дереву
    assert any(e.level == "warning" and "меняет план" in e.message for e in run.events)


async def test_fallback_model_is_used_when_primary_fails(build_harness):
    failing = FakeChat(lambda m, t: "", fail=RuntimeError("tool_use_failed"))
    fallback = FakeChat(make_main_responder([FIXED_CART]))
    h = await build_harness(main=failing, fallback=fallback)
    run_id = await h.start()

    assert await h.status(run_id) == "awaiting_approval"
    assert fallback.calls
    run = await h.c.store.get_run(run_id)
    assert any("запасную" in e.message for e in run.events)


async def test_without_reproduction_no_fix_is_offered_and_no_human_is_asked(build_harness):
    from tests.fakes import FakeGitHub

    h = await build_harness(github=FakeGitHub(ci=lambda files: None))
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed"
    assert run.outcome == "not_reproduced"
    assert run.approval is None  # до человека дело не дошло
    assert h.github.prs == [] and list(h.github.branches) == ["main"]  # ни ветки, ни PR
    assert "не удалось воспроизвести" in run.result["title"].lower()


async def test_repository_tree_is_cached_between_runs(build_harness):
    h = await build_harness()
    await h.start()
    await h.start()
    assert h.github.calls.count("get_tree") == 1


async def test_unexpected_error_marks_run_failed(build_harness):
    from tests.fakes import FakeGitHub

    class BrokenGitHub(FakeGitHub):
        async def create_pr(self, *args, **kwargs):
            raise RuntimeError("boom")

    h = await build_harness(github=BrokenGitHub())
    run_id = await h.start()
    await h.decide(run_id, approved=True)

    run = await h.c.store.get_run(run_id)
    assert run.status == "failed"
    assert "boom" in run.error
    assert len(h.github.deleted) == 1  # мусорная ветка удалена


async def test_agent_can_honestly_report_no_defect(build_harness):
    from tests.fakes import FakeChat, make_main_responder, tool_call

    investigation = [
        tool_call("read_file", {"path": "app/cart.py"}, "c1"),
        tool_call(
            "submit_finding",
            {"file_path": "NONE", "root_cause": "Код корректен: цена считается верно.", "fix_plan": "-"},
            "c2",
        ),
    ]
    h = await build_harness(main=FakeChat(make_main_responder([], investigation)))
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed" and run.outcome == "not_reproduced"
    assert run.approval is None and list(h.github.branches) == ["main"]
    assert "Код корректен" in run.result["message"]
