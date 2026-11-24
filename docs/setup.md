# Настройка: ключи и демо-репозиторий

Все значения кладутся в `.env` (скопируйте из `.env.example`). После изменения `.env` перезапустите сервис: `make up`.

## 1. Groq — `GROQ_API_KEY`

1. Откройте https://console.groq.com и войдите (Google или GitHub).
2. **API Keys** → **Create API Key** → скопируйте ключ `gsk_...` (показывается один раз).
3. Проверьте в разделе **Models**, что модели из `.env` доступны: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b` (проверено 17.11.2026). Список у Groq меняется: если какой-то модели нет — замените в `GROQ_MODEL_*` на доступную с поддержкой tool calling.

## 2. GitHub — `GITHUB_TOKEN`

1. GitHub → аватар → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Repository access** → **Only select repositories** → выберите демо-репозиторий.
3. **Repository permissions**:
   * Contents — Read and write (ветка, коммит);
   * Pull requests — Read and write;
   * Issues — Read and write (опрос issue, комментарии);
   * Actions — Read-only (результаты и логи тестов);
   * Metadata — Read-only (включается само).
4. **Generate token** → скопируйте `github_pat_...`.

Подойдёт и classic-токен с правом `repo`, но он даёт доступ ко всем вашим репозиториям — для постоянного использования лучше fine-grained.

Чтобы агент сам подхватывал issue, укажите репозитории в `WATCHED_REPOS=owner/name` и создайте в них метку `bug`.

## 3. Telegram (необязательно) — `TELEGRAM_BOT_TOKEN`

1. В Telegram: **@BotFather** → `/newbot` → имя → username на `bot` → скопируйте токен `123456:AA...`.
2. Откройте своего бота и нажмите **Start** — без этого бот не сможет вам писать.
3. Запустите сервис и отправьте своему боту `/start`. Бот запомнит чат и ответит вашим chat ID — искать его вручную не нужно.
4. На сайте в поле **Telegram** укажите свой `@username` (или присланный chat ID).
5. Для запусков из GitHub Issues одобрение уходит последнему, кто написал боту `/start`. Чтобы закрепить конкретный чат, задайте `TELEGRAM_DEFAULT_CHAT_ID`.

Бот работает через long polling. Если у бота когда-то был настроен вебхук (например, из n8n), сбросьте его, иначе Telegram вернёт ошибку 409: откройте в браузере `https://api.telegram.org/bot<ТОКЕН>/deleteWebhook`.

## 4. LangSmith (необязательно)

1. https://smith.langchain.com → **Settings** → **API Keys** → **Create API Key**.
2. В `.env`: `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=...`, при желании `LANGSMITH_PROJECT`.
3. Трейсы появятся в проекте `ai-bug-investigator`.

## 5. Песочница для тестов (Docker)

Агент запускает тесты исправляемого проекта в одноразовых Docker-контейнерах. В `docker-compose.yml` для этого уже настроены доступ к сокету Docker (`/var/run/docker.sock`) и группа `0`. Ничего делать не нужно, если сервис запускается через `make up`.

* Образы скачиваются при первом запуске на соответствующем языке. Чтобы первый запуск не ждал загрузку:

  ```bash
  docker pull python:3.12-slim node:22-slim golang:1.23-alpine
  ```

* Выключить песочницу: `SANDBOX_ENABLED=false` в `.env`. Тогда баг подтверждается только через GitHub Actions; без CI агент исправление не предлагает.
* Доступ к сокету Docker равен правам администратора хоста — поэтому сервис слушает только `127.0.0.1`.

## 6. Демо-репозитории

| Каталог | Язык | Баг | Тесты ловят баг? |
|---|---|---|---|
| [examples/demo-repo](../examples/demo-repo) | Python, pytest | `average_price([])` — деление на ноль | да |
| [examples/demo-js](../examples/demo-js) | JavaScript, Jest | `averagePrice([])` возвращает `NaN` | да |
| [examples/demo-go](../examples/demo-go) | Go, go test | `Average` делит на `len-1` | нет — агент напишет тест сам |

Создайте на GitHub новый репозиторий и загрузите содержимое нужного каталога, сохранив структуру. Если в демо-репозитории есть `.github/workflows`, GitHub Actions станут вторым, независимым контуром проверки после PR (для загрузки таких файлов токену нужно право `workflow`).

Готовые тексты ошибок — в README каждого каталога и в [docs/testing.md](testing.md).

## 7. Локальный режим (проекты с компьютера, без GitHub)

1. Положите git-репозиторий в папку `local-projects/` внутри проекта, например `local-projects/my-app` (папка должна содержать `.git`). Свою папку можно указать в `.env`: `LOCAL_PROJECTS_DIR=/полный/путь/к/папке`.
2. Перезапустите сервис: `make up`. В строке статуса на главной появится «Локальные проекты: local:my-app».
3. В форме укажите `local:my-app` и описание ошибки.

Как это устроено: папка подключается в контейнер как `/projects`. Агент работает с git напрямую: читает код из текущей ветки, архив для песочницы — `git archive`, изменения — новая ветка `ai-fix/...`, собранная низкоуровневыми командами git (`hash-object`, временный индекс, `commit-tree`, `update-ref`). Рабочие файлы, индекс и текущая ветка не меняются; агент удаляет только свои ветки `ai-fix/*`. Описание изменений сохраняется в git notes (`git notes --ref=ai-bug-investigator show ai-fix/<id>`). Ключ GitHub для локального режима не нужен, CI нет — проверка только в песочнице.

## 8. Локальный запуск без Docker

Нужен Python 3.12:

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
```

```bash
uvicorn app.main:app --reload
```
