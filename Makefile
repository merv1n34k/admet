CONTROL_ENV ?= .venv
CONTROL_PYTHON ?= 3.12-x86_64
ANALYZE_ENV ?= .venv-arm64
ANALYZE_PYTHON ?= 3.12
ANALYZE_PORT ?= 8080

.PHONY: setup dev control analyze build test test-all test-core test-analyze test-control lint fmt clean

setup:
	uv sync

dev: analyze

control:
	UV_PROJECT_ENVIRONMENT=$(CONTROL_ENV) UV_PYTHON=$(CONTROL_PYTHON) uv sync --extra control
	UV_PROJECT_ENVIRONMENT=$(CONTROL_ENV) UV_PYTHON=$(CONTROL_PYTHON) uv run admet control

analyze:
	arch -arm64 /usr/bin/env UV_PROJECT_ENVIRONMENT=$(ANALYZE_ENV) UV_PYTHON=$(ANALYZE_PYTHON) uv sync --extra analyze
	arch -arm64 /usr/bin/env UV_PROJECT_ENVIRONMENT=$(ANALYZE_ENV) UV_PYTHON=$(ANALYZE_PYTHON) uv run admet analyze --port $(ANALYZE_PORT)

build:
	uv build

test: test-core

test-all: test-core test-analyze test-control

test-core:
	uv run -m unittest discover -s tests

test-analyze:
	uv run -m unittest discover -s tests -p '*analyze*.py'

test-control:
	uv run -m unittest discover -s tests -p '*control*.py'

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
