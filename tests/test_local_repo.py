"""Локальный режим: настоящий git-репозиторий во временной папке, без GitHub."""

import subprocess

import pytest

from app.clients.github import GitHubError
from app.clients.local_repo import LocalRepoClient, RepoRouter
from tests.fakes import FIXED_CART, REPO_FILES, FakeGitHub, FakeSandbox

FILES = {k: v for k, v in REPO_FILES.items() if not k.startswith(".github/")}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def projects(tmp_path):
    repo = tmp_path / "projects" / "shop"
    for path, content in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (repo / "notes.txt").write_text("незакоммиченный файл пользователя\n")
    return tmp_path / "projects"


async def test_local_client_reads_and_commits_without_touching_worktree(projects):
    client = LocalRepoClient(str(projects), "/Users/me/projects")
    repo = "local:shop"
    assert client.list_projects() == ["shop"]
    assert (await client.get_repo(repo))["default_branch"] == "main"
    base = (await client.get_branch(repo, "main"))["commit"]["sha"]
    assert "app/cart.py" in await client.get_tree(repo, base)
    content, blob = await client.get_file(repo, "app/cart.py", "main")
    assert "len(items)" in content
    assert await client.search_code(repo, "average_price") == ["app/cart.py", "tests/test_cart.py"]

    await client.create_branch(repo, "ai-fix/abc", base)
    res = await client.put_file(
        repo, "app/cart.py", content=FIXED_CART, sha=blob, branch="ai-fix/abc", message="fix: empty cart"
    )
    await client.put_file(
        repo,
        "tests/test_new.py",
        content="def test_x():\n    pass\n",
        sha=None,
        branch="ai-fix/abc",
        message="test: new",
    )
    assert (await client.get_file(repo, "app/cart.py", "ai-fix/abc"))[0] == FIXED_CART
    # Рабочая копия и текущая ветка пользователя не изменились.
    worktree = projects / "shop"
    assert git(worktree, "symbolic-ref", "--short", "HEAD") == "main"
    assert "len(items)" in (worktree / "app/cart.py").read_text()
    assert git(worktree, "status", "--porcelain") == "?? notes.txt"
    assert git(worktree, "log", "-1", "--format=%an", "ai-fix/abc") == "AI Bug Investigator"

    archive = await client.get_tarball(repo, "ai-fix/abc")
    import io
    import tarfile

    from app.sandbox import repack_github_tarball

    names = tarfile.open(fileobj=io.BytesIO(repack_github_tarball(archive))).getnames()
    assert "tests/test_new.py" in names and "app/cart.py" in names

    pr = await client.create_pr(repo, title="fix", head="ai-fix/abc", base="main", body="описание")
    assert pr["local"] and pr["head_sha"] == git(worktree, "rev-parse", "ai-fix/abc")
    assert "git -C /Users/me/projects/shop merge ai-fix/abc" in pr["apply_hint"]
    assert git(worktree, "notes", "--ref=ai-bug-investigator", "show", "ai-fix/abc") == "описание"

    await client.delete_branch(repo, "ai-fix/abc")
    with pytest.raises(GitHubError):
        await client.get_branch(repo, "ai-fix/abc")
    with pytest.raises(GitHubError):
        await client.delete_branch(repo, "main")  # чужие ветки агент не удаляет
    assert res["commit_sha"]


async def test_local_client_rejects_bad_names_and_paths(projects):
    client = LocalRepoClient(str(projects))
    for bad in ("local:../etc", "local:missing"):
        with pytest.raises(GitHubError):
            await client.get_repo(bad)
    with pytest.raises(GitHubError):
        await client.get_file("local:shop", "../secret", "main")


async def test_router_dispatches_by_prefix(projects):
    gh = FakeGitHub()
    router = RepoRouter(gh, LocalRepoClient(str(projects)))
    assert (await router.get_repo("local:shop"))["default_branch"] == "main"
    assert (await router.get_repo("acme/shop"))["full_name"] == "acme/shop"
    assert router.supports_actions("local:shop") is False and router.supports_actions("acme/shop") is True
    with pytest.raises(GitHubError):
        await RepoRouter(gh, None).get_repo("local:shop")


async def test_full_run_on_local_project(build_harness, projects):
    router = RepoRouter(FakeGitHub(), LocalRepoClient(str(projects), "/Users/me/projects"))
    h = await build_harness(github=router, sandbox=FakeSandbox())
    from app.schemas import Incident
    from tests.fakes import TRACE

    run_id = await h.c.runner.start(Incident(repo="local:shop", error_text=TRACE))
    await h.c.runner.wait_idle()
    run = await h.c.store.get_run(run_id)
    assert run.status == "awaiting_approval"
    assert run.approval["compare_url"] == ""
    assert run.approval["ci_label"].startswith("баг исправлен")

    await h.decide(run_id, approved=True)
    run = await h.c.store.get_run(run_id)
    assert run.status == "completed" and run.outcome == "pr_ok"
    assert run.result["title"] == "Исправление готово в локальной ветке"
    assert "merge ai-fix/" in run.result["message"]
    worktree = projects / "shop"
    branch = git(worktree, "branch", "--list", "ai-fix/*").strip(" *")
    assert "if not items" in git(worktree, "show", f"{branch}:app/cart.py")
    assert (
        "len(items)" in (worktree / "app/cart.py").read_text()
        and "if not items" not in (worktree / "app/cart.py").read_text()
    )
