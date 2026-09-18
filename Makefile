.PHONY: setup serve serve-live describe build test test-all test-core test-analyze test-control lint fmt clean

setup:
	uv sync --all-extras

# MCP on stdio. Simulated by default: connecting is forced simulated and a
# request for real hardware is refused.
serve: setup
	PYLON_CAMEMU=2 uv run admet serve --simulated

serve-live: setup
	uv run admet serve

describe: setup
	uv run admet describe acquisition

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
