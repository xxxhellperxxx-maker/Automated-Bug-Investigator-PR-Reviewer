"""Честная проверка: воспроизведение бага тестами до исправления и сравнение после."""

import io
import tarfile

import pytest

from app.sandbox import compare_runs, has_python_tests, parse_pytest, repack_github_tarball
from tests.fakes import BUGGY_CART, FIXED_CART, REPO_FILES, FakeGitHub, FakeSandbox

NO_CI_FILES = {k: v for k, v in REPO_FILES.items() if not k.startswith(".github/")}


async def test_bug_is_reproduced_then_fix_verified_in_sandbox(build_harness):
    sandbox = FakeSandbox()
    h = await build_harness(github=FakeGitHub(files=NO_CI_FILES), sandbox=sandbox)
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "awaiting_approval"
    assert run.approval["reproduction"].startswith("ошибка воспроизведена тестами: падали 1")
    assert run.approval["ci_label"].startswith("баг исправлен: падавших тестов было 1, все проходят")
    # Улика из песочницы попала в промпт следователя.
    investigator_prompt = [m for m, tools in h.main.calls if tools][0][1].content
    assert "tests/test_cart.py::test_empty" in investigator_prompt
    # Песочница получила исходный код (с багом), затем код из ветки агента (с исправлением).
    assert "if not items" not in sandbox.runs[0]["app/cart.py"]
    assert "if not items" in sandbox.runs[1]["app/cart.py"]
    assert any("Ошибка воспроизведена" in e.message for e in run.events)

    await h.decide(run_id, approved=True)
    run = await h.c.store.get_run(run_id)
    assert run.outcome == "pr_ok"
    # Без GitHub Actions проверка после PR тоже идёт в песочнице.
    assert len(sandbox.runs) == 3
    assert "песочнице" in h.github.comments[0][1]


async def test_new_failure_after_fix_is_caught_and_retried(build_harness):
    breaking = FIXED_CART.replace("return 0", "return 0  # BROKEN")
    h = await build_harness(
        github=FakeGitHub(files=NO_CI_FILES), sandbox=FakeSandbox(), fixes=[breaking, FIXED_CART]
    )
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "awaiting_approval"
    assert run.approval["attempt"] == 2
    fixer_calls = [m for m, tools in h.main.calls if not tools]
    assert "test_average" in fixer_calls[1][-1].content  # лог нового падения передан модели
    assert any("новые падения" in e.message for e in run.events)


async def test_agent_writes_regression_test_when_tests_do_not_catch_bug(build_harness):
    from tests.fakes import regression_only_rules

    sandbox = FakeSandbox(rules=regression_only_rules)
    h = await build_harness(github=FakeGitHub(files=NO_CI_FILES), sandbox=sandbox)
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "awaiting_approval"
    test_path = f"tests/test_ai_regression_{run_id[:8]}.py"
    assert test_path in run.approval["reproduction"]
    assert run.approval["ci_label"].startswith("баг исправлен: регрессионный тест теперь проходит")
    assert f"+++ b/{test_path}" in run.approval["diff"]
    # Тест закоммичен в ветку агента вместе с исправлением; фиксер видел его в промпте.
    branch = [b for b in h.github.branches if b != "main"][0]
    assert test_path in h.github.branches[branch]["files"]
    fixer_prompt = [m for m, tools in h.main.calls if not tools and "PROVE it" not in m[0].content][0][-1]
    assert "average_price([]) == 0" in fixer_prompt.content

    await h.decide(run_id, approved=True)
    assert test_path in h.github.prs[0]["body"]


async def test_bad_regression_test_means_not_reproduced(build_harness):
    from tests.fakes import FIXED_CART, FakeChat, make_main_responder

    passing_test = "def test_nothing():\n    assert True\n"
    main = FakeChat(make_main_responder([FIXED_CART], repro=[passing_test, passing_test]))
    h = await build_harness(
        github=FakeGitHub(files=NO_CI_FILES), sandbox=FakeSandbox(rules=lambda f: []), main=main
    )
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed" and run.outcome == "not_reproduced"
    assert run.approval is None and h.github.prs == []
    assert sum("не воспроизводит" in e.message for e in run.events) == 2


def test_parse_pytest_output():
    out = (
        "FAILED tests/test_a.py::test_x - KeyError: 'discount'\n"
        "ERROR tests/test_b.py\n2 failed, 5 passed in 0.04s"
    )
    run = parse_pytest(out, 1)
    assert run.status == "failed"
    assert run.failed == ["tests/test_a.py::test_x", "tests/test_b.py"]
    assert run.summary == "2 failed, 5 passed"
    assert parse_pytest("7 passed in 0.01s", 0).status == "passed"
    assert parse_pytest("no tests ran in 0.01s", 5).status == "no_tests"


def test_compare_runs_verdicts():
    from app.sandbox import TestRun

    base = {"status": "failed", "failed": ["t::a", "t::b"], "summary": "2 failed"}
    ok = compare_runs(base, TestRun("passed", [], "7 passed"))
    assert ok["ok"] and ok["fixed"] == ["t::a", "t::b"]
    new = compare_runs(base, TestRun("failed", ["t::c"], "1 failed"))
    assert not new["ok"] and new["new_failures"] == ["t::c"]
    same = compare_runs(base, TestRun("failed", ["t::a", "t::b"], "2 failed"))
    assert not same["verified"] and "не исправлен" in same["label"]
    partial = compare_runs(base, TestRun("failed", ["t::b"], "1 failed"))
    assert not partial["verified"], "частичное исправление не считается подтверждённым"
    targeted = compare_runs(base, TestRun("failed", ["t::b"], "1 failed"), target=["t::a"])
    assert targeted["verified"] and "ещё падают" in targeted["label"]
    assert not compare_runs({"failed": []}, TestRun("passed", [], "3 passed"))["verified"]


def test_repack_strips_top_dir_and_unsafe_members():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("o-r-sha/app/x.py", b"x = 1\n"), ("o-r-sha/../evil.py", b"bad")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("o-r-sha/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with tarfile.open(fileobj=io.BytesIO(repack_github_tarball(buf.getvalue()))) as out:
        assert out.getnames() == ["app/x.py"]


def test_has_python_tests():
    assert has_python_tests(["app/x.py", "tests/test_x.py"])
    assert not has_python_tests(["app/x.py", "README.md"])


@pytest.mark.parametrize("cart", [BUGGY_CART])
def test_fake_sandbox_rules_match_bug(cart):
    from tests.fakes import sandbox_rules

    assert sandbox_rules({"app/cart.py": cart}) == ["tests/test_cart.py::test_empty"]


def test_check_repro_run():
    from app.languages import python_plan
    from app.sandbox import TestRun, check_repro_run

    plan, path, code = python_plan(), "tests/test_ai_regression_x.py", "def test_a():\n    assert f() == 1\n"
    good = TestRun("failed", [f"{path}::test_a"], output=f"FAILED {path}::test_a - assert 1 == 0")
    assert check_repro_run(good, plan, path, code)[:2] == (True, "")
    assert not check_repro_run(TestRun("passed", []), plan, path, code)[0]
    bad_import = TestRun(
        "failed",
        [f"{path}::test_a"],
        output=f"FAILED {path}::test_a - ModuleNotFoundError: No module named 'x'",
    )
    assert "mistake in the test" in check_repro_run(bad_import, plan, path, code)[1]
    assert not check_repro_run(TestRun("failed", [path]), plan, path, code)[0]


def test_add_files_to_tar():
    import io as _io
    import tarfile as _tarfile

    from app.sandbox import add_files_to_tar

    buf = _io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"x = 1\n"
        info = _tarfile.TarInfo("app/x.py")
        info.size = len(data)
        tar.addfile(info, _io.BytesIO(data))
    out = add_files_to_tar(buf.getvalue(), {"tests/test_new.py": "def test_x():\n    pass\n"})
    with _tarfile.open(fileobj=_io.BytesIO(out)) as tar:
        assert sorted(tar.getnames()) == ["app/x.py", "tests/test_new.py"]


async def test_build_failure_before_fix_is_not_counted_as_reproduction(build_harness):
    sandbox = FakeSandbox(rules=lambda files: ["example.com/shop/calc::(build)"])
    h = await build_harness(github=FakeGitHub(files=NO_CI_FILES), sandbox=sandbox)
    run_id = await h.start()

    run = await h.c.store.get_run(run_id)
    assert run.outcome == "not_reproduced" and run.approval is None
    assert any("не запускаются в песочнице" in e.message for e in run.events)
    fixer_calls = [m for m, tools in h.main.calls if not tools and "PROVE it" not in m[0].content]
    assert fixer_calls == [], "попытки исправления не тратятся на проблему окружения"
