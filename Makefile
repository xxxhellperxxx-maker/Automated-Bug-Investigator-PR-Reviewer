.PHONY: up down logs build test lint fmt run

up:            ## Запустить сервис на http://localhost:8000
	docker compose up -d --build --wait

down:
	docker compose down

logs:
	docker compose logs -f app

build:
	docker compose build

test:          ## Тесты (pytest) в контейнере Python 3.12
	docker compose --profile test run --rm --build tests

lint:          ## Ruff
	docker compose --profile test run --rm --build tests ruff check app tests

fmt:
	docker compose --profile test run --rm --build tests ruff format app tests

run:           ## Локальный запуск без Docker (нужен Python 3.12 и .venv)
	uvicorn app.main:app --reload
