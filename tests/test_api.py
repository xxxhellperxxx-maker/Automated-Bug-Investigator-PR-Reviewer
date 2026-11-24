import hashlib
import hmac
import json

import httpx
import pytest

from app.main import create_app
from tests.fakes import TRACE


@pytest.fixture
async def api(build_harness):
    async def factory(**kw):
        h = await build_harness(**kw)
        app = create_app(h.c, start_background=False)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        clients.append((client, ctx))
        return h, client

    clients: list = []
    yield factory
    for client, ctx in clients:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def test_manual_run_full_flow_with_web_approval(api):
    h, client = await api()
    resp = await client.post(
        "/api/v1/runs",
        json={"repo": "https://github.com/acme/shop.git", "error_text": TRACE, "telegram_chat_id": ""},
    )
    assert resp.status_code == 202
    run_id = resp.json()["id"]
    await h.c.runner.wait_idle()

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["repo"] == "acme/shop"
    assert run["status"] == "awaiting_approval"
    assert run["channel"] == "web"
    assert run["approval"]["diff"]

    resp = await client.post(f"/api/v1/runs/{run_id}/decision", json={"approved": True})
    assert resp.status_code == 202
    await h.c.runner.wait_idle()

    run = (await client.get(f"/api/v1/runs/{run_id}")).json()
    assert run["status"] == "completed"
    assert run["pr_url"].endswith("/pull/7")
    assert run["result"]["title"] == "Pull Request создан и проверен"

    # Повторное решение отклоняется: защита от двойного клика.
    resp = await client.post(f"/api/v1/runs/{run_id}/decision", json={"approved": False})
    assert resp.status_code == 409

    runs = (await client.get("/api/v1/runs")).json()
    assert runs[0]["id"] == run_id


async def test_validation_errors(api):
    _, client = await api()
    resp = await client.post("/api/v1/runs", json={"repo": "not a repo", "error_text": TRACE})
    assert resp.status_code == 422
    resp = await client.post(
        "/api/v1/runs", json={"repo": "a/b", "error_text": TRACE, "telegram_chat_id": "abc"}
    )
    assert resp.status_code == 422
    assert (await client.get("/api/v1/runs/missing")).status_code == 404


async def test_pages_and_health(api):
    _, client = await api()
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    page = await client.get("/")
    assert page.status_code == 200 and 'data-testid="run-form"' in page.text
    assert (await client.get("/runs/abc")).status_code == 200
    assert (await client.get("/static/run.js")).status_code == 200


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_github_webhook_checks_signature_and_starts_run(api):
    h, client = await api(github_webhook_secret="s3cret", watched_repos="acme/shop")
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/shop"},
        "issue": {"number": 12, "title": "Crash on empty cart", "body": TRACE, "labels": [{"name": "bug"}]},
    }
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}

    bad = await client.post(
        "/api/v1/webhooks/github", content=body, headers=headers | {"X-Hub-Signature-256": "sha256=0"}
    )
    assert bad.status_code == 401

    ok = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers=headers | {"X-Hub-Signature-256": _sign("s3cret", body)},
    )
    assert ok.status_code == 202 and ok.json()["status"] == "started"
    again = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers=headers | {"X-Hub-Signature-256": _sign("s3cret", body)},
    )
    assert again.json()["reason"] == "already processed"

    await h.c.runner.wait_idle()
    run = await h.c.store.get_run(ok.json()["id"])
    assert run.source == "issue" and run.issue_number == 12
    assert run.status == "awaiting_approval"


async def test_webhook_disabled_without_secret(api):
    _, client = await api()
    resp = await client.post("/api/v1/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "issues"})
    assert resp.status_code == 503


async def test_status_endpoint_reports_configuration(api):
    _, client = await api(watched_repos="acme/shop")
    status = (await client.get("/api/v1/status")).json()
    assert status["groq"] is True and status["telegram"] is False
    assert status["watched_repos"] == ["acme/shop"]
    assert "test" not in str(status.values()), "значения ключей не должны отдаваться"


async def test_missing_groq_key_does_not_crash_startup(tmp_path):
    from app.agent.llm import MissingKeyLLM, build_llms
    from tests.conftest import make_settings

    llms = build_llms(make_settings(tmp_path, groq_api_key=""))
    assert isinstance(llms.main, MissingKeyLLM)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        await llms.main.bind_tools([]).ainvoke([])


async def test_unknown_telegram_username_is_rejected_with_hint(api):
    _, client = await api()
    resp = await client.post(
        "/api/v1/runs", json={"repo": "acme/shop", "error_text": TRACE, "telegram_chat_id": "@stranger"}
    )
    assert resp.status_code == 422
    assert "/start" in resp.json()["detail"]
