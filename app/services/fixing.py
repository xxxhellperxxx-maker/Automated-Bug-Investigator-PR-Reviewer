"""Разбор ответа модели-фиксера, diff и быстрая самопроверка синтаксиса до коммита."""

import ast
import difflib
import json
import re
from dataclasses import asdict, dataclass

FILE_RE = re.compile(r"===FILE_START===\r?\n?(.*?)\r?\n?===FILE_END===", re.S)
FENCE_START_RE = re.compile(r"^```[\w+-]*\r?\n")
FENCE_END_RE = re.compile(r"\r?\n```\s*$")


class FixFormatError(ValueError):
    pass


@dataclass
class FixProposal:
    title: str
    explanation: str
    content: str
    diff: str

    def as_dict(self) -> dict:
        return asdict(self)


def make_diff(old: str, new: str, path: str, context: int = 2) -> str:
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", n=context, lineterm=""
    )
    return "\n".join(lines)


def parse_fix_output(text: str, original: str, path: str) -> FixProposal:
    match = FILE_RE.search(text)
    if not match:
        raise FixFormatError("ответ не содержит блок ===FILE_START=== ... ===FILE_END===")
    content = FENCE_END_RE.sub("", FENCE_START_RE.sub("", match.group(1)))
    if not content.endswith("\n"):
        content += "\n"
    if content.strip() == original.strip():
        raise FixFormatError("файл не изменён")
    if len(content) < len(original) * 0.4:
        # Модель «потеряла» большую часть файла (обычная проблема на длинных файлах).
        raise FixFormatError("новая версия файла подозрительно короткая, вероятно, код обрезан")

    title_m = re.search(r"TITLE:\s*(.+)", text)
    title = (title_m.group(1).strip() if title_m else "fix: automated bug fix")[:72]
    if not title.lower().startswith(("fix", "bug")):
        title = f"fix: {title}"[:72]
    expl_m = re.search(r"EXPLANATION:\s*(.*?)(?====FILE_START===)", text, re.S)
    explanation = expl_m.group(1).strip() if expl_m else ""
    return FixProposal(title, explanation, content, make_diff(original, content, path))


def check_syntax(path: str, content: str) -> str | None:
    """Линтер-минимум: возвращает текст ошибки или None. Тесты всё равно идут в CI."""
    try:
        if path.endswith(".py"):
            ast.parse(content, filename=path)
        elif path.endswith(".json"):
            json.loads(content)
    except SyntaxError as exc:
        return f"SyntaxError in {path} line {exc.lineno}: {exc.msg}"
    except ValueError as exc:
        return f"Invalid JSON in {path}: {exc}"
    return None
