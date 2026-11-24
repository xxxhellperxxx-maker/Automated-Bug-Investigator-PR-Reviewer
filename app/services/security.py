"""Простой security-аудит добавленных строк до создания PR (расширение ТЗ: OWASP)."""

import re

RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(eval|exec)\s*\("), "A03 Injection: выполнение произвольного кода (eval/exec)"),
    (
        re.compile(r"shell\s*=\s*True|os\.system\s*\(|os\.popen\s*\("),
        "A03 Injection: запуск команд через shell",
    ),
    (
        re.compile(r"""\.execute\(\s*(f["']|["'].*["']\s*(%|\+|\.format))"""),
        "A03 Injection: SQL-запрос собирается из строк",
    ),
    (
        re.compile(r"pickle\.loads?\(|yaml\.load\((?!.*SafeLoader)|marshal\.loads\("),
        "A08 Небезопасная десериализация",
    ),
    (
        re.compile(r"""(?i)(password|passwd|secret|token|api_key|apikey)\s*[:=]\s*["'][^"']{6,}["']"""),
        "A07 Секрет записан прямо в коде",
    ),
    (re.compile(r"verify\s*=\s*False"), "A02 Отключена проверка TLS-сертификата"),
    # JavaScript / TypeScript
    (
        re.compile(r"child_process|\bexecSync\(|new Function\("),
        "A03 Injection: запуск команд или кода (Node.js)",
    ),
    (re.compile(r"\.innerHTML\s*=|dangerouslySetInnerHTML"), "A03 XSS: вставка HTML без экранирования"),
    (re.compile(r"rejectUnauthorized\s*:\s*false"), "A02 Отключена проверка TLS-сертификата (Node.js)"),
    # Go
    (re.compile(r'exec\.Command\(\s*"(sh|bash)"'), "A03 Injection: запуск команд через shell (Go)"),
    (re.compile(r"InsecureSkipVerify\s*:\s*true"), "A02 Отключена проверка TLS-сертификата (Go)"),
    (re.compile(r"\bhashlib\.(md5|sha1)\("), "A02 Слабый алгоритм хеширования"),
]


def scan_diff(diff: str) -> list[str]:
    findings: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern, title in RULES:
            if pattern.search(line[1:]):
                findings.append(f"{title}: `{line[1:].strip()[:120]}`")
    return findings
