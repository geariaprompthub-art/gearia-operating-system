.DEFAULT_GOAL := help

.PHONY: help up down test lint typecheck migrate logs smoke web-check

help:
	@echo "Targets: up down test lint typecheck migrate logs smoke web-check"

up:
	docker compose up --build -d

down:
	docker compose down

test:
	docker compose --profile tools run --rm --no-deps api-test pytest -q

lint:
	docker compose --profile tools run --rm --no-deps api-test python -m compileall -q app

typecheck:
	docker compose --profile tools run --rm --no-deps web-test npm run typecheck

web-check:
	docker compose --profile tools run --rm --no-deps web-test npm run lint

migrate:
	docker compose exec api alembic upgrade head

logs:
	docker compose logs -f --tail=100

smoke:
	docker compose exec api python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/health/live').read().decode())"
