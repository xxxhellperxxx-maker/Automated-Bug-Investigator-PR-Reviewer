import httpx
import pytest

from app.clients.github import GitHubClient, GitHubError
from app.services.ci import evaluate_runs, extract_failure_log
from app.services.fixing import FixFormatError, check_syntax, parse_fix_output
from app.services.security import scan_diff
from app.services.trace_parser import map_frames, normalize_repo, parse_frames, resolve_path
from tests.fakes import BUGGY_CART, FIXED_CART, TRACE, fix_answer

TREE = ["app/__init__.py", "app/cart.py", "tests/test_cart.py"]


class TestTraceParser:
    def test_skips_library_frames(self):
        frames = parse_frames(TRACE)
        assert [(f.path, f.line, f.function) for f in frames] == [
            ("/home/runner/work/shop/shop/app/cart.py", 3, "average_price")
        ]

    def test_generic_frames(self):
        frames = parse_frames("TypeError: x\n    at render (src/components/Cart.vue:42)")
        assert frames[0].path == "src/components/Cart.vue" and frames[0].line == 42

    def test_maps_absolute_paths_to_tree(self):
        assert map_frames(parse_frames(TRACE), TREE) == [
            {"path": "app/cart.py", "line": 3, "function": "average_price"}
        ]

    def test_resolve_path(self):
        assert resolve_path("./app/cart.py", TREE) == "app/cart.py"
        assert resolve_path("cart.py", TREE) == "app/cart.py"
        assert resolve_path("../etc/passwd", TREE) is None
        assert resolve_path("nope.py", TREE) is None

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("acme/shop", "acme/shop"),
            ("https://github.com/acme/shop.git", "acme/shop"),
            (" acme/shop/ ", "acme/shop"),
        ],
    )
    def test_normalize_repo(self, value, expected):
        assert normalize_repo(value) == expected

    @pytest.mark.parametrize("value", ["acme", "acme/shop/extra", "../x/y", "a b/c"])
    def test_normalize_repo_rejects(self, value):
        with pytest.raises(ValueError):
            normalize_repo(value)


class TestCi:
    run = {"id": 1, "name": "tests", "event": "push", "status": "completed", "conclusion": "success"}

    def test_statuses(self):
        kw = {"no_runs_grace": 90, "timeout": 600}
        assert evaluate_runs([], 10, **kw).status == "pending"
        assert evaluate_runs([], 100, **kw).status == "none"
        assert evaluate_runs([self.run], 10, **kw).status == "passed"
        assert evaluate_runs([self.run | {"conclusion": "failure"}], 10, **kw).failed_run_ids == [1]
        assert evaluate_runs([self.run | {"status": "in_progress"}], 10, **kw).status == "pending"
        assert evaluate_runs([self.run | {"status": "in_progress"}], 700, **kw).status == "timeout"

    def test_prefers_pull_request_runs(self):
        kw = {"no_runs_grace": 90, "timeout": 600, "prefer_event": "pull_request"}
        pr_run = self.run | {"id": 2, "event": "pull_request", "conclusion": "failure"}
        assert evaluate_runs([self.run, pr_run], 10, **kw).status == "failed"
        assert evaluate_runs([self.run], 10, **kw).status == "pending"
        result = evaluate_runs([self.run], 100, **kw)
        assert result.status == "passed" and result.note

    def test_extract_failure_log(self):
        raw = "2026-01-01T00:00:00.1Z setup\n2026-01-01T00:00:01.1Z ===== FAILURES =====\nE assert 1 == 2"
        assert extract_failure_log(raw).startswith("= FAILURES =")


class TestFixing:
    def test_parse_and_diff(self):
        proposal = parse_fix_output(fix_answer(FIXED_CART), BUGGY_CART, "app/cart.py")
        assert proposal.title.startswith("fix:")
        assert proposal.explanation.startswith("Деление")
        assert proposal.content == FIXED_CART
        assert "+    if not items:" in proposal.diff

    def test_strips_markdown_fences(self):
        answer = fix_answer("```python\n" + FIXED_CART + "```\n")
        assert parse_fix_output(answer, BUGGY_CART, "app/cart.py").content == FIXED_CART

    @pytest.mark.parametrize("answer", ["no markers", fix_answer(BUGGY_CART), fix_answer("x = 1\n")])
    def test_rejects_bad_answers(self, answer):
        with pytest.raises(FixFormatError):
            parse_fix_output(answer, BUGGY_CART * 3, "app/cart.py")

    def test_check_syntax(self):
        assert check_syntax("a.py", FIXED_CART) is None
        assert "SyntaxError" in check_syntax("a.py", "def f(:\n")
        assert check_syntax("a.json", "{") is not None
        assert check_syntax("a.txt", "{") is None


def test_security_scan_only_added_lines():
    diff = (
        "--- a/x.py\n+++ b/x.py\n-eval(old)\n+result = eval(user_input)\n+API_KEY = 'abcdef123456'\n+ok = 1"
    )
    findings = scan_diff(diff)
    assert len(findings) == 2
    assert findings[0].startswith("A03") and findings[1].startswith("A07")


async def test_github_client_retries_and_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/acme/shop" and len(calls) == 1:
            return httpx.Response(502)
        if request.url.path == "/repos/acme/shop":
            assert request.headers["Authorization"] == "Bearer t"
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(404, json={"message": "Not Found"})

    gh = GitHubClient("t", transport=httpx.MockTransport(handler), backoff=0)
    assert (await gh.get_repo("acme/shop"))["default_branch"] == "main"
    assert len(calls) == 2
    with pytest.raises(GitHubError) as exc:
        await gh.get_file("acme/shop", "missing.py", "main")
    assert exc.value.status == 404
    await gh.aclose()


def test_reasoning_block_is_stripped_from_model_text():
    from langchain_core.messages import AIMessage

    from app.agent.graph import _text

    assert (
        _text(AIMessage(content="<think>hmm VERDICT: REJECT</think>\nVERDICT: APPROVE")) == "VERDICT: APPROVE"
    )


async def test_telegram_client_sends_long_poll_timeout_as_parameter():
    import json as _json

    from app.clients.telegram import TelegramClient, TelegramError

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content or b"{}")
        seen.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("/sendMessage") and body.get("chat_id") == "bad":
            return httpx.Response(400, json={"ok": False, "description": "chat not found"})
        return httpx.Response(
            200, json={"ok": True, "result": [] if "getUpdates" in request.url.path else {}}
        )

    tg = TelegramClient("1:abc", transport=httpx.MockTransport(handler))
    assert await tg.get_updates(offset=5, timeout=25) == []
    assert seen[-1] == (
        "getUpdates",
        {"timeout": 25, "allowed_updates": ["callback_query", "message"], "offset": 5},
    )
    await tg.send_message("1", "<b>hi</b>", buttons=[[("OK", "approve:x")]])
    assert seen[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve:x"
    await tg.answer_callback("cb", "ok")
    assert seen[-1] == ("answerCallbackQuery", {"callback_query_id": "cb", "text": "ok"})
    with pytest.raises(TelegramError, match="chat not found"):
        await tg.send_message("bad", "x")
    await tg.aclose()
