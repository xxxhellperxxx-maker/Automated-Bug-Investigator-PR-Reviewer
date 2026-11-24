"""Инструменты агента-следователя.

Ошибки инструментов не бросаются, а возвращаются модели текстом «ERROR: ...»:
так агент может перепланировать (другой путь, другой запрос) — это fallback-механизм из ТЗ.
"""

from langchain_core.tools import BaseTool, StructuredTool

from app.clients.github import GitHubClient, GitHubError
from app.schemas import Finding
from app.services.trace_parser import resolve_path

MAX_LINES = 400
MAX_TOOL_OUTPUT = 12000


def build_tools(gh: GitHubClient, repo: str, ref: str, tree: list[str]) -> list[BaseTool]:
    file_cache: dict[str, str] = {}

    def suggest(path: str) -> str:
        name = path.rsplit("/", 1)[-1].lower()
        similar = [p for p in tree if name and name in p.lower()][:5]
        return f" Similar files: {', '.join(similar)}" if similar else " Use list_files to see the tree."

    async def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        """Read a file from the repository. Returns numbered lines. Optional 1-based line range."""
        resolved = resolve_path(path, tree)
        if resolved is None:
            return f"ERROR: file '{path}' is not in the repository.{suggest(path)}"
        if resolved not in file_cache:
            try:
                file_cache[resolved], _ = await gh.get_file(repo, resolved, ref)
            except GitHubError as exc:
                return f"ERROR: cannot read '{resolved}': {exc.message or exc.status}"
        lines = file_cache[resolved].splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + MAX_LINES - 1, start + MAX_LINES - 1)
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines)" if end < len(lines) else ""
        return f"{resolved} (lines {start}-{end} of {len(lines)})\n{body}{more}"[:MAX_TOOL_OUTPUT]

    async def search_code(query: str) -> str:
        """Search repository code for an identifier or text. Returns matching file paths."""
        query = query.strip()[:100]
        if not query:
            return "ERROR: empty query"
        try:
            paths = await gh.search_code(repo, query)
        except GitHubError as exc:
            paths = []
            note = f" (GitHub search unavailable: {exc.status})"
        else:
            note = ""
        if not paths:
            # Свежие репозитории GitHub индексирует не сразу — ищем хотя бы по именам файлов.
            paths = [p for p in tree if query.lower() in p.lower()][:10]
            note += " (matched by file name; code index returned nothing)"
        return ("\n".join(paths) or "No matches.") + note

    async def list_files(prefix: str = "") -> str:
        """List repository files whose path starts with the given prefix (directory)."""
        prefix = prefix.strip().removeprefix("./").lstrip("/")
        files = [p for p in tree if p.startswith(prefix)][:200]
        return "\n".join(files) or f"No files under '{prefix}'."

    async def submit_finding(file_path: str, root_cause: str, fix_plan: str) -> str:
        """Submit the final investigation result."""
        return "Finding received."

    return [
        StructuredTool.from_function(coroutine=read_file, name="read_file"),
        StructuredTool.from_function(coroutine=search_code, name="search_code"),
        StructuredTool.from_function(coroutine=list_files, name="list_files"),
        StructuredTool.from_function(
            coroutine=submit_finding,
            name="submit_finding",
            description="Submit the final result: the file to change, the root cause and the fix plan.",
            args_schema=Finding,
        ),
    ]
