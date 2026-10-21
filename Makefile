# Development entry points. The commands mirror .github/workflows/ci.yml so
# "run it locally" and "run it in CI" cannot drift apart.
VENV ?= .venv
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
else
BIN := $(VENV)/bin
endif

PYTEST := $(BIN)/pytest
RUFF := $(BIN)/ruff
MYPY := $(BIN)/mypy
MKDOCS := $(BIN)/mkdocs

.DEFAULT_GOAL := help

.PHONY: help test lint typecheck docs check

help:
	@echo "make test       Run the test suite with CI's coverage gate ($(PYTEST))"
	@echo "make lint       Run ruff check . and ruff format --check . (CI lint job)"
	@echo "make typecheck  Run mypy (CI lint job)"
	@echo "make docs       Build the bilingual documentation with --strict"
	@echo "make check      lint + typecheck + test"

test:
	$(PYTEST) --cov=downctl --cov-report=term --cov-fail-under=85

lint:
	$(RUFF) check .
	$(RUFF) format --check .

typecheck:
	$(MYPY)

docs:
	$(MKDOCS) build --strict

check: lint typecheck test
