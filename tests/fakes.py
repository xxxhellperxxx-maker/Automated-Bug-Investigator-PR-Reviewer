"""Тестовые двойники внешних сервисов: GitHub, LLM, Telegram. Без сети и ключей."""

import asyncio
import hashlib
import io
import tarfile
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.clients.github import GitHubError

BUGGY_CART = """def average_price(items):
    total = sum(item["price"] for item in items)
    return total / len(items)
"""

FIXED_CART = """def average_price(items):
    if not items:
        return 0
    total = sum(item["price"] for item in items)
    return total / len(items)
"""

TRACE = """Traceback (most recent call last):
  File "/home/runner/work/shop/shop/app/cart.py", line 3, in average_price
    return total / len(items)
  File "/usr/lib/python3.12/site-packages/pluggy/_hooks.py", line 10, in __call__
ZeroDivisionError: division by zero"""

REPO_FILES = {
    ".github/workflows/tests.yml": "on: [push, pull_request]\n",
    "app/__init__.py": "",
    "app/cart.py": BUGGY_CART,
    "tests/test_cart.py": (
        "from app.cart import average_price\n\ndef test_empty():\n    assert average_price([]) == 0\n"
    ),
}


def _blob(content: str) -> str:
    return hashlib.sha1(content.encode(), usedforsecurity=False).hexdigest()[:10]


def fixed_ci(files: dict[str, str]) -> str | None:
    return "success" if "if not items" in files.get("app/cart.py", "") else "failure"


class FakeGitHub:
    def __init__(
        self, files: dict[str, str] | None = None, ci: Callable[[dict[str, str]], str | None] = fixed_ci
    ) -> None:
        self.branches: dict[str, dict[str, Any]] = {
            "main": {"sha": "base0", "files": dict(files or REPO_FILES)}
        }
        self.commits: dict[str, dict[str, str]] = {"base0": dict(files or REPO_FILES)}
        self.ci = ci
        self.prs: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.deleted: list[str] = []
        self.issues: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._n = 0

    async def get_repo(self, repo: str) -> dict[str, Any]:
        self.calls.append("get_repo")
        return {"default_branch": "main", "full_name": repo}

    async def get_branch(self, repo: str, branch: str) -> dict[str, Any]:
        return {"commit": {"sha": self.branches[branch]["sha"]}}

    async def get_tree(self, repo: str, sha: str) -> list[str]:
        self.calls.append("get_tree")
        return [*self.commits[sha], "docs/logo.png"]

    async def get_file(self, repo: str, path: str, ref: str) -> tuple[str, str]:
        files = self.branches[ref]["files"]
        if path not in files:
            raise GitHubError(404, "Not Found", path)
        return files[path], _blob(files[path])

    async def get_tarball(self, repo: str, ref: str) -> bytes:
        """Настоящий tar.gz с каталогом owner-repo-sha/, как у GitHub."""
        files = self.commits.get(ref) or self.branches[ref]["files"]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo(f"acme-shop-{ref}/{path}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    async def search_code(self, repo: str, query: str, limit: int = 5) -> list[str]:
        return [p for p, c in self.branches["main"]["files"].items() if query in c][:limit]

    async def create_branch(self, repo: str, branch: str, sha: str) -> None:
        if branch in self.branches:
            raise GitHubError(422, "Reference already exists", branch)
        self.branches[branch] = {"sha": sha, "files": dict(self.commits[sha])}

    async def delete_branch(self, repo: str, branch: str) -> None:
        if branch not in self.branches:
            raise GitHubError(422, "Reference does not exist", branch)
        self.deleted.append(branch)
        del self.branches[branch]

    async def put_file(
        self, repo: str, path: str, *, content: str, sha: str | None, branch: str, message: str
    ) -> dict[str, str]:
        files = self.branches[branch]["files"]
        if sha is None and path in files:
            raise GitHubError(422, "sha wasn't supplied", path)
        if sha is not None and (path not in files or _blob(files[path]) != sha):
            raise GitHubError(409, "sha does not match", path)
        self._n += 1
        files[path] = content
        commit_sha = f"commit{self._n}"
        self.commits[commit_sha] = dict(files)
        self.branches[branch]["sha"] = commit_sha
        return {"commit_sha": commit_sha, "file_sha": _blob(content)}

    async def list_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        files = self.commits.get(head_sha)
        conclusion = self.ci(files) if files is not None else None
        if conclusion is None:
            return []
        suffix = head_sha.removeprefix("commit")
        run_id = int(suffix) if suffix.isdigit() else 0
        runs = [
            {
                "id": run_id,
                "name": "tests",
                "event": "push",
                "status": "completed",
                "conclusion": conclusion,
                "html_url": f"https://ci/{run_id}",
            }
        ]
        if any(pr["head_sha"] == head_sha for pr in self.prs):
            runs.append(
                {
                    "id": run_id + 1000,
                    "name": "tests",
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": conclusion,
                    "html_url": f"https://ci/{run_id + 1000}",
                }
            )
        return runs

    async def list_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        return [{"id": run_id, "conclusion": "failure"}]

    async def get_job_logs(self, repo: str, job_id: int) -> str:
        return (
            "2026-11-17T10:00:00.0000000Z collected 1 item\n"
            "2026-11-17T10:00:01.0000000Z ===== FAILURES =====\n"
            "2026-11-17T10:00:01.0000000Z E   ZeroDivisionError: division by zero\n"
        )

    async def create_pr(self, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        pr = {
            "number": 7,
            "url": f"https://github.com/{repo}/pull/7",
            "head_sha": self.branches[head]["sha"],
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
        self.prs.append(pr)
        return {k: pr[k] for k in ("number", "url", "head_sha")}

    async def comment_issue(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def list_issues(self, repo: str, *, label: str, since: str) -> list[dict[str, Any]]:
        return self.issues


Responder = Callable[[list[BaseMessage], list | None], AIMessage | str]


class FakeChat:
    """Минимальный двойник чат-модели: bind_tools + ainvoke."""

    def __init__(self, responder: Responder, fail: Exception | None = None) -> None:
        self.responder = responder
        self.fail = fail
        self.calls: list[tuple[list[BaseMessage], list | None]] = []

    def bind_tools(self, tools: list) -> "FakeChat":
        bound = FakeChat(self.responder, self.fail)
        bound.calls = self.calls
        bound._tools = tools
        return bound

    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage:
        tools = getattr(self, "_tools", None)
        self.calls.append((messages, tools))
        if self.fail is not None:
            raise self.fail
        result = self.responder(messages, tools)
        if isinstance(result, str):
            result = AIMessage(content=result)
        result.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        return result


def tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def fix_answer(content: str) -> str:
    return (
        f"TITLE: fix: handle empty cart in average_price\n"
        f"EXPLANATION: Деление на ноль при пустой корзине; добавлена проверка.\n"
        f"===FILE_START===\n{content}===FILE_END==="
    )


REPRO_TEST = """from app.cart import average_price


def test_empty_cart_average_is_zero():
    assert average_price([]) == 0
"""


def repro_answer(content: str = REPRO_TEST) -> str:
    return f"EXPLANATION: Проверяет среднюю цену пустой корзины.\n===FILE_START===\n{content}===FILE_END==="


def make_main_responder(
    fixes: list[str], investigation: list[AIMessage] | None = None, repro: list[str] | None = None
) -> Responder:
    """Расследование по сценарию investigation, затем исправления из fixes по порядку."""
    steps = investigation or [
        tool_call("read_file", {"path": "app/cart.py"}, "c1"),
        tool_call(
            "submit_finding",
            {
                "file_path": "app/cart.py",
                "root_cause": "Пустой список даёт деление на ноль.",
                "fix_plan": "Вернуть 0 для пустого списка.",
            },
            "c2",
        ),
    ]
    fix_iter = iter(fixes)
    repro_iter = iter(repro or [REPRO_TEST, REPRO_TEST])

    def respond(messages: list[BaseMessage], tools: list | None) -> AIMessage | str:
        if "PROVE it" in str(messages[0].content):
            return repro_answer(next(repro_iter))
        if tools:
            done = sum(1 for m in messages if isinstance(m, ToolMessage))
            return steps[min(done, len(steps) - 1)]
        return fix_answer(next(fix_iter))

    return respond


def critic_responder(messages: list[BaseMessage], tools: list | None) -> str:
    return (
        "VERDICT: APPROVE\n## Проблема\nДеление на ноль.\n## Причина\nНет проверки.\n"
        "## Что изменено\nПроверка."
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 5.0)
        await asyncio.sleep(0)


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def send_message(self, chat_id: str, html: str, buttons=None) -> dict[str, Any]:
        self.sent.append({"chat_id": chat_id, "html": html, "buttons": buttons})
        return {"message_id": len(self.sent)}

    async def edit_text(self, chat_id: str, message_id: int, html: str) -> None:
        self.edits.append(html)

    async def answer_callback(self, callback_id: str, text: str) -> None:
        self.answers.append(text)

    async def get_me(self) -> dict[str, Any]:
        return {"username": "test_bot"}

    async def get_updates(self, offset, timeout: int = 25) -> list:
        await asyncio.sleep(3600)
        return []

    async def aclose(self) -> None:
        pass


def sandbox_rules(files: dict[str, str]) -> list[str]:
    """Какие тесты падают на этой версии кода (имитация pytest)."""
    failed = []
    cart = files.get("app/cart.py", "")
    if "if not items" not in cart:
        failed.append("tests/test_cart.py::test_empty")
    if "BROKEN" in cart:
        failed.append("tests/test_cart.py::test_average")
    return failed


def regression_only_rules(files: dict[str, str]) -> list[str]:
    """Существующие тесты баг не ловят; падает только регрессионный тест агента, пока баг на месте."""
    bug = "if not items" not in files.get("app/cart.py", "")
    return [
        f"{path}::test_empty_cart_average_is_zero"
        for path, code in files.items()
        if "test_ai_regression" in path and "average_price([]) == 0" in code and bug
    ]


class FakeSandbox:
    """Двойник DockerSandbox: распаковывает присланный архив и «запускает» тесты по правилам."""

    def __init__(self, rules: Callable[[dict[str, str]], list[str]] = sandbox_rules) -> None:
        self.rules = rules
        self.runs: list[dict[str, str]] = []
        self.plans: list = []

    async def run_tests(self, source_tar: bytes, plan=None):
        from app.sandbox_types import TestRun

        with tarfile.open(fileobj=io.BytesIO(source_tar)) as tar:
            files = {m.name: tar.extractfile(m).read().decode() for m in tar.getmembers() if m.isfile()}
        self.runs.append(files)
        self.plans.append(plan)
        failed = self.rules(files)
        total = 3
        if failed:
            summary = f"{len(failed)} failed, {total - len(failed)} passed"
            output = "\n".join(f"FAILED {f} - ZeroDivisionError: division by zero" for f in failed)
            return TestRun("failed", failed, summary, output + f"\n{summary} in 0.1s", 1)
        return TestRun("passed", [], f"{total} passed", f"{total} passed in 0.1s", 0)
