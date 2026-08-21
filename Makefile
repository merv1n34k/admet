ANALYZE_PORT ?= 8080

.PHONY: setup dev control analyze build test test-all test-core test-analyze test-control lint fmt clean

setup:
	uv sync --all-extras

dev: analyze

control: setup
	PYLON_CAMEMU=2 uv run admet control

analyze: setup
	uv run admet analyze --port $(ANALYZE_PORT)

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
