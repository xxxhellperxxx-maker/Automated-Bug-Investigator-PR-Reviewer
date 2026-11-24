"""Тексты для человека: запрос одобрения, итоговые отчёты, описание PR."""

from html import escape
from typing import Any

MAX_DIFF_TG = 1500


def compare_url(repo: str, base: str, branch: str) -> str:
    if repo.startswith("local:"):
        return ""  # у локального проекта нет страницы на GitHub
    return f"https://github.com/{repo}/compare/{base}...{branch}"


def approval_payload(state: dict[str, Any], run_url: str) -> dict[str, Any]:
    finding, proposal, ci, review = state["finding"], state["proposal"], state["ci"], state["review"]
    return {
        "repo": state["repo"],
        "file_path": finding["file_path"],
        "root_cause": finding["root_cause"],
        "fix_plan": finding.get("fix_plan", ""),
        "title": proposal["title"],
        "explanation": proposal["explanation"],
        "diff": full_diff(state)[:8000],
        "ci_label": ci["label"],
        "ci_runs": ci.get("runs", []),
        "attempt": state["attempt"],
        "verdict": review["verdict"],
        "security": review["security"],
        "compare_url": compare_url(state["repo"], state["default_branch"], state["branch"]),
        "run_url": run_url,
        "reproduction": reproduction_label(state.get("baseline"), state.get("repro_test")),
    }


def full_diff(state: dict[str, Any]) -> str:
    """Diff исправления плюс новый регрессионный тест, если агент его написал."""
    diff = state["proposal"]["diff"]
    repro = state.get("repro_test")
    if repro:
        from app.services.fixing import make_diff

        diff += "\n" + make_diff("", repro["content"], repro["path"])
    return diff


def reproduction_label(baseline: dict[str, Any] | None, repro_test: dict[str, Any] | None = None) -> str:
    if repro_test:
        return (
            f"ошибка воспроизведена новым регрессионным тестом {repro_test['path']} (падал на исходном коде)"
        )
    if not baseline:
        return "тесты до исправления не запускались"
    if baseline["status"] == "failed":
        return (
            f"ошибка воспроизведена тестами: падали {len(baseline['failed'])} ({baseline.get('summary', '')})"
        )
    return f"до исправления все тесты проходили ({baseline.get('summary', '')}) — тесты ошибку не ловили"


def approval_html(p: dict[str, Any]) -> str:
    diff = p["diff"] if len(p["diff"]) <= MAX_DIFF_TG else p["diff"][:MAX_DIFF_TG] + "\n... (обрезано)"
    verdict = "ревьюер-модель согласна" if p["verdict"] == "APPROVE" else "ревьюер-модель сомневается"
    security = "\n".join(f"- {escape(s)}" for s in p["security"]) if p["security"] else "замечаний нет"
    return "\n".join(
        [
            "<b>AI Bug Investigator: нужно ваше решение</b>",
            f"<b>Репозиторий:</b> {escape(p['repo'])}",
            f"<b>Файл:</b> {escape(p['file_path'])}",
            f"<b>Причина:</b> {escape(p['root_cause'][:700])}",
            f"<b>Исправление:</b> {escape(p['explanation'][:500])}",
            f"<b>Воспроизведение:</b> {escape(p.get('reproduction', '-'))}",
            f"<b>Тесты:</b> {escape(p['ci_label'])} (попытка {p['attempt']})",
            f"<b>Самопроверка:</b> {verdict}",
            f"<b>Security:</b> {security}",
            f"<pre>{escape(diff)}</pre>",
            (f'<a href="{escape(p["compare_url"])}">Изменения на GitHub</a> · ' if p["compare_url"] else "")
            + f'<a href="{escape(p["run_url"])}">Запуск</a>',
        ]
    )


def pr_body(state: dict[str, Any], critic_body: str) -> str:
    finding, proposal, ci = state["finding"], state["proposal"], state["ci"]
    body = critic_body.strip() or (
        f"## Причина\n{finding['root_cause']}\n\n## Что изменено\n{proposal['explanation']}"
    )
    incident = state["incident"]
    footer = [
        "",
        "---",
        f"Файл: `{finding['file_path']}` · попыток: {state['attempt']} · проверка до PR: {ci['label']}",
    ]
    if incident.get("issue_number"):
        footer.append(f"Fixes #{incident['issue_number']}")
    if state.get("repro_test"):
        footer.append(
            f"Добавлен регрессионный тест `{state['repro_test']['path']}`: "
            "падал до исправления, проходит после."
        )
    footer.append("_PR создан автоматически AI Bug Investigator после одобрения человеком._")
    return body + "\n" + "\n".join(footer)


POST_PR_HEAD = {
    "passed": "Проверка после PR: все проверки зелёные, новых ошибок нет.",
    "failed": "Проверка после PR: есть упавшие проверки. PR требует доработки, не мержить.",
    "none": "Проверка после PR: в репозитории не настроены автоматические проверки.",
    "timeout": "Проверка после PR: проверки не завершились вовремя.",
}


def post_pr_comment(ci: dict[str, Any]) -> str:
    if ci.get("source") == "sandbox":
        return f"Проверка после PR (тесты в изолированной песочнице на коммите PR): {ci['label']}"
    lines = [POST_PR_HEAD[ci["status"]]]
    if ci.get("note"):
        lines.append(ci["note"])
    lines += [f"- [{r['name']}]({r['url']}): {r['conclusion'] or r['status']}" for r in ci.get("runs", [])]
    return "\n".join(lines)


def final_report(state: dict[str, Any]) -> dict[str, str]:
    outcome = state.get("outcome", "error")
    pr = state.get("pr") or {}
    if outcome in ("pr_ok", "pr_needs_work") and pr.get("local"):
        verified = outcome == "pr_ok"
        title = "Исправление готово в локальной ветке" + ("" if verified else ", но требует внимания")
        message = (
            f"Ветка {state['branch']} в локальном проекте. Ваши файлы и текущая ветка не изменены.\n\n"
            f"{pr.get('apply_hint', '')}\n\nПовторная проверка ветки: {state['post_ci'].get('label', '-')}"
        )
    elif outcome in ("pr_ok", "pr_needs_work"):
        title = (
            "Pull Request создан и проверен"
            if outcome == "pr_ok"
            else "Pull Request создан, но требует внимания"
        )
        message = f"{pr.get('url', '')}\n\n{POST_PR_HEAD[state['post_ci']['status']]}"
    elif outcome == "diagnosed":
        d = state["diagnosis"]
        facts, hyp = d["facts"], d["hypothesis"]
        from app.services.diagnosis import CONFIDENCE, frame_label

        title = "Диагноз по тексту ошибки (без проверки кодом)"
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(hyp["fix_steps"], 1))
        parts = [
            f"Где: {frame_label(facts['raise_site'])}"
            + (f" · {facts['error_type']}" if facts["error_type"] else ""),
            f"Причина: {hyp['cause']}",
            f"Как исправить:\n{steps}" if steps else "",
            f"Уверенность: {CONFIDENCE.get(hyp['confidence'], hyp['confidence'])}",
            "Это гипотеза по тексту ошибки: код не читался, тесты не запускались. Укажите репозиторий, "
            "чтобы агент воспроизвёл ошибку и проверил исправление.",
        ]
        message = "\n\n".join(p for p in parts if p)
    elif outcome == "not_reproduced":
        finding = state.get("finding")
        title = "Ошибку не удалось воспроизвести"
        if not finding:
            message = (
                "Агент изучил код и не нашёл дефекта, соответствующего описанию. "
                "Исправление не предлагается, "
                f"PR не создан.\n\nВывод агента: {state.get('no_defect_reason', '-')}"
            )
        else:
            message = (
                "Агент не смог подтвердить ошибку тестами, поэтому исправление без проверки не предлагается "
                "и PR не создан."
                f"\n\nГипотеза агента: файл {finding['file_path']}. {finding.get('root_cause', '')}"
                "\nЧтобы агент мог проверить исправление, добавьте тест или CI, воспроизводящие ошибку."
            )
    elif outcome == "rejected":
        title = "Исправление отклонено"
        message = "Исправление отклонено человеком. Ветка удалена, Pull Request не создан."
    else:
        finding = state.get("finding") or {}
        ci = state.get("ci") or {}
        title = "Не удалось подготовить рабочее исправление"
        message = (
            f"Попыток: {state.get('attempt', 0)}. Результат последней: "
            f"{state.get('fix_error') or ci.get('label', '-')}. Ветка удалена, Pull Request не создан."
            f"\n\nФайл: {finding.get('file_path', '-')}\nПричина: {finding.get('root_cause', '-')}"
        )
    report = {"outcome": outcome, "title": title, "message": message, "pr_url": pr.get("url", "")}
    if outcome == "diagnosed":
        report["diagnosis"] = state["diagnosis"]
    return report


def report_html(report: dict[str, str]) -> str:
    return f"<b>{escape(report['title'])}</b>\n{escape(report['message'])}"
