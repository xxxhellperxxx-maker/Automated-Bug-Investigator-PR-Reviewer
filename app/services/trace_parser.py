"""Разбор стек-трейса без LLM: дешевле, быстрее и детерминированно."""

import re
from dataclasses import asdict, dataclass

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SKIP_RE = re.compile(
    r"(site-packages|dist-packages|node_modules|/vendor/|/usr/lib|<frozen|/lib/python|<string>)"
)
PY_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\S+))?')
GENERIC_FRAME_RE = re.compile(r"([\w./\\-]+\.(?:py|js|ts|jsx|tsx|vue|php|go|rb|java)):(\d+)")
MAX_ERROR_CHARS = 6000


@dataclass
class Frame:
    path: str
    line: int
    function: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


LOCAL_RE = re.compile(r"^local:[A-Za-z0-9_.-]+$")


def normalize_repo(value: str) -> str:
    repo = value.strip()
    if repo.startswith("local:"):
        if not LOCAL_RE.match(repo) or repo in ("local:.", "local:.."):
            raise ValueError("Локальный проект: local:<имя папки> (буквы, цифры, . _ -)")
        return repo
    repo = re.sub(r"^https?://(www\.)?github\.com/", "", repo)
    repo = repo.removesuffix("/").removesuffix(".git")
    if not REPO_RE.match(repo) or ".." in repo:
        raise ValueError("Репозиторий должен быть в формате owner/name или local:<папка>")
    return repo


def parse_frames(text: str) -> list[Frame]:
    """Фреймы кода приложения; фреймы библиотек и интерпретатора отбрасываются."""
    text = text[:MAX_ERROR_CHARS]
    frames: list[Frame] = []
    seen: set[tuple[str, int]] = set()

    def add(path: str, line: str, function: str = "") -> None:
        path = path.replace("\\", "/")
        if SKIP_RE.search(path):
            return
        key = (path, int(line))
        if key not in seen:
            seen.add(key)
            frames.append(Frame(path, int(line), function or ""))

    for m in PY_FRAME_RE.finditer(text):
        add(m.group(1), m.group(2), m.group(3) or "")
    for m in GENERIC_FRAME_RE.finditer(text):
        add(m.group(1), m.group(2))
    return frames


def resolve_path(path: str, tree: list[str]) -> str | None:
    """Сопоставляет путь (абсолютный путь с сервера, ./путь) с файлом репозитория."""
    path = path.strip().strip("`'\"").replace("\\", "/").removeprefix("./").lstrip("/")
    if not path or ".." in path.split("/"):
        return None
    if path in tree:
        return path
    suffix_hits = [p for p in tree if path.endswith("/" + p) or p.endswith("/" + path)]
    if suffix_hits:
        return max(suffix_hits, key=len)
    return None


def map_frames(frames: list[Frame], tree: list[str]) -> list[dict]:
    candidates: list[dict] = []
    for frame in frames:
        hits = [resolve_path(frame.path, tree)] if resolve_path(frame.path, tree) else []
        if not hits:
            name = frame.path.rsplit("/", 1)[-1]
            hits = [p for p in tree if p.rsplit("/", 1)[-1] == name]
        for path in hits:
            item = {"path": path, "line": frame.line, "function": frame.function}
            if item not in candidates:
                candidates.append(item)
    return candidates


def filter_tree(tree: list[str]) -> list[str]:
    binary = re.compile(r"\.(png|jpe?g|gif|svg|ico|lock|woff2?|ttf|eot|pdf|zip|gz|mp4|webp)$", re.I)
    junk = re.compile(r"(^|/)(node_modules|vendor|\.git|dist|build|__pycache__|\.venv)/")
    return [p for p in tree if not binary.search(p) and not junk.search(p)]
