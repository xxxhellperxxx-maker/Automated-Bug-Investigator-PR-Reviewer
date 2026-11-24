# REST API

Базовый адрес: `http://localhost:8000`. Интерактивная документация (Swagger): `/docs`, схема: `/openapi.json`.

| Метод | Путь | Описание |
|---|---|---|
| POST | `/api/v1/runs` | Запустить агента по тексту ошибки |
| GET | `/api/v1/runs?limit=50` | Список запусков |
| GET | `/api/v1/runs/{id}` | Запуск: статус, события, данные для одобрения, результат |
| POST | `/api/v1/runs/{id}/decision` | Одобрить или отклонить (только для канала `web`) |
| POST | `/api/v1/webhooks/github` | Вебхук GitHub `issues` (нужен `GITHUB_WEBHOOK_SECRET`) |
| GET | `/api/v1/status` | Какие интеграции настроены (без значений ключей), имя бота, список локальных проектов |
| GET | `/healthz` | Проверка живости |

## POST /api/v1/runs

```json
{
  "repo": "octocat/bug-demo",
  "error_text": "Traceback (most recent call last): ...",
  "telegram_chat_id": "123456789"
}
```

* `repo` — `owner/name`, ссылка на GitHub или `local:<папка>` для проекта из `LOCAL_PROJECTS_DIR` (результат — локальная ветка вместо PR). **Необязательно:** без `repo` агент делает диагноз по тексту ошибки — итог `diagnosed`, в `result.diagnosis` факты из трейса (`facts`) и гипотеза (`hypothesis`), `verified: false`.
* `telegram_chat_id` — необязательно: chat ID (число) или `@username`. `@username` работает, если человек написал боту `/start`. Если указан и бот настроен, одобрение придёт в Telegram; иначе — на сайте.

Ответ `202`: `{"id": "<run_id>", "url": "/runs/<run_id>"}`. Ошибки: `422` — неверный репозиторий, неверный chat ID или `@username`, который ещё не писал боту `/start`.

## GET /api/v1/runs/{id}

Статусы (`status`): `queued`, `running`, `awaiting_approval`, `completed`, `failed`.
Итог (`outcome`): `pr_ok` — PR создан и проверен; `pr_needs_work` — PR создан, проверка после PR не прошла или не настроена; `rejected` — отклонено человеком; `gave_up` — исправление не прошло проверку за отведённые попытки; `not_reproduced` — баг не удалось воспроизвести или дефект не найден (исправление не предлагается); `diagnosed` — диагноз по тексту ошибки без репозитория; `error` — сбой запуска.

```json
{
  "id": "…", "repo": "octocat/bug-demo", "source": "web", "channel": "web",
  "status": "awaiting_approval", "outcome": null, "pr_url": null, "tokens": 4210,
  "approval": {
    "file_path": "app/cart.py", "root_cause": "…", "explanation": "…", "diff": "--- a/app/cart.py …",
    "reproduction": "ошибка воспроизведена тестами: падали 2 (2 failed, 5 passed)",
    "ci_label": "баг исправлен: падавших тестов было 2, все проходят, новых падений нет (7 passed)",
    "attempt": 1, "verdict": "APPROVE", "security": [],
    "compare_url": "https://github.com/…/compare/main...ai-fix/…"
  },
  "result": null,
  "events": [{"ts": "…", "node": "investigate", "level": "info", "message": "Агент читает app/cart.py"}]
}
```

## POST /api/v1/runs/{id}/decision

```json
{"approved": true, "comment": "необязательно"}
```

Ответ `202`. Ошибки: `404` — нет запуска; `409` — запуск не ждёт решения (уже обработан) или решение ожидается в Telegram.

## Вебхук GitHub

Включается, если задан `GITHUB_WEBHOOK_SECRET`. В настройках репозитория: **Settings → Webhooks → Add webhook**, Payload URL `https://<адрес>/api/v1/webhooks/github`, Content type `application/json`, тот же Secret, событие **Issues**. Подпись `X-Hub-Signature-256` проверяется. Запуск стартует на `opened`/`labeled` с меткой `ISSUE_LABEL`; если задан `WATCHED_REPOS`, принимаются только эти репозитории. Без публичного адреса используйте опрос issues — он включается сам при заданном `WATCHED_REPOS`.
