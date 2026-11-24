"""Профили языков для песочницы: как установить зависимости, запустить тесты и понять, что упало.

Логика агента (воспроизвести → исправить → проверить тем же фактом) от языка не зависит.
Здесь — только то, что отличается: образ, команды, разбор вывода тест-раннера в единый формат
«список упавших тестов» и правила, куда положить регрессионный тест.

Поддерживаются: Python (pytest), JavaScript/TypeScript (Jest, Vitest, node --test, иначе npm test),
Go (go test). Идентификатор упавшего теста — «<файл или пакет>::<имя теста>».
"""

import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.sandbox_types import TestRun

REPORT_MARK = "@@AIBI_REPORT@@"
LOG_MARK = "@@AIBI_LOG@@"


@dataclass(frozen=True)
class TestPlan:
    language: str  # python | javascript | go
    runner: str  # pytest | jest | vitest | node-test | npm-test | go-test
    image: str
    install_cmd: str
    test_cmd: str
    install_env: tuple[str, ...]
    test_env: tuple[str, ...]
    framework_hint: str  # для промпта регрессионного теста
    test_file_re: re.Pattern[str] = field(compare=False)

    test_script: str = ""

    @property
    def spec(self) -> dict[str, str]:
        """Сериализуемое описание для состояния графа (сам TestPlan в чекпойнт не кладём)."""
        return {
            "language": self.language,
            "runner": self.runner,
            "test_script": self.test_script,
            "image": self.image,
        }

    def has_tests(self, tree: list[str]) -> bool:
        return any(self.test_file_re.search(p) for p in tree)

    def parse(self, output: str, exit_code: int) -> TestRun:
        return PARSERS[self.runner](output, exit_code)


# ----------------------------------------------------------------------------- парсеры

RESULT_LINE_RE = re.compile(r"^(FAILED|ERROR) (\S+)", re.M)
SUMMARY_RE = re.compile(r"^=*\s*(\d+ (?:passed|failed|error).*?) in [\d.]+s", re.M)


def parse_pytest(output: str, exit_code: int) -> TestRun:
    """Коды pytest: 0 — всё прошло, 1 — есть падения, 2 — прерван (ошибки сбора), 5 — тестов нет."""
    failed = sorted({m.group(2) for m in RESULT_LINE_RE.finditer(output)})
    summary_m = SUMMARY_RE.findall(output)
    summary = summary_m[-1] if summary_m else ""
    tail = output[-4000:]
    if exit_code == 0:
        return TestRun("passed", [], summary, tail, exit_code)
    if exit_code in (1, 2) and (failed or exit_code == 1):
        return TestRun("failed", failed or ["(collection error)"], summary, tail, exit_code)
    if exit_code == 5:
        return TestRun("no_tests", [], summary or "no tests collected", tail, exit_code)
    return TestRun("error", failed, summary, tail, exit_code)


def _split_report(output: str) -> tuple[str, str]:
    if REPORT_MARK not in output:
        return "", output
    _, rest = output.split(REPORT_MARK, 1)
    report, _, log = rest.partition(LOG_MARK)
    return report.strip(), log


def _rel(path: str) -> str:
    return path.removeprefix("/w/").lstrip("/")


def parse_jest_json(output: str, exit_code: int) -> TestRun:
    """Jest и Vitest умеют одинаковый JSON-отчёт (--json / --reporter=json)."""
    report_text, log = _split_report(output)
    try:
        report = json.loads(report_text)
    except ValueError:
        status = "passed" if exit_code == 0 else "error"
        return TestRun(status, [], "тест-раннер не выдал отчёт", log[-3000:], exit_code)
    failed: list[str] = []
    messages: list[str] = []
    for suite in report.get("testResults", []):
        file = _rel(suite.get("name", suite.get("testFilePath", "?")))
        assertions = suite.get("assertionResults", [])
        bad = [a for a in assertions if a.get("status") == "failed"]
        for a in bad:
            failed.append(f"{file}::{a.get('fullName') or a.get('title')}")
            messages.append(
                f"FAILED {file}::{a.get('fullName')}\n" + "\n".join(a.get("failureMessages", []))[:1200]
            )
        if suite.get("status") == "failed" and not bad:
            failed.append(f"{file}::(suite)")  # файл не загрузился: синтаксис, импорт
            messages.append(f"FAILED {file}::(suite)\n{suite.get('message', '')[:1500]}")
    total = report.get("numTotalTests", 0)
    n_failed = report.get("numFailedTests", 0) + report.get("numFailedTestSuites", 0) * (not total)
    summary = (
        f"{len(failed)} failed, {report.get('numPassedTests', 0)} passed" if failed else f"{total} passed"
    )
    text = ("\n\n".join(messages) or log)[-4000:]
    if failed:
        return TestRun("failed", sorted(set(failed)), summary, text, exit_code)
    if total == 0 and not n_failed:
        return TestRun("no_tests", [], "тесты не найдены", text, exit_code)
    return TestRun("passed", [], summary, text, exit_code)


TAP_RE = re.compile(r"^\s*(not ok|ok) \d+ - (.+?)(?:\s+#.*)?$", re.M)


def parse_tap(output: str, exit_code: int) -> TestRun:
    """node --test --test-reporter=tap."""
    failed, passed = [], 0
    for status, name in TAP_RE.findall(output):
        if status == "ok":
            passed += 1
        else:
            failed.append(f"node::{name.strip()}")
    tail = output[-4000:]
    if failed:
        return TestRun(
            "failed", sorted(set(failed)), f"{len(failed)} failed, {passed} passed", tail, exit_code
        )
    if exit_code != 0:
        return TestRun("failed", ["node::(run)"], "node --test завершился с ошибкой", tail, exit_code)
    return TestRun("passed" if passed else "no_tests", [], f"{passed} passed", tail, exit_code)


def parse_exit_code(output: str, exit_code: int) -> TestRun:
    """Неизвестный раннер (npm test): только код возврата — грубо, но честно."""
    tail = output[-4000:]
    if exit_code == 0:
        return TestRun("passed", [], "npm test: ok", tail, exit_code)
    return TestRun("failed", ["npm::test"], "npm test завершился с ошибкой", tail, exit_code)


def parse_go_json(output: str, exit_code: int) -> TestRun:
    """go test -json ./...: события по тестам; падение пакета без упавших тестов — ошибка сборки."""
    failed: list[str] = []
    passed = 0
    test_output: dict[str, list[str]] = {}
    pkg_failed: set[str] = set()
    pkg_with_failed_tests: set[str] = set()
    plain: list[str] = []
    for line in output.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                plain.append(line)
            continue
        pkg, test, action = ev.get("Package", ""), ev.get("Test"), ev.get("Action")
        if test and action == "output":
            test_output.setdefault(f"{pkg}::{test}", []).append(ev.get("Output", ""))
        elif test and action == "fail":
            failed.append(f"{pkg}::{test}")
            pkg_with_failed_tests.add(pkg)
        elif test and action == "pass":
            passed += 1
        elif not test and action == "fail":
            pkg_failed.add(pkg)
        elif not test and action == "output":
            plain.append(ev.get("Output", "").rstrip())
    failed += [f"{pkg}::(build)" for pkg in sorted(pkg_failed - pkg_with_failed_tests)]
    messages = [f"FAILED {t}\n" + "".join(test_output.get(t, []))[-1200:] for t in failed]
    text = ("\n".join(messages) + "\n" + "\n".join(plain[-40:]))[-4000:]
    if failed:
        return TestRun(
            "failed", sorted(set(failed)), f"{len(failed)} failed, {passed} passed", text, exit_code
        )
    if exit_code != 0:
        return TestRun("failed", ["go::(build)"], "go test завершился с ошибкой", text, exit_code)
    return TestRun("passed" if passed else "no_tests", [], f"{passed} passed", text, exit_code)


PARSERS: dict[str, Callable[[str, int], TestRun]] = {
    "pytest": parse_pytest,
    "jest": parse_jest_json,
    "vitest": parse_jest_json,
    "node-test": parse_tap,
    "npm-test": parse_exit_code,
    "go-test": parse_go_json,
}

# ----------------------------------------------------------------------------- профили

PY_TEST_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|conftest\.py|pytest\.ini)$")
JS_TEST_RE = re.compile(r"(^|/)(__tests__/.+|[^/]+\.(test|spec))\.(c|m)?[jt]sx?$")
GO_TEST_RE = re.compile(r"_test\.go$")


def _report_cmd(cmd: str) -> str:
    """Запустить раннер, сохранить JSON-отчёт и код возврата, вывести отчёт и хвост лога с метками."""
    return (
        f"{cmd} >/tmp/aibi.log 2>&1; code=$?; echo '{REPORT_MARK}'; cat /tmp/aibi-report.json 2>/dev/null; "
        f"echo '{LOG_MARK}'; tail -c 4000 /tmp/aibi.log; exit $code"
    )


def python_plan(image: str = "python:3.12-slim") -> TestPlan:
    return TestPlan(
        language="python",
        runner="pytest",
        image=image,
        install_cmd=(
            "set -e; cd /w; req=''; "
            'for f in requirements-dev.txt requirements.txt; do [ -f $f ] && req="-r $f" && break; done; '
            "pip install -q --no-cache-dir --disable-pip-version-check --target /w/.deps $req pytest"
        ),
        test_cmd="python -m pytest -q -rfE --tb=short --color=no -p no:cacheprovider",
        install_env=("HOME=/tmp", "PIP_ROOT_USER_ACTION=ignore"),
        test_env=("HOME=/tmp", "PYTHONPATH=/w/.deps:/w", "PYTHONDONTWRITEBYTECODE=1"),
        framework_hint="pytest (plain test functions with assert; only pytest and the standard library)",
        test_file_re=PY_TEST_RE,
    )


def javascript_runner(package_json: dict) -> str:
    deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}
    test_script = str(package_json.get("scripts", {}).get("test", ""))
    if "vitest" in deps or "vitest" in test_script:
        return "vitest"
    if "jest" in deps or "jest" in test_script:
        return "jest"
    if "node --test" in test_script or not test_script:
        return "node-test"
    return "npm-test"


def javascript_plan(package_json: dict, image: str = "node:22-slim") -> TestPlan:
    test_script = str(package_json.get("scripts", {}).get("test", ""))
    return javascript_plan_for(javascript_runner(package_json), test_script, image)


def javascript_plan_for(runner: str, test_script: str = "", image: str = "node:22-slim") -> TestPlan:
    if runner == "vitest":
        cmd = _report_cmd("npx vitest run --reporter=json --outputFile=/tmp/aibi-report.json")
        hint = "Vitest (import { test, expect } from 'vitest')"
    elif runner == "jest":
        cmd = _report_cmd("npx jest --ci --json --outputFile=/tmp/aibi-report.json")
        hint = "Jest (global test/expect, same module style as the existing code: require or import)"
    elif runner == "node-test":
        cmd = "node --test --test-reporter=tap 2>&1"
        hint = (
            "the built-in node:test runner (import test from 'node:test'; import assert from 'node:assert')"
        )
    else:
        cmd = "npm test 2>&1"
        hint = f"the project's existing test setup (npm test runs: {test_script})"
    return TestPlan(
        language="javascript",
        runner=runner,
        image=image,
        # --ignore-scripts: при установке не выполняем postinstall-скрипты чужих пакетов.
        install_cmd=(
            "set -e; cd /w; if [ -f package-lock.json ]; then npm ci --ignore-scripts --no-audit --no-fund; "
            "elif [ -f package.json ]; then npm install --ignore-scripts --no-audit --no-fund; fi"
        ),
        test_cmd=cmd,
        install_env=("HOME=/tmp", "npm_config_cache=/tmp/.npm", "npm_config_update_notifier=false"),
        test_env=("HOME=/tmp", "CI=true", "npm_config_cache=/tmp/.npm", "NODE_OPTIONS=--no-warnings"),
        framework_hint=hint,
        test_file_re=JS_TEST_RE,
        test_script=test_script,
    )


def go_plan(image: str = "golang:1.23-alpine") -> TestPlan:
    # /tmp в песочнице смонтирован с noexec, а go test запускает собранный бинарник,
    # поэтому GOTMPDIR — на томе.
    cache = (
        "GOMODCACHE=/w/.aibi-go/mod",
        "GOCACHE=/w/.aibi-go/build",
        "GOTMPDIR=/w/.aibi-go/tmp",
        "GOFLAGS=-mod=mod",
        "CGO_ENABLED=0",
    )
    return TestPlan(
        language="go",
        runner="go-test",
        image=image,
        install_cmd="set -e; cd /w; if [ -f go.mod ]; then go mod download; fi",
        test_cmd="mkdir -p /w/.aibi-go/tmp && go test -json -count=1 ./... 2>&1",
        install_env=("HOME=/tmp", *cache),
        test_env=("HOME=/tmp", "GOPROXY=off", *cache),
        framework_hint="Go's standard testing package (func TestXxx(t *testing.T)), same package as the code",
        test_file_re=GO_TEST_RE,
    )


EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".jsx": "javascript",
    ".go": "go",
}


def detect_language(tree: list[str], hint_paths: list[str] | None = None) -> str | None:
    """Язык по файлам из стек-трейса, затем по манифестам, затем по большинству исходников."""
    for path in hint_paths or []:
        lang = EXT_LANG.get(posixpath.splitext(path)[1])
        if lang:
            return lang
    names = {posixpath.basename(p) for p in tree}
    if "go.mod" in names:
        return "go"
    if "package.json" in tree:
        return "javascript"
    if names & {"pyproject.toml", "requirements.txt", "setup.py"}:
        return "python"
    counts: dict[str, int] = {}
    for p in tree:
        lang = EXT_LANG.get(posixpath.splitext(p)[1])
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return max(counts, key=counts.get) if counts else None


async def make_plan(
    tree: list[str],
    read_file: Callable[[str], Awaitable[str]],
    hint_paths: list[str] | None = None,
    python_image: str = "python:3.12-slim",
) -> TestPlan | None:
    lang = detect_language(tree, hint_paths)
    if lang == "python":
        return python_plan(python_image)
    if lang == "go":
        return go_plan()
    if lang == "javascript":
        package = {}
        if "package.json" in tree:
            try:
                package = json.loads(await read_file("package.json"))
            except ValueError:
                package = {}
        return javascript_plan(package)
    return None


def plan_from_spec(spec: dict[str, str] | None) -> TestPlan | None:
    if not spec:
        return None
    if spec["language"] == "python":
        return python_plan(spec.get("image") or "python:3.12-slim")
    if spec["language"] == "go":
        return go_plan(spec.get("image") or "golang:1.23-alpine")
    if spec["language"] == "javascript":
        return javascript_plan_for(
            spec["runner"], spec.get("test_script", ""), spec.get("image") or "node:22-slim"
        )
    return None


def regression_test_path(plan: TestPlan, tree: list[str], bug_file: str, run_id: str) -> str:
    """Куда положить регрессионный тест, чтобы раннер его нашёл."""
    tag = run_id[:8]
    if plan.language == "python":
        test_dir = "tests/" if any(p.startswith("tests/") for p in tree) else ""
        return f"{test_dir}test_ai_regression_{tag}.py"
    if plan.language == "go":
        directory = posixpath.dirname(bug_file)
        return posixpath.join(directory, f"ai_regression_{tag}_test.go")
    existing = next((p for p in tree if JS_TEST_RE.search(p) and "node_modules" not in p), None)
    if existing:
        m = re.search(r"\.(test|spec)\.((?:c|m)?[jt]sx?)$", existing)
        suffix = f".{m.group(1)}.{m.group(2)}" if m else ".test.js"
        directory = posixpath.dirname(existing)
    else:
        ext = posixpath.splitext(bug_file)[1].lstrip(".") or "js"
        suffix = f".test.{ext}"
        directory = posixpath.dirname(bug_file)
    return posixpath.join(directory, f"ai_regression_{tag}{suffix}")


GO_TEST_FUNC_RE = re.compile(r"^func (Test\w+)\(", re.M)
JS_TEST_NAME_RE = re.compile(r"""\b(?:test|it)\s*\(\s*(['"`])(.+?)\1""")


def regression_test_ids(plan: TestPlan, test_path: str, failed: list[str], code: str) -> list[str]:
    """Какие из упавших тестов — это регрессионный тест агента."""
    by_path = [f for f in failed if f.startswith(f"{test_path}::")]
    if by_path:
        return by_path
    if plan.language == "go":
        names = set(GO_TEST_FUNC_RE.findall(code))
        return [f for f in failed if f.rsplit("::", 1)[-1] in names]
    if plan.runner == "node-test":
        names = {m.group(2) for m in JS_TEST_NAME_RE.finditer(code)}
        return [f for f in failed if f.removeprefix("node::") in names]
    return []


def is_load_failure(test_id: str) -> bool:
    """Тест упал, не начав выполняться: файл не собрался или не импортировался."""
    return test_id.endswith(("::(suite)", "::(build)", "::(run)")) or test_id in (
        "(collection error)",
        "npm::test",
    )
