.PHONY: install up down migrate seed seed-docker test test-unit test-integration lint format postman docs

VENV = .venv
BIN = $(VENV)/bin

install:
	uv pip install -e ".[dev]"

up:
	docker compose up --build -d

down:
	docker compose down

migrate:
	$(BIN)/alembic upgrade head

seed:
	$(BIN)/python scripts/seed_forecasts.py

seed-docker:
	docker compose run --rm api python scripts/seed_forecasts.py

test:
	$(BIN)/pytest --cov=src/agrobot/domain --cov=src/agrobot/application --cov-report=term-missing --cov-fail-under=90

test-unit:
	$(BIN)/pytest tests/unit

test-integration:
	$(BIN)/pytest tests/integration tests/api

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy --strict src

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

postman:
	npx --yes newman run postman/agrobot.postman_collection.json -e postman/agrobot.local.postman_environment.json

docs:
	@echo "Contrato en http://localhost:8080" && python3 -m http.server 8080 -d docs
