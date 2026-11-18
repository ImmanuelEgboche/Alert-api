.PHONY: help setup dev migrate migration test clean docker-build docker-up docker-down format lint

# Use the project venv when it exists, so make works without activating it first
BIN := $(if $(wildcard venv/bin/python),venv/bin/,)

help:
	@echo "Crypto Alert API - Available Commands"
	@echo "======================================"
	@echo "  make setup        - Initial project setup"
	@echo "  make dev          - Start development server"
	@echo "  make migrate      - Apply database migrations"
	@echo "  make migration m=\"add x\" - Generate a migration from model changes"
	@echo "  make test         - Run tests"
	@echo "  make test-cov     - Run tests with coverage"
	@echo "  make lint         - Run linters"
	@echo "  make format       - Format code"
	@echo "  make docker-build - Build Docker image"
	@echo "  make docker-up    - Start Docker services"
	@echo "  make docker-down  - Stop Docker services"
	@echo "  make clean        - Clean temporary files"

setup:
	@echo "Running setup script..."
	@bash scripts/setup.sh

dev:
	@echo "Starting development server..."
	@$(BIN)uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	@$(BIN)python -m src.migrate

migration:
	@test -n "$(m)" || (echo 'Usage: make migration m="describe the change"' && exit 1)
	@$(BIN)alembic revision --autogenerate -m "$(m)"

test:
	@echo "Running tests..."
	@$(BIN)pytest tests/ -v

test-cov:
	@echo "Running tests with coverage..."
	@$(BIN)pytest tests/ -v --cov=src --cov-report=html --cov-report=term

test-load:
	@echo "Running load tests..."
	@$(BIN)locust -f tests/load/locustfile.py --host=http://localhost:8000

lint:
	@echo "Running linters..."
	@$(BIN)ruff check src tests
	@$(BIN)mypy src

format:
	@echo "Formatting code..."
	@$(BIN)black src tests
	@$(BIN)ruff check --fix src tests

docker-build:
	@echo "Building Docker image..."
	@docker build -t crypto-alert-api:latest .

docker-up:
	@echo "Starting Docker services..."
	@docker-compose up -d

docker-down:
	@echo "Stopping Docker services..."
	@docker-compose down

docker-logs:
	@docker-compose logs -f api

clean:
	@echo "Cleaning temporary files..."
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name ".coverage" -delete
	@echo "Cleaned!"
