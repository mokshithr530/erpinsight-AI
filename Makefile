.PHONY: help install-backend install-frontend install test run-backend run-frontend docker-up docker-down clean

help:
	@echo "ERPInsight-AI Development Commands:"
	@echo "  make install         Install backend and frontend dependencies"
	@echo "  make install-backend Install Python dependencies"
	@echo "  make install-frontend Install Node dependencies"
	@echo "  make test            Run pytest backend test suite"
	@echo "  make run-backend     Launch FastAPI backend locally on port 8000"
	@echo "  make run-frontend    Launch Vite React frontend locally on port 5173"
	@echo "  make docker-up       Spin up all services (backend, frontend, Redis, MySQL) via Docker"
	@echo "  make docker-down     Stop all Docker Compose services"
	@echo "  make clean           Remove cached files and temporary test databases"

install-backend:
	pip install -r requirements-dev.txt

install-frontend:
	npm install

install: install-backend install-frontend

test:
	pytest -v

run-backend:
	uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

run-frontend:
	npm run dev

docker-up:
	docker compose up --build

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache dist/ build/
