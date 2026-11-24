"""Локальная песочница для тестов: отдельные Docker-контейнеры через Docker Engine API.

Зачем: агент должен честно проверять исправление, а не верить модели. Песочница
1) воспроизводит баг — запускает тесты на исходном коде до исправления,
2) проверяет исправление — запускает тесты на коммите агента и сравнивает с исходным состоянием.

Изоляция: код репозитория выполняется в одноразовом контейнере без сети (NetworkMode=none),
без секретов сервиса, с read-only корневой ФС, без capabilities, с лимитами памяти/CPU/процессов
и таймаутом. Сеть есть только на шаге установки зависимостей (pip), который идёт в отдельном контейнере.
"""

import contextlib
import io
import logging
import re
import struct
import tarfile
import uuid
from typing import Any

import httpx

from app.languages import PY_TEST_RE, parse_pytest  # noqa: F401 — реэкспорт
from app.sandbox_types import Status, TestRun

log = logging.getLogger(__name__)

MAX_SOURCE_BYTES = 30 * 1024 * 1024


class SandboxError(RuntimeError):
    pass


def has_python_tests(tree: list[str]) -> bool:
    return any(PY_TEST_RE.search(p) for p in tree)


def repack_github_tarball(data: bytes, limit: int = MAX_SOURCE_BYTES) -> bytes:
    """Архив GitHub содержит каталог owner-repo-sha/. Убираем его и всё небезопасное (ссылки, устройства)."""
    out = io.BytesIO()
    total = 0
    with (
        tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as src,
        tarfile.open(fileobj=out, mode="w") as dst,
    ):
        for member in src.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            name = parts[1]
            if name.startswith("/") or ".." in name.split("/"):
                continue
            if not (member.isfile() or member.isdir()):
                continue
            total += member.size
            if total > limit:
                raise SandboxError("репозиторий слишком большой для песочницы")
            member.name = name
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
    return out.getvalue()


def add_files_to_tar(archive: bytes, files: dict[str, str]) -> bytes:
    """Добавляет файлы (например, регрессионный тест агента) в tar с исходниками."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as src,
        tarfile.open(fileobj=out, mode="w") as dst,
    ):
        for member in src.getmembers():
            if member.name in files:
                continue
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            dst.addfile(info, io.BytesIO(data))
    return out.getvalue()


# Падение из-за ошибки в самом тесте (не тот импорт, нет фикстуры, синтаксис) — это не воспроизведение бага.
INVALID_TEST_RE = re.compile(
    r"(ImportError|ModuleNotFoundError|SyntaxError|IndentationError|fixture '\S+' not found|"
    r"Cannot find module|is not defined|undefined: |cannot use .* as|ReferenceError)"
)


def check_repro_run(run: TestRun, plan: Any, test_path: str, code: str) -> tuple[bool, str, list[str]]:
    """Тест воспроизводит баг, если падает на исходном коде, и падает не из-за ошибки в самом тесте.

    Возвращает (воспроизведён, причина отказа для модели, id тестов агента).
    """
    from app.languages import is_load_failure, regression_test_ids

    if run.status in ("error", "timeout"):
        return False, f"The test run did not complete ({run.status}):\n{run.output[-1500:]}", []
    ids = regression_test_ids(plan, test_path, run.failed, code)
    loads = [f for f in run.failed if is_load_failure(f)]
    if loads and (not ids or all(is_load_failure(i) for i in ids)):
        return False, f"The test file could not be compiled, collected or imported:\n{run.output[-1500:]}", []
    if test_path in run.failed:
        return False, f"The test file could not be collected or imported:\n{run.output[-1500:]}", []
    if not ids:
        return False, "The test PASSED on the current (buggy) code, so it does not reproduce the bug.", []
    for line in run.output.splitlines():
        if line.startswith("FAILED") and INVALID_TEST_RE.search(line):
            return False, f"The test fails because of a mistake in the test itself: {line.strip()}", []
    for test_id in ids:
        block = run.output.split(f"FAILED {test_id}", 1)
        if len(block) == 2 and INVALID_TEST_RE.search(block[1][:600]):
            return False, f"The test fails because of a mistake in the test itself:\n{block[1][:600]}", []
    return True, "", ids


def _demux(raw: bytes) -> str:
    """Логи контейнера без TTY идут кадрами: 8 байт заголовка (поток, длина) + данные."""
    chunks, pos = [], 0
    while pos + 8 <= len(raw):
        size = struct.unpack(">I", raw[pos + 4 : pos + 8])[0]
        chunks.append(raw[pos + 8 : pos + 8 + size])
        pos += 8 + size
    if pos == 0:
        return raw.decode("utf-8", errors="replace")
    return b"".join(chunks).decode("utf-8", errors="replace")


class DockerSandbox:
    def __init__(
        self,
        socket_path: str = "/var/run/docker.sock",
        image: str = "python:3.12-slim",
        test_timeout: float = 180.0,
        install_timeout: float = 240.0,
        memory_mb: int = 768,
    ) -> None:
        self._http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://docker", timeout=30.0
        )
        self.image = image
        self.test_timeout = test_timeout
        self.install_timeout = install_timeout
        self.memory = memory_mb * 1024 * 1024
        self._images_ready: set[str] = set()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def available(self) -> bool:
        try:
            return (await self._http.get("/_ping", timeout=5.0)).status_code == 200
        except httpx.HTTPError:
            return False

    async def _call(
        self, method: str, path: str, *, timeout: float | None = 30.0, **kw: Any
    ) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, timeout=timeout, **kw)
        except httpx.HTTPError as exc:
            raise SandboxError(f"Docker API недоступен: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise SandboxError(f"Docker API {resp.status_code} on {path}: {resp.text[:200]}")
        return resp

    async def _ensure_image(self, image: str) -> None:
        if image in self._images_ready:
            return
        name, _, tag = image.partition(":")
        try:
            resp = await self._http.get(f"/images/{image}/json")
        except httpx.HTTPError as exc:
            raise SandboxError(f"Docker API недоступен: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            log.info("pulling sandbox image %s", image)
            await self._call(
                "POST", "/images/create", params={"fromImage": name, "tag": tag or "latest"}, timeout=600.0
            )
        self._images_ready.add(image)

    async def _run_container(
        self, name: str, config: dict[str, Any], timeout: float, archive: bytes | None = None
    ) -> tuple[int | None, str]:
        resp = await self._call("POST", "/containers/create", params={"name": name}, json=config)
        cid = resp.json()["Id"]
        try:
            if archive is not None:
                await self._call(
                    "PUT",
                    f"/containers/{cid}/archive",
                    params={"path": "/w"},
                    content=archive,
                    headers={"Content-Type": "application/x-tar"},
                    timeout=120.0,
                )
            await self._call("POST", f"/containers/{cid}/start")
            try:
                waited = await self._call("POST", f"/containers/{cid}/wait", timeout=timeout)
                code: int | None = waited.json().get("StatusCode")
            except SandboxError:
                with contextlib.suppress(SandboxError):
                    await self._call("POST", f"/containers/{cid}/kill")
                code = None  # таймаут
            logs = await self._call("GET", f"/containers/{cid}/logs", params={"stdout": 1, "stderr": 1})
            return code, _demux(logs.content)
        finally:
            with contextlib.suppress(SandboxError):
                await self._call("DELETE", f"/containers/{cid}", params={"force": "1", "v": "1"})

    def _host_config(self, volume: str, network: bool) -> dict[str, Any]:
        return {
            "Binds": [f"{volume}:/w"],
            "NetworkMode": "bridge" if network else "none",
            "Memory": self.memory,
            "NanoCpus": 1_000_000_000,
            "PidsLimit": 256,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": "rw,size=128m"},
        }

    async def run_tests(self, source_tar: bytes, plan: Any = None) -> TestRun:
        """source_tar — несжатый tar с файлами репозитория в корне; plan — профиль языка (TestPlan)."""
        from app.languages import python_plan

        plan = plan or python_plan(self.image)
        await self._ensure_image(plan.image)
        run_id = uuid.uuid4().hex[:12]
        volume = f"aibi-sbx-{run_id}"
        await self._call("POST", "/volumes/create", json={"Name": volume})
        try:
            # Шаг 1: зависимости (с сетью, без секретов). Код репозитория здесь не запускается.
            code, out = await self._run_container(
                f"aibi-install-{run_id}",
                {
                    "Image": plan.image,
                    "Cmd": ["sh", "-c", plan.install_cmd],
                    "WorkingDir": "/w",
                    "Env": list(plan.install_env),
                    "HostConfig": self._host_config(volume, network=True),
                },
                self.install_timeout,
                archive=source_tar,
            )
            if code != 0:
                status: Status = "timeout" if code is None else "error"
                return TestRun(
                    status, summary="не удалось установить зависимости", output=out[-3000:], exit_code=code
                )
            # Шаг 2: тесты (без сети).
            code, out = await self._run_container(
                f"aibi-test-{run_id}",
                {
                    "Image": plan.image,
                    "Cmd": ["sh", "-c", f"cd /w && {plan.test_cmd}"],
                    "WorkingDir": "/w",
                    "Env": list(plan.test_env),
                    "HostConfig": self._host_config(volume, network=False),
                },
                self.test_timeout,
            )
            if code is None:
                return TestRun(
                    "timeout",
                    summary=f"тесты не завершились за {int(self.test_timeout)} с",
                    output=out[-3000:],
                )
            return plan.parse(out, code)
        finally:
            with contextlib.suppress(SandboxError):
                await self._call("DELETE", f"/volumes/{volume}", params={"force": "1"})


def compare_runs(
    baseline: dict[str, Any] | None, after: TestRun, target: list[str] | None = None
) -> dict[str, Any]:
    """Честный вердикт: что было сломано до исправления, что починено, что сломалось нового.

    verified=True только если баг подтверждённо исправлен: все падения исходного кода (или, если задан
    target, — тесты, воспроизводящие этот баг) теперь проходят и не появилось новых падений.
    """
    before = set((baseline or {}).get("failed", []))
    now = set(after.failed)
    fixed = sorted(before - now)
    new = sorted(now - before)
    still = sorted(before & now)

    if after.status in ("timeout", "error", "no_tests"):
        label = {
            "timeout": "локальные тесты не завершились вовремя",
            "error": f"локальные тесты не запустились: {after.summary or 'ошибка песочницы'}",
            "no_tests": "pytest не нашёл тестов",
        }[after.status]
        status = "timeout" if after.status == "timeout" else "failed"
        return {
            "verified": False,
            "status": status,
            "ok": False,
            "label": label,
            "fixed": fixed,
            "new_failures": new,
            "still_failing": still,
            "summary": after.summary,
            "source": "sandbox",
            "runs": [],
        }

    targets = set(target or [])
    if new:
        verified, status = False, "failed"
        label = f"локальные тесты: новые падения после исправления — {', '.join(new[:5])}"
    elif not before:
        verified, status = False, "passed" if after.status == "passed" else "failed"
        label = "не подтверждено: до исправления тесты эту ошибку не воспроизводили"
    elif targets and targets <= set(fixed):
        verified, status = True, "passed"
        label = "баг исправлен: регрессионный тест теперь проходит, новых падений нет"
        others = sorted(set(still) - targets)
        if others:
            label += f"; ещё падают (падали и до исправления): {', '.join(others[:5])}"
        elif after.summary:
            label += f" ({after.summary})"
    elif targets:
        verified, status = False, "failed"
        label = "баг не исправлен: регрессионный тест по-прежнему падает"
    elif still:
        verified, status = False, "failed"
        label = f"баг не исправлен полностью: по-прежнему падают {', '.join(still[:5])}"
    else:
        verified, status = True, "passed"
        label = f"баг исправлен: падавших тестов было {len(fixed)}, все проходят, новых падений нет"
        if after.summary:
            label += f" ({after.summary})"
    return {
        "verified": verified,
        "status": status,
        "ok": verified,
        "label": label,
        "fixed": fixed,
        "new_failures": new,
        "still_failing": still,
        "summary": after.summary,
        "source": "sandbox",
        "runs": [],
    }


def describe_baseline(baseline: dict[str, Any] | None, max_chars: int = 2500) -> str:
    """Текст для промптов: результат тестов на исходном коде (улика, а не догадка)."""
    if not baseline:
        return "(tests were not run before the fix)"
    if baseline["status"] == "passed":
        return (
            f"All tests pass on the default branch ({baseline.get('summary', '')}): the bug is not covered."
        )
    if baseline["status"] != "failed":
        return f"Test run before the fix: {baseline['status']} {baseline.get('summary', '')}"
    output = baseline.get("output", "")
    return (
        f"Failing tests on the default branch BEFORE any change: {', '.join(baseline['failed'][:10])}\n"
        f"pytest output (tail):\n{output[-max_chars:]}"
    )
