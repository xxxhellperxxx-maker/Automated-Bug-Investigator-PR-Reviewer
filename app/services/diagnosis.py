"""Диагноз по одному тексту ошибки, без репозитория.

Факты (место, где выброшено исключение, тип ошибки, цепочка вызовов) извлекаются из текста
детерминированно. Причина и способ исправления — гипотеза модели, и она так и помечается:
код не читался, тесты не запускались.
"""

import json
import re
from typing import Any

from app.services.trace_parser import Frame

ERROR_LINE_RE = re.compile(
    r"^\s*(?:Uncaught\s+)?(?P<type>(?:[\w.]+\.)?\w*(?:Error|Exception|Exit|Interrupt|Fault)|panic)"
    r"(?::\s*(?P<message>.*))?$",
    re.M,
)
EXT_LANG = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".java": "Java",
    ".rb": "Ruby",
    ".php": "PHP",
    ".vue": "JavaScript",
}
CONFIDENCE = {"high": "высокая", "medium": "средняя", "low": "низкая"}


def raise_site(frames: list[Frame], text: str) -> Frame | None:
    """Где выброшено исключение: в трейсе Python — последний фрейм, в JS/Go и прочих — первый."""
    if not frames:
        return None
    return frames[-1] if "most recent call last" in text else frames[0]


def error_type(text: str) -> tuple[str, str]:
    matches = list(ERROR_LINE_RE.finditer(text))
    if not matches:
        return "", ""
    m = matches[-1] if "most recent call last" in text else matches[0]
    return m.group("type"), (m.group("message") or "").strip()[:300]


def guess_language(frames: list[Frame], text: str) -> str:
    for frame in frames:
        for ext, lang in EXT_LANG.items():
            if frame.path.endswith(ext):
                return lang
    if "Traceback (most recent call last)" in text:
        return "Python"
    if "goroutine " in text or "panic:" in text:
        return "Go"
    return ""


def trace_facts(frames: list[Frame], text: str) -> dict[str, Any]:
    site = raise_site(frames, text)
    etype, emessage = error_type(text)
    return {
        "raise_site": site.as_dict() if site else None,
        "error_type": etype,
        "error_message": emessage,
        "language": guess_language(frames, text),
        "frames": [f.as_dict() for f in frames[:15]],
    }


def parse_diagnosis(text: str) -> dict[str, Any] | None:
    """Ответ модели — JSON-объект; лишний текст вокруг (или блок ```json) допускается."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("cause"):
        return None
    location = data.get("location") if isinstance(data.get("location"), dict) else {}
    code = data.get("code_fix") if isinstance(data.get("code_fix"), dict) else {}
    steps = data.get("fix_steps") if isinstance(data.get("fix_steps"), list) else []
    confidence = str(data.get("confidence", "low")).lower()
    return {
        "summary": str(data.get("summary", ""))[:500],
        "location": {
            "file": str(location.get("file") or ""),
            "line": location.get("line") if isinstance(location.get("line"), int) else None,
            "function": str(location.get("function") or ""),
        },
        "cause": str(data["cause"])[:2000],
        "fix_steps": [str(s)[:500] for s in steps][:8],
        "code_fix": {
            "before": str(code.get("before") or "")[:2000],
            "after": str(code.get("after") or "")[:2000],
        },
        "confidence": confidence if confidence in CONFIDENCE else "low",
        "how_to_verify": str(data.get("how_to_verify", ""))[:1000],
        "need_more_info": str(data.get("need_more_info", ""))[:1000],
    }


def frame_label(frame: dict[str, Any] | None) -> str:
    if not frame:
        return "не указано в тексте ошибки"
    label = f"{frame['path']}:{frame['line']}"
    return f"{label} в {frame['function']}" if frame.get("function") else label
