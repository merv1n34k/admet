.PHONY: setup dev describe build test test-all test-core test-analyze test-control test-integration lint fmt clean docs-dev docs-build publish-upstream
.DEFAULT_GOAL := setup

ANALYZE_TESTS := $(wildcard tests/test_analyze_*.py tests/test_cellpose_helpers.py tests/test_opencv_engine.py)
CONTROL_TESTS := $(wildcard tests/test_control_*.py tests/test_camera_backend.py tests/test_fluidics.py tests/test_observation.py tests/test_recording_limits.py)
INTEGRATION_TESTS := tests/test_core_service.py tests/test_json_protocol.py tests/test_operations.py tests/test_observe_operation.py tests/test_protocol_planning.py tests/test_flow_scout.py tests/test_qt_backend.py
CORE_TESTS := $(filter-out $(ANALYZE_TESTS) $(CONTROL_TESTS) $(INTEGRATION_TESTS),$(wildcard tests/test_*.py))
modules = $(patsubst tests/%.py,tests.%,$(1))

setup:
	uv sync --all-extras

dev:
	uv run --extra control admet control

describe:
	uv run admet describe acquisition

build:
	uv build

test: test-core test-control

test-all: test test-integration test-analyze

test-core:
	uv run -m unittest $(call modules,$(CORE_TESTS))

test-control:
	uv run -m unittest $(call modules,$(CONTROL_TESTS))

test-integration:
	uv run -m unittest $(call modules,$(INTEGRATION_TESTS))

test-analyze:
	uv run -m unittest $(call modules,$(ANALYZE_TESTS))

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

docs-dev:
	cd docs && bun install && bun run dev

docs-build:
	cd docs && bun install && bun run build

UPSTREAM ?= git@github.com:YP-Biotech/admet.git

# Push the committed master to UPSTREAM without the vendor folders, in every
# commit. The rewrite is deterministic, so this is an ordinary fast-forward push.
# With TAG=v..., also tag that master and push the tag, which starts a release.
publish-upstream:
	@tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	git clone -q --no-local --single-branch --branch master . "$$tmp" && \
	cd "$$tmp" && \
	uvx git-filter-repo --path src/admet/vendor --path admet/vendor --invert-paths --force > /dev/null && \
	if git log --name-only --format= | grep -q 'vendor/'; then echo "vendor paths remain; not pushing" >&2; exit 1; fi && \
	git push $(UPSTREAM) master && \
	if [ -n "$(TAG)" ]; then git tag -a "$(TAG)" -m "ADMET $(TAG)" master && git push $(UPSTREAM) "$(TAG)"; fi

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info docs/.vitepress/dist docs/.vitepress/cache
