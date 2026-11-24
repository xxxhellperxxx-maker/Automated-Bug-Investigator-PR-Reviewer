"""Асинхронный клиент GitHub REST API с повторами при сетевых сбоях и лимитах."""

import asyncio
import base64
import logging
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class GitHubError(Exception):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"GitHub {status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


def _path(value: str) -> str:
    return quote(value, safe="/")


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-bug-investigator",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # httpx убирает Authorization при редиректе на другой хост (логи Actions отдаются через blob storage).
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=30.0, follow_redirects=True, transport=transport
        )
        self._retries = retries
        self._backoff = backoff

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self._retries):
            last = attempt == self._retries - 1
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise GitHubError(0, f"network error: {exc.__class__.__name__}", path) from exc
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            if resp.status_code in RETRY_STATUSES and not last:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else self._backoff * 2**attempt
                await asyncio.sleep(min(delay, 30.0))
                continue
            if resp.status_code >= 400:
                try:
                    message = resp.json().get("message", "")
                except ValueError:
                    message = resp.text[:200]
                raise GitHubError(resp.status_code, message, path)
            return resp
        raise AssertionError("unreachable")

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._request(method, path, **kwargs)
        return resp.json() if resp.content else None

    # --- чтение репозитория -------------------------------------------------
    async def get_repo(self, repo: str) -> dict[str, Any]:
        return await self._json("GET", f"/repos/{repo}")

    async def get_branch(self, repo: str, branch: str) -> dict[str, Any]:
        return await self._json("GET", f"/repos/{repo}/branches/{_path(branch)}")

    async def get_tree(self, repo: str, sha: str) -> list[str]:
        data = await self._json("GET", f"/repos/{repo}/git/trees/{sha}", params={"recursive": "1"})
        return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

    async def get_file(self, repo: str, path: str, ref: str) -> tuple[str, str]:
        """Возвращает (текст, sha блоба)."""
        data = await self._json("GET", f"/repos/{repo}/contents/{_path(path)}", params={"ref": ref})
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError(400, "path is not a file", path)
        if not data.get("content") and data.get("size", 0) > 0:
            raise GitHubError(413, "file is too large for the contents API", path)
        text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        return text, data["sha"]

    async def get_tarball(self, repo: str, ref: str) -> bytes:
        """Архив репозитория на коммите (для песочницы). GitHub отдаёт редирект на codeload."""
        resp = await self._request("GET", f"/repos/{repo}/tarball/{ref}", timeout=120.0)
        return resp.content

    async def search_code(self, repo: str, query: str, limit: int = 5) -> list[str]:
        data = await self._json(
            "GET", "/search/code", params={"q": f"{query} repo:{repo}", "per_page": str(limit)}
        )
        return [item["path"] for item in data.get("items", [])]

    # --- запись ---------------------------------------------------------------
    async def create_branch(self, repo: str, branch: str, sha: str) -> None:
        await self._json("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def delete_branch(self, repo: str, branch: str) -> None:
        await self._request("DELETE", f"/repos/{repo}/git/refs/heads/{_path(branch)}")

    async def put_file(
        self, repo: str, path: str, *, content: str, sha: str | None, branch: str, message: str
    ) -> dict[str, str]:
        """sha=None — создать новый файл (например, регрессионный тест)."""
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        data = await self._json("PUT", f"/repos/{repo}/contents/{_path(path)}", json=body)
        return {"commit_sha": data["commit"]["sha"], "file_sha": data["content"]["sha"]}

    async def create_pr(self, repo: str, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        data = await self._json(
            "POST", f"/repos/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body}
        )
        return {"number": data["number"], "url": data["html_url"], "head_sha": data["head"]["sha"]}

    async def comment_issue(self, repo: str, number: int, body: str) -> None:
        await self._json("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    # --- CI (GitHub Actions) ----------------------------------------------------
    async def list_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        data = await self._json("GET", f"/repos/{repo}/actions/runs", params={"head_sha": head_sha})
        return data.get("workflow_runs", [])

    async def list_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        data = await self._json("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        return data.get("jobs", [])

    async def get_job_logs(self, repo: str, job_id: int) -> str:
        resp = await self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        return resp.text

    # --- issues -------------------------------------------------------------------
    async def list_issues(self, repo: str, *, label: str, since: str) -> list[dict[str, Any]]:
        data = await self._json(
            "GET",
            f"/repos/{repo}/issues",
            params={"labels": label, "state": "open", "since": since, "per_page": "30"},
        )
        return [i for i in data if "pull_request" not in i]
