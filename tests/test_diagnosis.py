"""Режим без репозитория: диагноз по тексту ошибки."""

import json

import httpx

from app.main import create_app
from app.schemas import Incident
from app.services.diagnosis import error_type, parse_diagnosis, raise_site, trace_facts
from app.services.trace_parser import parse_frames
from tests.fakes import TRACE, FakeChat, FakeGitHub

PY_TRACE = """Traceback (most recent call last):
  File "/srv/shop/shop/orders.py", line 15, in order_total
    subtotal = order_subtotal(lines)
  File "/srv/shop/shop/pricing.py", line 7, in unit_price
    discount = product["discount"]
KeyError: 'discount'"""

JS_TRACE = """TypeError: Cannot read properties of undefined (reading 'price')
    at averagePrice (src/cart.js:3:16)
    at Object.<anonymous> (tests/cart.test.js:8:30)"""

GO_TRACE = """panic: runtime error: index out of range [3] with length 3

goroutine 1 [running]:
main.lookup(...)
        /app/internal/store/store.go:42
main.main()
        /app/cmd/server/main.go:17 +0x1d"""

ANSWER = {
    "summary": "Обращение к отсутствующему ключу discount.",
    "location": {"file": "shop/pricing.py", "line": 7, "function": "unit_price"},
    "cause": "У товара нет поля discount, а код читает его напрямую.",
    "fix_steps": ["Использовать product.get('discount', 0)", "Добавить тест для товара без скидки"],
    "code_fix": {
        "before": 'discount = product["discount"]',
        "after": 'discount = product.get("discount", 0)',
    },
    "confidence": "high",
    "how_to_verify": "Тест: unit_price('book') == 500",
    "need_more_info": "",
}


def diagnose_responder(messages, tools):
    assert "do NOT have access to the repository" in messages[0].content
    return "```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"


def test_trace_facts_for_three_languages():
    py = trace_facts(parse_frames(PY_TRACE), PY_TRACE)
    assert py["raise_site"]["path"].endswith("shop/pricing.py") and py["raise_site"]["line"] == 7
    assert (py["error_type"], py["error_message"]) == ("KeyError", "'discount'")
    assert py["language"] == "Python" and len(py["frames"]) == 2

    js = trace_facts(parse_frames(JS_TRACE), JS_TRACE)
    assert js["raise_site"]["path"] == "src/cart.js" and js["raise_site"]["line"] == 3
    assert js["error_type"] == "TypeError" and js["language"] == "JavaScript"

    go = trace_facts(parse_frames(GO_TRACE), GO_TRACE)
    assert go["raise_site"]["path"].endswith("internal/store/store.go") and go["error_type"] == "panic"
    assert go["language"] == "Go"

    plain = trace_facts([], "Заказ с книгой не считается")
    assert plain["raise_site"] is None and plain["error_type"] == ""
    assert raise_site([], "") is None and error_type("no error here") == ("", "")


def test_parse_diagnosis_tolerates_wrapping_and_rejects_garbage():
    parsed = parse_diagnosis("Вот разбор:\n" + json.dumps(ANSWER) + "\nНадеюсь, помог.")
    assert parsed["location"]["line"] == 7 and parsed["confidence"] == "high"
    assert len(parsed["fix_steps"]) == 2
    weird = dict(ANSWER, confidence="очень", location="pricing.py", fix_steps="одной строкой")
    parsed = parse_diagnosis(json.dumps(weird))
    assert parsed["confidence"] == "low" and parsed["location"]["file"] == "" and parsed["fix_steps"] == []
    assert parse_diagnosis("не JSON") is None
    assert parse_diagnosis(json.dumps({"summary": "без причины"})) is None


async def test_run_without_repository_gives_diagnosis_only(build_harness):
    gh = FakeGitHub()
    h = await build_harness(github=gh, main=FakeChat(diagnose_responder))
    run_id = await h.c.runner.start(Incident(repo="", error_text=PY_TRACE))
    await h.c.runner.wait_idle()

    run = await h.c.store.get_run(run_id)
    assert run.status == "completed" and run.outcome == "diagnosed"
    assert run.approval is None and gh.calls == []  # к GitHub не обращались
    d = run.result["diagnosis"]
    assert d["verified"] is False
    assert d["facts"]["raise_site"]["line"] == 7  # факт из трейса, а не от модели
    assert d["hypothesis"]["fix_steps"][0].startswith("Использовать")
    assert "без проверки кодом" in run.result["title"]
    assert "гипотеза" in run.result["message"]
    nodes = {e.node for e in run.events}
    assert {"intake", "diagnose", "report"} <= nodes and "load_repo" not in nodes


async def test_diagnosis_retries_when_model_breaks_format(build_harness):
    answers = iter(["я думаю, дело в скидке", json.dumps(ANSWER)])
    h = await build_harness(main=FakeChat(lambda m, t: next(answers)))
    run_id = await h.c.runner.start(Incident(repo="", error_text=PY_TRACE))
    await h.c.runner.wait_idle()
    run = await h.c.store.get_run(run_id)
    assert run.outcome == "diagnosed" and run.result["diagnosis"]["hypothesis"]["confidence"] == "high"


async def test_api_accepts_error_text_without_repository(build_harness):
    h = await build_harness(main=FakeChat(diagnose_responder))
    app = create_app(h.c, start_background=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/runs", json={"error_text": TRACE})
            assert resp.status_code == 202
            resp2 = await client.post("/api/v1/runs", json={"repo": "  ", "error_text": TRACE})
            assert resp2.status_code == 202
            await h.c.runner.wait_idle()
            run = (await client.get(f"/api/v1/runs/{resp.json()['id']}")).json()
            assert run["repo"] == "" and run["outcome"] == "diagnosed"
