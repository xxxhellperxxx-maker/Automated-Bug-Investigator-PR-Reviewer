"""Локальный режим: агент работает с git-репозиторием в папке на компьютере, без GitHub.

Репозиторий задаётся как `local:<имя папки>` внутри LOCAL_PROJECTS_DIR (в Docker она подключена в /projects).
Клиент повторяет интерфейс GitHubClient, поэтому граф агента не знает, откуда код.

Изменения агента — отдельная ветка `ai-fix/...`, собранная низкоуровневыми командами git
(hash-object, временный индекс, commit-tree, update-ref): рабочие файлы и текущая ветка
пользователя не трогаются. Вместо Pull Request — эта ветка и команда, как её применить.
"""

import asyncio
import contextlib
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.clients.github import GitHubError

LOCAL_PREFIX = "local:"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
AUTHOR = {
    "GIT_AUTHOR_NAME": "AI Bug Investigator",
    "GIT_AUTHOR_EMAIL": "ai-bug-investigator@localhost",
    "GIT_COMMITTER_NAME": "AI Bug Investigator",
    "GIT_COMMITTER_EMAIL": "ai-bug-investigator@localhost",
}


def is_local(repo: str) -> bool:
    return repo.startswith(LOCAL_PREFIX)


class LocalRepoClient:
    supports_actions = False

    def __init__(self, root: str, host_root: str = "") -> None:
        self.root = Path(root)
        self.host_root = host_root.rstrip("/")  # путь на компьютере пользователя — для подсказок в отчёте

    async def aclose(self) -> None:
        return None

    # --- служебное ------------------------------------------------------------
    def path(self, repo: str) -> Path:
        name = repo.removeprefix(LOCAL_PREFIX)
        if not NAME_RE.match(name) or name in (".", ".."):
            raise GitHubError(400, "имя локального проекта: только буквы, цифры, . _ -", repo)
        path = self.root / name
        if not (path / ".git").exists():
            raise GitHubError(404, f"в {self.root} нет git-репозитория «{name}»", repo)
        return path

    def host_path(self, repo: str) -> str:
        name = repo.removeprefix(LOCAL_PREFIX)
        return f"{self.host_root}/{name}" if self.host_root else str(self.root / name)

    def list_projects(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / ".git").exists() and NAME_RE.match(p.name))

    async def _git(
        self,
        repo: str,
        *args: str,
        input_data: bytes | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> bytes:
        cwd = self.path(repo)
        proc = await asyncio.create_subprocess_exec(
            # Папка смонтирована с другим владельцем — без safe.directory git откажется работать.
            "git",
            "-c",
            "safe.directory=*",
            "-c",
            "core.quotepath=off",
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input_data), timeout)
        except TimeoutError as exc:
            proc.kill()
            raise GitHubError(504, f"git {args[0]}: таймаут", repo) from exc
        if proc.returncode != 0:
            message = err.decode("utf-8", errors="replace").strip()[:300]
            status = (
                404
                if (
                    "unknown revision" in message
                    or "does not exist" in message
                    or "not a valid object" in message
                    or "bad revision" in message
                )
                else 400
            )
            raise GitHubError(status, message or f"git {args[0]} failed", repo)
        return out

    async def _text(self, repo: str, *args: str, **kw: Any) -> str:
        return (await self._git(repo, *args, **kw)).decode("utf-8", errors="replace").strip()

    # --- чтение -------------------------------------------------------------------
    async def get_repo(self, repo: str) -> dict[str, Any]:
        branch = await self._text(repo, "symbolic-ref", "--short", "HEAD")
        return {"default_branch": branch, "full_name": repo, "private": True}

    async def get_branch(self, repo: str, branch: str) -> dict[str, Any]:
        sha = await self._text(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
        return {"name": branch, "commit": {"sha": sha}}

    async def get_tree(self, repo: str, sha: str) -> list[str]:
        out = await self._text(repo, "ls-tree", "-r", "--name-only", sha)
        return [line for line in out.splitlines() if line]

    async def get_file(self, repo: str, path: str, ref: str) -> tuple[str, str]:
        if ".." in path.split("/"):
            raise GitHubError(400, "недопустимый путь", path)
        blob = await self._text(repo, "rev-parse", f"{ref}:{path}")
        content = await self._git(repo, "cat-file", "blob", blob)
        return content.decode("utf-8", errors="replace"), blob

    async def search_code(self, repo: str, query: str, limit: int = 5) -> list[str]:
        try:
            out = await self._text(repo, "grep", "-l", "-I", "-F", "-e", query, "HEAD")
        except GitHubError:
            return []  # git grep возвращает 1, если совпадений нет
        return [line.split(":", 1)[1] for line in out.splitlines() if ":" in line][:limit]

    async def get_tarball(self, repo: str, ref: str) -> bytes:
        name = repo.removeprefix(LOCAL_PREFIX)
        # Каталог-префикс как у архивов GitHub: repack_github_tarball его снимает.
        return await self._git(
            repo, "archive", "--format=tar.gz", f"--prefix={name}-local/", ref, timeout=120.0
        )

    # --- запись (только в ветки агента) --------------------------------------------
    async def create_branch(self, repo: str, branch: str, sha: str) -> None:
        await self._git(repo, "update-ref", f"refs/heads/{branch}", sha, "0" * 40)

    async def delete_branch(self, repo: str, branch: str) -> None:
        if not branch.startswith("ai-fix/"):
            raise GitHubError(403, "агент удаляет только свои ветки ai-fix/*", branch)
        await self._git(repo, "update-ref", "-d", f"refs/heads/{branch}")

    async def put_file(
        self, repo: str, path: str, *, content: str, sha: str | None, branch: str, message: str
    ) -> dict[str, str]:
        if ".." in path.split("/") or path.startswith("/"):
            raise GitHubError(400, "недопустимый путь", path)
        parent = await self._text(repo, "rev-parse", f"refs/heads/{branch}")
        mode = "100644"
        listing = await self._text(repo, "ls-tree", parent, "--", path)
        if listing:
            # Формат ls-tree: "<mode> <type> <object>\t<path>"; режим файла (например, исполняемый) сохраняем.
            mode, _, current = listing.split(None, 3)[:3]
            if sha and current != sha:
                raise GitHubError(409, "файл в ветке изменился", path)
        elif sha:
            raise GitHubError(404, "файл не найден в ветке", path)
        blob = await self._text(repo, "hash-object", "-w", "--stdin", input_data=content.encode("utf-8"))
        # Временный индекс: рабочая копия и индекс пользователя не меняются.
        index = {"GIT_INDEX_FILE": os.path.join(tempfile.gettempdir(), f"aibi-index-{uuid.uuid4().hex}")}
        try:
            await self._git(repo, "read-tree", parent, env=index)
            await self._git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=index)
            tree = await self._text(repo, "write-tree", env=index)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(index["GIT_INDEX_FILE"])
        commit = await self._text(repo, "commit-tree", tree, "-p", parent, "-m", message, env=AUTHOR)
        await self._git(repo, "update-ref", f"refs/heads/{branch}", commit, parent)
        return {"commit_sha": commit, "file_sha": blob}

    async def create_pr(self, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        """PR нет — результат остаётся локальной веткой; описание сохраняется в заметку к коммиту."""
        sha = await self._text(repo, "rev-parse", f"refs/heads/{head}")
        await self._git(repo, "notes", "--ref=ai-bug-investigator", "add", "-f", "-m", body, sha, env=AUTHOR)
        where = self.host_path(repo)
        return {
            "number": 0,
            "url": f"local:{repo.removeprefix(LOCAL_PREFIX)}#{head}",
            "head_sha": sha,
            "local": True,
            "apply_hint": f"git -C {where} diff {base}..{head}   # посмотреть\n"
            f"git -C {where} merge {head}   # применить",
        }

    async def comment_issue(self, repo: str, number: int, body: str) -> None:
        return None  # у локального проекта нет issue и PR: итог виден в интерфейсе

    # --- CI: в локальном режиме его нет, проверка — только песочница ----------------
    async def list_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        return []

    async def list_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        return []

    async def get_job_logs(self, repo: str, job_id: int) -> str:
        return ""

    async def list_issues(self, repo: str, *, label: str, since: str) -> list[dict[str, Any]]:
        return []


class RepoRouter:
    """Направляет вызовы в LocalRepoClient для `local:*` и в GitHubClient для остальных."""

    def __init__(self, github: Any, local: LocalRepoClient | None) -> None:
        self.github = github
        self.local = local

    def client(self, repo: str) -> Any:
        if is_local(repo):
            if self.local is None:
                raise GitHubError(400, "локальный режим выключен: не задан LOCAL_PROJECTS_DIR", repo)
            return self.local
        return self.github

    def supports_actions(self, repo: str) -> bool:
        return getattr(self.client(repo), "supports_actions", True)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(repo: str, *args: Any, **kwargs: Any) -> Any:
            return await getattr(self.client(repo), name)(repo, *args, **kwargs)

        return call
