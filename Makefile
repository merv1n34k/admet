.PHONY: setup dev describe build test test-all test-core test-analyze test-control test-integration test-desktop lint fmt clean
.DEFAULT_GOAL := setup

ANALYZE_TESTS := $(wildcard tests/test_analyze_*.py tests/test_cellpose_helpers.py tests/test_opencv_engine.py)
DESKTOP_TESTS := $(wildcard tests/test_qt_desktop.py tests/test_qt_theme.py)
CONTROL_TESTS := $(wildcard tests/test_control_*.py tests/test_camera_backend.py tests/test_fluidics.py tests/test_observation.py tests/test_recording_limits.py)
INTEGRATION_TESTS := tests/test_core_service.py tests/test_json_protocol.py tests/test_operations.py tests/test_observe_operation.py tests/test_protocol_planning.py tests/test_flow_scout.py tests/test_qt_backend.py
CORE_TESTS := $(filter-out $(ANALYZE_TESTS) $(DESKTOP_TESTS) $(CONTROL_TESTS) $(INTEGRATION_TESTS),$(wildcard tests/test_*.py))
modules = $(patsubst tests/%.py,tests.%,$(1))

setup:
	uv sync --all-extras

dev:
	uv run --extra control admet qt

describe:
	uv run admet describe acquisition

build:
	uv build

test: test-core test-control

test-all: test test-integration test-analyze test-desktop

test-core:
	uv run -m unittest $(call modules,$(CORE_TESTS))

test-control:
	uv run -m unittest $(call modules,$(CONTROL_TESTS))

test-integration:
	uv run -m unittest $(call modules,$(INTEGRATION_TESTS))

test-analyze:
	uv run -m unittest $(call modules,$(ANALYZE_TESTS))

test-desktop:
	uv run --extra control -m unittest $(call modules,$(DESKTOP_TESTS))

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
