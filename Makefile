.PHONY: setup dev describe build test test-all test-core test-analyze test-control test-desktop lint fmt clean

setup:
	uv sync --all-extras

dev:
	uv run --extra control admet qt

describe: setup
	uv run admet describe acquisition

build:
	uv build

test: test-core

test-all: test-core test-analyze test-control test-desktop

test-core:
	uv run -m unittest discover -s tests

test-analyze:
	uv run -m unittest discover -s tests -p '*analyze*.py'

test-control:
	uv run -m unittest discover -s tests -p '*control*.py'

test-desktop:
	uv run --extra control -m unittest tests.test_qt_backend tests.test_qt_desktop

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
