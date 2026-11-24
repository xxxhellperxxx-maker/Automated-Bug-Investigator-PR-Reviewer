import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
    run_id: str
    incident: dict[str, Any]  # Incident.model_dump()
    channel: str  # web | telegram

    # intake / load_repo
    repo: str
    frames: list[dict[str, Any]]
    default_branch: str
    base_sha: str
    tree: list[str]
    candidates: list[dict[str, Any]]
    has_actions: bool
    baseline: dict[str, Any] | None  # тесты исходного кода в песочнице (воспроизведение)
    test_plan: dict[str, str] | None  # язык и тест-раннер песочницы (languages.TestPlan.spec)
    repro_mode: str  # tests | write_test | ci | none — чем подтверждается баг
    reproduced: bool
    repro_test: dict[str, Any] | None  # регрессионный тест агента: path, content, ids
    repro_committed: bool

    # investigate
    finding: dict[str, str] | None  # file_path, root_cause, fix_plan; None — дефект не найден
    no_defect_reason: str

    # fix / commit / verify / reflect (петля самопроверки)
    branch: str | None
    original_content: str
    file_sha: str
    attempt: int
    feedback: str
    previous_fix: str
    proposal: dict[str, str] | None
    fix_error: str | None
    commit_sha: str
    ci: dict[str, Any]
    route: str

    # review / approval / PR
    review: dict[str, Any]
    decision: dict[str, Any]
    pr: dict[str, Any]
    post_ci: dict[str, Any]

    diagnosis: dict[str, Any]  # режим без репозитория: факты из трейса + гипотеза модели
    outcome: str
    report: dict[str, Any]
    tokens: Annotated[int, operator.add]
