"""Граф агента на LangGraph.

intake → load_repo → investigate (ReAct) → fix ⇄ commit → verify → reflect (Reflexion, до N попыток)
→ review (критик + security) → request_approval → await_approval (interrupt, Human-in-the-Loop)
→ open_pr → post_pr_check → report.  Отказ или неудача: → cleanup (удалить ветку) → report.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from app.agent import messages as msg
from app.agent import prompts
from app.agent.llm import LLMs, ainvoke_with_fallback, usage_tokens
from app.agent.state import AgentState
from app.agent.tools import MAX_TOOL_OUTPUT, build_tools
from app.clients.github import GitHubError
from app.clients.telegram import Notifier
from app.config import Settings
from app.languages import is_load_failure, make_plan, plan_from_spec, regression_test_path
from app.sandbox import (
    SandboxError,
    TestRun,
    add_files_to_tar,
    check_repro_run,
    compare_runs,
    describe_baseline,
    repack_github_tarball,
)
from app.schemas import Channel, Finding, Incident, Source
from app.services.ci import CiResult, evaluate_runs, extract_failure_log
from app.services.diagnosis import frame_label, parse_diagnosis, trace_facts
from app.services.fixing import FILE_RE, FixFormatError, check_syntax, parse_fix_output
from app.services.security import scan_diff
from app.services.trace_parser import (
    Frame,
    filter_tree,
    map_frames,
    normalize_repo,
    parse_frames,
    resolve_path,
)
from app.store import Store

MAX_TREE_IN_PROMPT = 300


@dataclass
class AgentDeps:
    settings: Settings
    github: Any  # GitHubClient или тестовый двойник с теми же методами
    llms: LLMs
    store: Store
    notifier: Notifier | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    tree_cache: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    sandbox: Any | None = None  # DockerSandbox или двойник; None — проверка только через GitHub Actions


def branch_name(settings: Settings, run_id: str) -> str:
    return f"{settings.branch_prefix}{run_id[:12]}"


THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def _text(message: BaseMessage) -> str:
    content = message.content
    if not isinstance(content, str):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    # Reasoning-модели (Qwen) могут вернуть рассуждения в <think>...</think> прямо в тексте ответа.
    return THINK_RE.sub("", content).strip()


def build_graph(deps: AgentDeps, checkpointer: Any) -> Any:
    s = deps.settings
    gh = deps.github

    async def emit(state: AgentState, node: str, text: str, level: str = "info") -> None:
        await deps.store.add_event(state["run_id"], node, text, level)

    async def count(state: AgentState, message: BaseMessage) -> int:
        tokens = usage_tokens(message)
        await deps.store.add_tokens(state["run_id"], tokens)
        return tokens

    async def call_llm(state: AgentState, node: str, primary: Any, fallback: Any, messages: list, **kw: Any):
        async def on_fallback(exc: Exception) -> None:
            await emit(
                state,
                node,
                f"Основная модель недоступна ({exc.__class__.__name__}), переключаюсь на запасную",
                "warning",
            )

        return await ainvoke_with_fallback(primary, fallback, messages, on_fallback=on_fallback, **kw)

    async def wait_for_ci(state: AgentState, sha: str, prefer_event: str | None = None) -> CiResult:
        start = deps.clock()
        while True:
            await deps.sleep(s.ci_poll_interval)
            runs = await gh.list_runs(state["repo"], sha)
            result = evaluate_runs(
                runs,
                deps.clock() - start,
                no_runs_grace=s.ci_no_runs_grace,
                timeout=s.ci_timeout,
                prefer_event=prefer_event,
            )
            if result.finished:
                return result

    async def run_sandbox(
        state: AgentState, ref: str, node: str, extra_files: dict[str, str] | None = None
    ) -> TestRun | None:
        """Тесты в изолированной песочнице на указанном коммите. None — песочница не смогла отработать."""
        try:
            archive = repack_github_tarball(await gh.get_tarball(state["repo"], ref))
            if extra_files:
                archive = add_files_to_tar(archive, extra_files)
            return await deps.sandbox.run_tests(archive, plan_from_spec(state.get("test_plan")))
        except (SandboxError, GitHubError) as exc:
            await emit(state, node, f"Песочница не смогла запустить тесты: {exc}", "warning")
            return None

    # ------------------------------------------------------------------ узлы
    async def intake(state: AgentState) -> dict:
        incident = Incident(**state["incident"])
        repo = normalize_repo(incident.repo) if incident.repo else ""
        frames = parse_frames(incident.error_text)
        source = f"GitHub Issue #{incident.issue_number}" if incident.source == Source.ISSUE else "форма"
        target = f"для {repo}" if repo else "без репозитория — режим диагноза по тексту"
        await emit(
            state,
            "intake",
            f"Получена ошибка {target} (источник: {source}). "
            f"Фреймов кода приложения в стек-трейсе: {len(frames)}.",
        )
        return {
            "repo": repo,
            "frames": [f.as_dict() for f in frames],
            "attempt": 1,
            "branch": None,
            "feedback": "",
            "previous_fix": "",
            "original_content": "",
            "tokens": 0,
        }

    async def diagnose(state: AgentState) -> dict:
        """Без репозитория: где ошибка (факт из трейса) и как исправить (гипотеза модели, без проверки)."""
        text = state["incident"]["error_text"]
        frames = [Frame(**f) for f in state["frames"]]
        facts = trace_facts(frames, text)
        await emit(
            state,
            "diagnose",
            "Агент разбирает текст ошибки. Код не читается и не запускается — ответ будет гипотезой",
        )
        if facts["raise_site"]:
            await emit(
                state, "diagnose", f"По стек-трейсу ошибка выброшена в {frame_label(facts['raise_site'])}"
            )
        frames_text = "\n".join(f"  {frame_label(f)}" for f in facts["frames"]) or "  (none found)"
        messages = [
            SystemMessage(prompts.DIAGNOSE_SYSTEM),
            HumanMessage(
                prompts.DIAGNOSE_USER.format(
                    error_text=text[:8000],
                    error_type=facts["error_type"] or "unknown",
                    raise_site=frame_label(facts["raise_site"]),
                    language=facts["language"] or "unknown",
                    frames=frames_text,
                )
            ),
        ]
        tokens, hypothesis = 0, None
        for _ in range(2):
            ai = await call_llm(state, "diagnose", deps.llms.main, deps.llms.fallback, messages)
            tokens += await count(state, ai)
            hypothesis = parse_diagnosis(_text(ai))
            if hypothesis:
                break
            messages += [
                ai,
                HumanMessage("Answer with ONE valid JSON object exactly in the required format."),
            ]
        if hypothesis is None:
            await emit(state, "diagnose", "Модель не дала разбор в нужном формате", "error")
            hypothesis = {
                "summary": "",
                "location": {},
                "cause": "Не удалось получить разбор от модели.",
                "fix_steps": [],
                "code_fix": {"before": "", "after": ""},
                "confidence": "low",
                "how_to_verify": "",
                "need_more_info": "Повторите запуск или укажите репозиторий.",
            }
        else:
            await emit(
                state,
                "diagnose",
                f"Причина (гипотеза, уверенность: {hypothesis['confidence']}): {hypothesis['cause'][:300]}",
            )
        return {
            "diagnosis": {"facts": facts, "hypothesis": hypothesis, "verified": False},
            "outcome": "diagnosed",
            "tokens": tokens,
        }

    async def load_repo(state: AgentState) -> dict:
        repo = state["repo"]
        info = await gh.get_repo(repo)
        default_branch = info["default_branch"]
        base_sha = (await gh.get_branch(repo, default_branch))["commit"]["sha"]
        key = (repo, base_sha)
        if key in deps.tree_cache:
            tree = deps.tree_cache[key]
            cached = " (из кэша)"
        else:
            tree = filter_tree(await gh.get_tree(repo, base_sha))
            deps.tree_cache[key] = tree
            cached = ""
        candidates = map_frames([Frame(**f) for f in state["frames"]], tree)
        await emit(
            state,
            "load_repo",
            f"Структура репозитория{cached}: {len(tree)} файлов, ветка {default_branch}. "
            f"Совпадений со стек-трейсом: {len(candidates)}.",
        )
        supports = getattr(gh, "supports_actions", True)
        supports = supports(repo) if callable(supports) else supports  # у локальных проектов CI нет
        has_actions = supports and any(
            p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")) for p in tree
        )
        return {
            "default_branch": default_branch,
            "base_sha": base_sha,
            "tree": tree,
            "candidates": candidates,
            "has_actions": has_actions,
        }

    async def reproduce(state: AgentState) -> dict:
        """Честность: сначала подтвердить баг фактами (тесты или CI), а не верить описанию ошибки."""
        tree = state["tree"]

        async def read(path: str) -> str:
            return (await gh.get_file(state["repo"], path, state["default_branch"]))[0]

        plan = await make_plan(tree, read, [c["path"] for c in state["candidates"]], s.sandbox_image)
        if deps.sandbox is not None and plan is not None:
            state = {**state, "test_plan": plan.spec}
            lang = {"plan": plan.spec}
            await emit(state, "reproduce", f"Язык проекта: {plan.language}, тест-раннер: {plan.runner}")
            if plan.has_tests(tree):
                await emit(
                    state, "reproduce", "Воспроизвожу ошибку: запускаю тесты исходного кода в песочнице"
                )
                run = await run_sandbox(state, state["base_sha"], "reproduce")
                if run is not None and run.status == "failed" and all(is_load_failure(f) for f in run.failed):
                    # Код не собирается/не загружается ещё до исправления — это проблема окружения, а не
                    # воспроизведение бага. Не тратим попытки исправления впустую.
                    first = next(
                        (ln for ln in run.output.splitlines() if ln.strip() and "FAILED" not in ln), ""
                    )
                    await emit(
                        state,
                        "reproduce",
                        f"Тесты исходного кода не запускаются в песочнице (сборка/загрузка): {first[:200]}",
                        "warning",
                    )
                    run = None
                if run is not None and run.status == "failed":
                    await emit(
                        state,
                        "reproduce",
                        f"Ошибка воспроизведена: падают тесты ({run.summary}): {', '.join(run.failed[:5])}",
                    )
                    return {
                        "baseline": run.as_dict(),
                        "reproduced": True,
                        "repro_mode": "tests",
                        "test_plan": lang["plan"],
                    }
                if run is not None and run.status == "passed":
                    await emit(
                        state,
                        "reproduce",
                        f"Все тесты исходного кода проходят ({run.summary}) — существующие тесты ошибку "
                        "не ловят, агент напишет регрессионный тест",
                        "warning",
                    )
                    return {
                        "baseline": run.as_dict(),
                        "reproduced": False,
                        "repro_mode": "write_test",
                        "test_plan": lang["plan"],
                    }
                if run is not None:
                    await emit(
                        state,
                        "reproduce",
                        f"Тесты исходного кода не отработали: {run.status} {run.summary}",
                        "warning",
                    )
            else:
                await emit(
                    state,
                    "reproduce",
                    f"В репозитории нет тестов ({plan.runner}) — агент напишет регрессионный тест",
                    "warning",
                )
                return {
                    "baseline": None,
                    "reproduced": False,
                    "repro_mode": "write_test",
                    "test_plan": lang["plan"],
                }
        if state.get("has_actions"):
            await emit(state, "reproduce", "Проверяю, падает ли CI на исходном коде (GitHub Actions)")
            base_ci = await wait_for_ci(state, state["base_sha"])
            if base_ci.status == "failed":
                await emit(state, "reproduce", "Ошибка подтверждена: CI на исходном коде падает")
                return {"baseline": None, "reproduced": True, "repro_mode": "ci"}
            await emit(state, "reproduce", f"CI на исходном коде: {base_ci.label}", "warning")
        else:
            why = (
                "песочница недоступна"
                if deps.sandbox is None
                else "язык проекта не поддерживается песочницей"
            )
            await emit(state, "reproduce", f"Нечем воспроизвести ошибку: {why} и CI нет", "warning")
        return {"baseline": None, "reproduced": False, "repro_mode": "none"}

    async def confirm_repro(state: AgentState) -> dict:
        """Без воспроизведения бага исправление не предлагается: иначе его нечем проверить."""
        finding = state.get("finding")
        if not finding:
            return {"route": "report", "outcome": "not_reproduced"}
        if state.get("reproduced"):
            return {"route": "fix"}
        if state.get("repro_mode") != "write_test":
            await emit(
                state,
                "confirm_repro",
                "Ошибку не удалось воспроизвести — исправление без проверки не предлагается",
                "error",
            )
            return {"route": "report", "outcome": "not_reproduced"}

        tree = state["tree"]
        plan = plan_from_spec(state.get("test_plan"))
        test_path = regression_test_path(plan, tree, finding["file_path"], state["run_id"])
        content, _ = await gh.get_file(state["repo"], finding["file_path"], state["default_branch"])
        example = next(
            (
                p
                for p in tree
                if plan.test_file_re.search(p) and "conftest" not in p and "node_modules" not in p
            ),
            None,
        )
        example_block = ""
        if "package.json" in tree:
            package_text, _ = await gh.get_file(state["repo"], "package.json", state["default_branch"])
            example_block += f"\npackage.json:\n{package_text[:2000]}\n"
        if plan.language == "go":
            pkg = re.search(r"^package (\w+)", content, re.M)
            example_block += f"\nThe test must be in Go package `{pkg.group(1) if pkg else 'main'}`.\n"
        if example:
            example_text, _ = await gh.get_file(state["repo"], example, state["default_branch"])
            example_block = f"\nExisting test file {example} (for import style):\n{example_text[:2500]}\n"
        feedback, tokens = "", 0
        for attempt in (1, 2):
            await emit(
                state,
                "confirm_repro",
                f"Пишу регрессионный тест, воспроизводящий ошибку (попытка {attempt}/2)",
            )
            ai = await call_llm(
                state,
                "confirm_repro",
                deps.llms.main,
                deps.llms.fallback,
                [
                    SystemMessage(prompts.REPRO_SYSTEM.format(framework=plan.framework_hint)),
                    HumanMessage(
                        prompts.REPRO_USER.format(
                            tree="\n".join(tree[:MAX_TREE_IN_PROMPT]),
                            error_text=state["incident"]["error_text"][:4000],
                            file_path=finding["file_path"],
                            root_cause=finding["root_cause"],
                            content=content[:12000],
                            example_block=example_block,
                            feedback_block=f"\nYOUR PREVIOUS TEST WAS REJECTED: {feedback}\n"
                            if feedback
                            else "",
                            test_path=test_path,
                        )
                    ),
                ],
            )
            tokens += await count(state, ai)
            match = FILE_RE.search(_text(ai))
            if not match:
                feedback = "The answer has no ===FILE_START=== ... ===FILE_END=== block."
                continue
            test_code = match.group(1).strip() + "\n"
            syntax_error = check_syntax(test_path, test_code)
            if syntax_error:
                feedback = syntax_error
                continue
            run = await run_sandbox(state, state["base_sha"], "confirm_repro", {test_path: test_code})
            if run is None:
                break
            ok, feedback, ids = check_repro_run(run, plan, test_path, test_code)
            if ok:
                await emit(
                    state,
                    "confirm_repro",
                    f"Ошибка воспроизведена регрессионным тестом: {', '.join(ids)} падает на исходном коде",
                )
                return {
                    "route": "fix",
                    "reproduced": True,
                    "baseline": run.as_dict(),
                    "repro_test": {"path": test_path, "content": test_code, "ids": ids},
                    "tokens": tokens,
                }
            await emit(state, "confirm_repro", f"Тест не воспроизводит ошибку: {feedback[:200]}", "warning")
        await emit(
            state,
            "confirm_repro",
            "Ошибку не удалось воспроизвести тестом — исправление без проверки не предлагается",
            "error",
        )
        return {"route": "report", "outcome": "not_reproduced", "tokens": tokens}

    async def investigate(state: AgentState) -> dict:
        tree, repo = state["tree"], state["repo"]
        tools = build_tools(gh, repo, state["default_branch"], tree)
        tool_map = {t.name: t for t in tools}
        tree_text = "\n".join(tree[:MAX_TREE_IN_PROMPT])
        if len(tree) > MAX_TREE_IN_PROMPT:
            tree_text += f"\n... and {len(tree) - MAX_TREE_IN_PROMPT} more files (use list_files)"
        candidates = (
            "\n".join(
                f"{c['path']}:{c['line']}" + (f" in {c['function']}" if c["function"] else "")
                for c in state["candidates"]
            )
            or "(no frames matched - use the file tree and search_code)"
        )
        messages: list[BaseMessage] = [
            SystemMessage(prompts.INVESTIGATOR_SYSTEM),
            HumanMessage(
                prompts.INVESTIGATOR_USER.format(
                    repo=repo,
                    error_text=state["incident"]["error_text"][:6000],
                    candidates=candidates,
                    tree=tree_text,
                    baseline=describe_baseline(state.get("baseline")),
                )
            ),
        ]
        await emit(state, "investigate", "Агент анализирует ошибку и ищет причину в коде")
        tokens = 0
        finding: Finding | None = None
        no_defect: Finding | None = None
        for _ in range(s.agent_max_iterations):
            ai: AIMessage = await call_llm(
                state, "investigate", deps.llms.main, deps.llms.fallback, messages, tools=tools
            )
            tokens += await count(state, ai)
            messages.append(ai)
            if not ai.tool_calls:
                messages.append(HumanMessage("Call submit_finding with your result now."))
                continue
            for call in ai.tool_calls:
                name, args, call_id = call["name"], call.get("args") or {}, call["id"]
                if name == "submit_finding":
                    try:
                        candidate = Finding.model_validate(args)
                        if candidate.file_path.strip().upper() == "NONE":
                            no_defect = candidate
                            result = "Finding received."
                            break
                        resolved = resolve_path(candidate.file_path, tree)
                        if resolved is None:
                            raise ValueError(f"file '{candidate.file_path}' is not in the repository tree")
                        finding = candidate.model_copy(update={"file_path": resolved})
                        result = "Finding received."
                    except (ValidationError, ValueError) as exc:
                        result = f"ERROR: invalid finding: {exc}"
                elif name in tool_map:
                    label = {
                        "read_file": f"Агент читает {args.get('path', '?')}",
                        "search_code": f"Агент ищет в коде: «{args.get('query', '')}»",
                        "list_files": f"Агент смотрит список файлов {args.get('prefix', '')}".strip(),
                    }[name]
                    await emit(state, "investigate", label)
                    try:
                        result = str(await tool_map[name].ainvoke(args))
                    except Exception as exc:  # некорректные аргументы, сбой сети — агент перепланирует
                        result = f"ERROR: tool {name} failed: {exc.__class__.__name__}: {exc}"
                    if result.startswith("ERROR"):
                        await emit(
                            state, "investigate", "Инструмент вернул ошибку, агент меняет план", "warning"
                        )
                else:
                    result = f"ERROR: unknown tool {name}"
                messages.append(ToolMessage(content=result[:MAX_TOOL_OUTPUT], tool_call_id=call_id))
            if finding or no_defect:
                break

        if no_defect is not None or (finding is None and not state["candidates"]):
            reason = no_defect.root_cause if no_defect else "агент не нашёл в коде место ошибки"
            await emit(state, "investigate", f"Дефект в коде не найден: {reason}", "warning")
            return {"finding": None, "no_defect_reason": reason, "tokens": tokens}
        if finding is None:
            fallback_path = state["candidates"][0]["path"]
            await emit(
                state,
                "investigate",
                f"Агент не завершил расследование, беру файл из стек-трейса: {fallback_path}",
                "warning",
            )
            finding = Finding(
                file_path=fallback_path,
                root_cause="Причина не сформулирована агентом; файл взят из стек-трейса.",
                fix_plan="Устранить ошибку из стек-трейса минимальным изменением.",
            )
        await emit(state, "investigate", f"Найдено: {finding.file_path}. {finding.root_cause}")
        return {"finding": finding.model_dump(), "tokens": tokens}

    async def fix(state: AgentState) -> dict:
        finding, attempt = state["finding"], state["attempt"]
        path = finding["file_path"]
        updates: dict[str, Any] = {}
        original = state.get("original_content")
        if not original:
            original, file_sha = await gh.get_file(state["repo"], path, state["default_branch"])
            updates |= {"original_content": original, "file_sha": file_sha}
        await emit(state, "fix", f"Попытка {attempt}/{s.max_fix_attempts}: готовлю исправление {path}")
        feedback = ""
        if state.get("feedback"):
            feedback = prompts.FIXER_FEEDBACK.format(
                feedback=state["feedback"], previous_fix=state.get("previous_fix") or "(no file produced)"
            )
        ai = await call_llm(
            state,
            "fix",
            deps.llms.main,
            deps.llms.fallback,
            [
                SystemMessage(prompts.FIXER_SYSTEM),
                HumanMessage(
                    prompts.FIXER_USER.format(
                        file_path=path,
                        root_cause=finding["root_cause"],
                        fix_plan=finding.get("fix_plan", ""),
                        error_text=state["incident"]["error_text"][:4000],
                        feedback_block=feedback,
                        baseline=describe_baseline(state.get("baseline"), max_chars=1500)
                        + (
                            "\n\nRegression test that must pass after the fix (do not change it):\n"
                            + state["repro_test"]["content"]
                            if state.get("repro_test")
                            else ""
                        ),
                        content=original,
                    )
                ),
            ],
        )
        updates["tokens"] = await count(state, ai)
        try:
            proposal = parse_fix_output(_text(ai), original, path)
        except FixFormatError as exc:
            await emit(state, "fix", f"Модель вернула некорректный ответ: {exc}", "warning")
            return updates | {"proposal": None, "fix_error": f"Answer format error: {exc}"}
        syntax_error = check_syntax(path, proposal.content)
        if syntax_error:
            await emit(
                state,
                "fix",
                f"Самопроверка: синтаксическая ошибка, коммит не делается ({syntax_error})",
                "warning",
            )
            return updates | {"proposal": proposal.as_dict(), "fix_error": syntax_error}
        await emit(state, "fix", f"Исправление готово: {proposal.title}")
        return updates | {"proposal": proposal.as_dict(), "fix_error": None}

    async def commit(state: AgentState) -> dict:
        repo, proposal = state["repo"], state["proposal"]
        branch = state.get("branch")
        updates: dict[str, Any] = {}
        if not branch:
            branch = branch_name(s, state["run_id"])
            await gh.create_branch(repo, branch, state["base_sha"])
            updates["branch"] = branch
            await emit(state, "commit", f"Создана ветка {branch}")
        repro = state.get("repro_test")
        if repro and not state.get("repro_committed"):
            await gh.put_file(
                repo,
                repro["path"],
                content=repro["content"],
                sha=None,
                branch=branch,
                message=f"test: regression test for {proposal['title'].removeprefix('fix: ')}",
            )
            updates["repro_committed"] = True
            await emit(state, "commit", f"Регрессионный тест {repro['path']} добавлен в ветку")
        result = await gh.put_file(
            repo,
            state["finding"]["file_path"],
            content=proposal["content"],
            sha=state["file_sha"],
            branch=branch,
            message=proposal["title"],
        )
        await emit(state, "commit", f"Коммит {result['commit_sha'][:7]} в ветку {branch}")
        return updates | {"commit_sha": result["commit_sha"], "file_sha": result["file_sha"]}

    async def verify(state: AgentState) -> dict:
        if state.get("baseline"):
            await emit(
                state, "verify", "Запускаю тесты исправленного кода в песочнице и сравниваю с исходным"
            )
            run = await run_sandbox(state, state["commit_sha"], "verify")
            if run is not None:
                target = (state.get("repro_test") or {}).get("ids")
                ci = compare_runs(state["baseline"], run, target=target)
                if not ci["verified"]:
                    ci["log"] = run.output[-3000:]
                await emit(
                    state, "verify", f"Результат: {ci['label']}", "info" if ci["verified"] else "warning"
                )
                return {"ci": ci}
        if not state.get("has_actions"):
            ci = CiResult(
                "none", "тесты не запускались: нет GitHub Actions, локальная проверка недоступна"
            ).as_dict()
            ci["verified"] = False
            await emit(state, "verify", f"Результат: {ci['label']}", "warning")
            return {"ci": ci}
        await emit(state, "verify", "Жду результаты тестов в GitHub Actions")
        result = await wait_for_ci(state, state["commit_sha"])
        ci = result.as_dict()
        if result.status == "failed" and result.failed_run_ids:
            try:
                jobs = await gh.list_jobs(state["repo"], result.failed_run_ids[0])
                job = next((j for j in jobs if j.get("conclusion") == "failure"), None)
                if job:
                    ci["log"] = extract_failure_log(await gh.get_job_logs(state["repo"], job["id"]))
            except GitHubError as exc:
                await emit(state, "verify", f"Не удалось получить лог тестов: {exc.status}", "warning")
        # Через CI баг считается исправленным, только если CI на исходном коде падал, а теперь проходит.
        ci["verified"] = result.status == "passed" and state.get("repro_mode") == "ci"
        if result.status == "passed" and not ci["verified"]:
            ci["label"] += " (не подтверждено: на исходном коде CI не падал)"
        elif ci["verified"]:
            ci["label"] = "баг исправлен: CI падал на исходном коде, теперь проходит"
        await emit(state, "verify", f"Результат: {ci['label']}", "info" if ci["verified"] else "warning")
        return {"ci": ci}

    async def reflect(state: AgentState) -> dict:
        ci = state.get("ci") or {}
        attempt = state["attempt"]
        if not state.get("fix_error") and ci.get("verified"):
            await emit(state, "reflect", "Баг подтверждённо исправлен — передаю на ревью и решение человека")
            return {"route": "review"}
        if not state.get("fix_error") and ci.get("status") in ("passed", "none"):
            # Тесты зелёные, но исправление бага не доказано — к человеку с этим не идём.
            await emit(
                state, "reflect", "Исправление не подтверждено проверкой — PR не предлагается", "error"
            )
            return {"route": "cleanup", "outcome": "not_reproduced"}
        if not state.get("fix_error") and ci.get("status") == "timeout":
            await emit(state, "reflect", "Тесты не завершились вовремя, останавливаюсь", "error")
            return {"route": "cleanup", "outcome": "gave_up"}
        reason = (
            state.get("fix_error")
            or ci.get("log")
            or "Failed checks: "
            + ", ".join(
                r["name"]
                for r in ci.get("runs", [])
                if r.get("conclusion") not in ("success", "skipped", "neutral")
            )
        )
        if attempt < s.max_fix_attempts:
            await emit(
                state,
                "reflect",
                f"Попытка {attempt} не удалась. Возвращаю агенту причину и пробую снова.",
                "warning",
            )
            previous = (state.get("proposal") or {}).get("content", "")
            return {
                "route": "fix",
                "attempt": attempt + 1,
                "feedback": reason[:3000],
                "previous_fix": previous[:8000],
            }
        await emit(state, "reflect", f"Не удалось починить за {s.max_fix_attempts} попытки", "error")
        return {"route": "cleanup", "outcome": "gave_up"}

    async def review(state: AgentState) -> dict:
        proposal, finding = state["proposal"], state["finding"]
        security = scan_diff(proposal["diff"])
        await emit(state, "review", "Ревьюер-модель проверяет исправление")
        ai = await call_llm(
            state,
            "review",
            deps.llms.fast,
            deps.llms.main,
            [
                SystemMessage(prompts.CRITIC_SYSTEM),
                HumanMessage(
                    prompts.CRITIC_USER.format(
                        file_path=finding["file_path"],
                        root_cause=finding["root_cause"],
                        explanation=proposal["explanation"],
                        ci_label=state["ci"]["label"],
                        baseline=describe_baseline(state.get("baseline"), max_chars=600),
                        security="; ".join(security) or "no findings",
                        error_text=state["incident"]["error_text"][:3000],
                        diff=proposal["diff"][:8000],
                    )
                ),
            ],
        )
        tokens = await count(state, ai)
        out = _text(ai)
        verdict = "REJECT" if "VERDICT: REJECT" in out.upper().replace("**", "") else "APPROVE"
        body = (
            out.split("\n", 1)[1] if out.upper().lstrip("* ").startswith("VERDICT") and "\n" in out else out
        )
        for item in security:
            await emit(state, "review", f"Security: {item}", "warning")
        await emit(
            state, "review", f"Вердикт ревьюера: {verdict}", "info" if verdict == "APPROVE" else "warning"
        )
        return {
            "review": {"verdict": verdict, "security": security, "pr_body": msg.pr_body(state, body)},
            "tokens": tokens,
        }

    async def request_approval(state: AgentState) -> dict:
        run_id = state["run_id"]
        payload = msg.approval_payload(state, f"{s.public_base_url}/runs/{run_id}")
        await deps.store.update_run(run_id, approval=payload)
        chat_id = state["incident"].get("chat_id")
        if state["channel"] == Channel.TELEGRAM and deps.notifier and chat_id:
            try:
                await deps.notifier.request_approval(run_id, chat_id, msg.approval_html(payload))
                await emit(state, "approval", "Запрос на одобрение отправлен в Telegram")
                return {}
            except Exception as exc:
                await emit(
                    state,
                    "approval",
                    f"Не удалось отправить в Telegram ({exc}). Подтвердите на сайте.",
                    "warning",
                )
                await deps.store.update_run(run_id, channel="web")
                return {"channel": "web"}
        await emit(state, "approval", "Ожидаю вашего решения на этой странице")
        return {}

    async def await_approval(state: AgentState) -> dict:
        # Только interrupt: при возобновлении LangGraph перезапускает узел целиком.
        decision = interrupt({"type": "approval", "run_id": state["run_id"]})
        approved = bool(decision.get("approved"))
        who = decision.get("actor", "человек")
        await emit(
            state,
            "approval",
            f"Решение: {'одобрено' if approved else 'отклонено'} ({who})",
            "info" if approved else "warning",
        )
        out: dict[str, Any] = {"decision": decision}
        if not approved:
            out["outcome"] = "rejected"
        return out

    async def open_pr(state: AgentState) -> dict:
        pr = await gh.create_pr(
            state["repo"],
            title=state["proposal"]["title"],
            head=state["branch"],
            base=state["default_branch"],
            body=state["review"]["pr_body"],
        )
        await deps.store.update_run(state["run_id"], pr_url=pr["url"])
        if pr.get("local"):
            await emit(state, "open_pr", f"Изменения готовы в локальной ветке {state['branch']}")
        else:
            await emit(state, "open_pr", f"Создан Pull Request #{pr['number']}")
        return {"pr": pr}

    async def post_pr_check(state: AgentState) -> dict:
        pr = state["pr"]
        await emit(state, "post_pr_check", "Проверяю, не появились ли новые ошибки после PR")
        ci: dict[str, Any] | None = None
        if state.get("has_actions"):
            ci = (await wait_for_ci(state, pr["head_sha"], prefer_event="pull_request")).as_dict()
        elif state.get("baseline"):
            run = await run_sandbox(state, pr["head_sha"], "post_pr_check")
            if run is not None:
                ci = compare_runs(state["baseline"], run)
        if ci is None:
            ci = CiResult("none", "проверки не настроены").as_dict()
        try:
            await gh.comment_issue(state["repo"], pr["number"], msg.post_pr_comment(ci))
        except GitHubError as exc:
            await emit(
                state, "post_pr_check", f"Не удалось оставить комментарий в PR: {exc.status}", "warning"
            )
        outcome = "pr_ok" if ci["status"] == "passed" else "pr_needs_work"
        await emit(
            state,
            "post_pr_check",
            msg.post_pr_comment(ci).split("\n", 1)[0],
            "info" if outcome == "pr_ok" else "warning",
        )
        return {"post_ci": ci, "outcome": outcome}

    async def cleanup(state: AgentState) -> dict:
        if state.get("branch"):
            try:
                await gh.delete_branch(state["repo"], state["branch"])
                await emit(state, "cleanup", f"Ветка {state['branch']} удалена")
            except GitHubError as exc:
                await emit(state, "cleanup", f"Не удалось удалить ветку: {exc.status}", "warning")
        return {}

    async def report(state: AgentState) -> dict:
        result = msg.final_report(state)
        incident = state["incident"]
        if state["channel"] == Channel.TELEGRAM and deps.notifier and incident.get("chat_id"):
            try:
                await deps.notifier.send(incident["chat_id"], msg.report_html(result))
            except Exception as exc:
                await emit(state, "report", f"Не удалось отправить итог в Telegram: {exc}", "warning")
        if incident.get("source") == Source.ISSUE and incident.get("issue_number"):
            try:
                await gh.comment_issue(
                    state["repo"],
                    incident["issue_number"],
                    f"**AI Bug Investigator: {result['title']}**\n\n{result['message']}",
                )
            except GitHubError as exc:
                await emit(state, "report", f"Не удалось прокомментировать issue: {exc.status}", "warning")
        await emit(state, "report", result["title"])
        return {"report": result}

    # ------------------------------------------------------------------ граф
    g = StateGraph(AgentState)
    for name, fn in [
        ("intake", intake),
        ("diagnose", diagnose),
        ("load_repo", load_repo),
        ("reproduce", reproduce),
        ("investigate", investigate),
        ("confirm_repro", confirm_repro),
        ("fix", fix),
        ("commit", commit),
        ("verify", verify),
        ("reflect", reflect),
        ("review", review),
        ("request_approval", request_approval),
        ("await_approval", await_approval),
        ("open_pr", open_pr),
        ("post_pr_check", post_pr_check),
        ("cleanup", cleanup),
        ("report", report),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges(
        "intake", lambda st: "load_repo" if st.get("repo") else "diagnose", ["load_repo", "diagnose"]
    )
    g.add_edge("diagnose", "report")
    g.add_edge("load_repo", "reproduce")
    g.add_edge("reproduce", "investigate")
    g.add_edge("investigate", "confirm_repro")
    g.add_conditional_edges("confirm_repro", lambda st: st["route"], ["fix", "report"])
    g.add_conditional_edges(
        "fix", lambda st: "reflect" if st.get("fix_error") else "commit", ["reflect", "commit"]
    )
    g.add_edge("commit", "verify")
    g.add_edge("verify", "reflect")
    g.add_conditional_edges("reflect", lambda st: st["route"], ["fix", "review", "cleanup"])
    g.add_edge("review", "request_approval")
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges(
        "await_approval",
        lambda st: "open_pr" if st["decision"].get("approved") else "cleanup",
        ["open_pr", "cleanup"],
    )
    g.add_edge("open_pr", "post_pr_check")
    g.add_edge("post_pr_check", "report")
    g.add_edge("cleanup", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
