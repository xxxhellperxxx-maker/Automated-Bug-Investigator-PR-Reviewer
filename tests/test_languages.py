"""Профили языков: определение языка и раннера, разбор вывода, размещение регрессионного теста."""

import json

from app.languages import (
    LOG_MARK,
    REPORT_MARK,
    detect_language,
    go_plan,
    javascript_plan,
    parse_go_json,
    parse_jest_json,
    parse_tap,
    plan_from_spec,
    python_plan,
    regression_test_ids,
    regression_test_path,
)
from app.sandbox import TestRun, check_repro_run


def test_detect_language():
    assert detect_language(["go.mod", "calc/calc.go"]) == "go"
    assert detect_language(["package.json", "src/a.js"]) == "javascript"
    assert detect_language(["requirements.txt", "app.py"]) == "python"
    # Файлы из стек-трейса важнее манифестов (монорепозиторий).
    assert detect_language(["package.json", "api/main.go", "go.mod"], ["web/app.ts"]) == "javascript"
    assert detect_language(["README.md"]) is None


def test_javascript_runner_choice():
    assert javascript_plan({"devDependencies": {"jest": "^29"}}).runner == "jest"
    assert javascript_plan({"devDependencies": {"vitest": "^2"}}).runner == "vitest"
    assert javascript_plan({"scripts": {"test": "node --test"}}).runner == "node-test"
    assert javascript_plan({}).runner == "node-test"
    assert javascript_plan({"scripts": {"test": "mocha"}}).runner == "npm-test"


def test_plan_spec_roundtrip():
    for plan in (python_plan(), go_plan(), javascript_plan({"devDependencies": {"jest": "1"}})):
        again = plan_from_spec(plan.spec)
        assert (again.language, again.runner, again.image) == (plan.language, plan.runner, plan.image)


def test_parse_jest_json():
    report = {
        "numTotalTests": 3,
        "numPassedTests": 2,
        "numFailedTests": 1,
        "testResults": [
            {
                "name": "/w/src/cart.test.js",
                "status": "failed",
                "assertionResults": [
                    {"status": "passed", "fullName": "sum ok"},
                    {
                        "status": "failed",
                        "fullName": "average of empty cart",
                        "failureMessages": ["Expected: 0\nReceived: NaN"],
                    },
                ],
            },
            {
                "name": "/w/src/broken.test.js",
                "status": "failed",
                "assertionResults": [],
                "message": "SyntaxError: Unexpected token",
            },
        ],
    }
    out = f"{REPORT_MARK}\n{json.dumps(report)}\n{LOG_MARK}\nlog"
    run = parse_jest_json(out, 1)
    assert run.status == "failed"
    assert run.failed == ["src/broken.test.js::(suite)", "src/cart.test.js::average of empty cart"]
    assert "Received: NaN" in run.output
    ok = parse_jest_json(
        f"{REPORT_MARK}\n"
        + json.dumps({"numTotalTests": 2, "numPassedTests": 2, "testResults": []})
        + f"\n{LOG_MARK}\n",
        0,
    )
    assert ok.status == "passed"
    assert parse_jest_json("no report at all", 1).status == "error"


def test_parse_go_json():
    lines = [
        {"Action": "run", "Package": "ex/calc", "Test": "TestAdd"},
        {"Action": "pass", "Package": "ex/calc", "Test": "TestAdd"},
        {
            "Action": "output",
            "Package": "ex/calc",
            "Test": "TestAvg",
            "Output": "avg_test.go:9: got 0, want 2.5\n",
        },
        {"Action": "fail", "Package": "ex/calc", "Test": "TestAvg"},
        {"Action": "fail", "Package": "ex/calc"},
        {"Action": "fail", "Package": "ex/broken"},
    ]
    run = parse_go_json("\n".join(json.dumps(x) for x in lines), 1)
    assert run.failed == ["ex/broken::(build)", "ex/calc::TestAvg"]
    assert "want 2.5" in run.output and run.summary == "2 failed, 1 passed"
    passed = parse_go_json(json.dumps({"Action": "pass", "Package": "p", "Test": "TestX"}), 0)
    assert passed.status == "passed"


def test_parse_tap():
    out = "TAP version 13\nok 1 - sum works\nnot ok 2 - average of empty cart\n# pass 1\n# fail 1\n"
    run = parse_tap(out, 1)
    assert run.failed == ["node::average of empty cart"] and run.summary == "1 failed, 1 passed"


def test_regression_test_paths():
    tree = ["go.mod", "calc/calc.go", "package.json", "src/cart.js", "src/__tests__/x.spec.ts"]
    assert (
        regression_test_path(go_plan(), tree, "calc/calc.go", "abcdef123")
        == "calc/ai_regression_abcdef12_test.go"
    )
    js = javascript_plan({"devDependencies": {"jest": "1"}})
    assert (
        regression_test_path(js, tree, "src/cart.js", "abcdef123")
        == "src/__tests__/ai_regression_abcdef12.spec.ts"
    )
    assert (
        regression_test_path(js, ["src/cart.mjs"], "src/cart.mjs", "abcdef123")
        == "src/ai_regression_abcdef12.test.mjs"
    )
    assert (
        regression_test_path(python_plan(), ["app.py"], "app.py", "abcdef123")
        == "test_ai_regression_abcdef12.py"
    )


def test_regression_ids_and_repro_check_for_go():
    plan, path = go_plan(), "calc/ai_regression_x_test.go"
    code = "package calc\n\nfunc TestAverageEmpty(t *testing.T) {}\n"
    run = TestRun(
        "failed",
        ["ex/calc::TestAverageEmpty", "ex/calc::TestOld"],
        output="FAILED ex/calc::TestAverageEmpty\n",
    )
    assert regression_test_ids(plan, path, run.failed, code) == ["ex/calc::TestAverageEmpty"]
    assert check_repro_run(run, plan, path, code)[0]
    build = TestRun("failed", ["ex/calc::(build)"], output="undefined: Averag")
    ok, feedback, _ = check_repro_run(build, plan, path, code)
    assert not ok and "could not be compiled" in feedback


def test_repro_check_for_jest_suite_failure():
    plan = javascript_plan({"devDependencies": {"jest": "1"}})
    path = "src/ai_regression_x.test.js"
    run = TestRun(
        "failed", [f"{path}::(suite)"], output=f"FAILED {path}::(suite)\nCannot find module './carts'"
    )
    ok, feedback, _ = check_repro_run(run, plan, path, "test('x', () => {})")
    assert not ok and "could not be compiled" in feedback
