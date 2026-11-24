from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_container
from app.container import Container
from app.runner import RunConflictError
from app.schemas import Channel, Decision, Incident, RunCreate, RunDetail, RunOut, Source
from app.services.trace_parser import normalize_repo

router = APIRouter(prefix="/api/v1", tags=["runs"])
ContainerDep = Annotated[Container, Depends(get_container)]


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(body: RunCreate, c: ContainerDep) -> dict[str, str]:
    """Ручной ввод ошибки. Агент работает в фоне, статус — GET /runs/{id}."""
    try:
        repo = normalize_repo(body.repo) if body.repo else ""
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    try:
        chat_id = await c.runner.resolve_chat(body.telegram_chat_id)
    except LookupError as exc:
        bot = f"@{c.telegram_bot_username}" if c.telegram_bot_username else "боту"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Не знаю {body.telegram_chat_id}: сначала напишите {bot} команду /start в Telegram",
        ) from exc
    incident = Incident(repo=repo, error_text=body.error_text, chat_id=chat_id, source=Source.WEB)
    run_id = await c.runner.start(incident)
    return {"id": run_id, "url": f"/runs/{run_id}"}


@router.get("/runs")
async def list_runs(c: ContainerDep, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[RunOut]:
    return await c.store.list_runs(limit)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, c: ContainerDep) -> RunDetail:
    run = await c.store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запуск не найден")
    return run


@router.post("/runs/{run_id}/decision", status_code=status.HTTP_202_ACCEPTED)
async def decide(run_id: str, body: Decision, c: ContainerDep) -> dict[str, str]:
    """Подтверждение на сайте. Если запрос ушёл в Telegram, решение принимается только там."""
    run = await c.store.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запуск не найден")
    if run.channel != Channel.WEB:
        raise HTTPException(status.HTTP_409_CONFLICT, "Решение по этому запуску ожидается в Telegram")
    try:
        await c.runner.resume(run_id, body.approved, actor="web", comment=body.comment)
    except RunConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"status": "accepted"}
