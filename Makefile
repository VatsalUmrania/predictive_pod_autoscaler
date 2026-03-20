.PHONY: help install install-dev test test-unit test-integration test-e2e lint format typecheck clean

help:
	@echo "PPA Development Commands"
	@echo "========================"
	@echo "  make install          Install package (production)"
	@echo "  make install-dev      Install with development dependencies"
	@echo "  make test             Run unit tests (fast)"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests"
	@echo "  make test-e2e         Run end-to-end tests"
	@echo "  make lint             Check code with ruff + mypy"
	@echo "  make format           Auto-format with black + ruff"
	@echo "  make typecheck        Run mypy type checking"
	@echo "  make clean            Remove build artifacts and caches"

install:
	pip install -e .

install-dev:
	pip install -e ".[dev]"

test:
	pytest tests/unit -v

test-unit:
	pytest tests/unit -v

test-integration:
	pytest tests/integration -v

test-e2e:
	pytest tests/e2e -v

lint:
	ruff check src tests
	mypy src/ppa --ignore-missing-imports

format:
	black src tests
	ruff check --fix src tests

typecheck:
	mypy src/ppa --ignore-missing-imports

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name *.egg-info -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist .coverage htmlcov
