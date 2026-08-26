SHELL := /usr/bin/env bash
PYTHON ?= python3
PROJECT ?= .
PROFILE ?= vl-convert-1.9.0
FAMILY ?= benizar
ENGINE ?= auto
FORMAT ?=
INPUT ?=
OUTPUT ?=
REQUIRE_DOCKER ?= 0
override MCP_VENV := .cache/vegavisuals/mcp-venv
MCP_PYTHON := $(MCP_VENV)/bin/python
MCP_CLI := $(MCP_VENV)/bin/vegavisuals
MCP_STAMP := $(MCP_VENV)/.installed
MCP_VERSION := 1.29.0
CONTAINER_LABEL := io.context.mcp-factory=vegavisuals
override PROJECT := $(value PROJECT)
override PROFILE := $(value PROFILE)
override FAMILY := $(value FAMILY)
override ENGINE := $(value ENGINE)
override FORMAT := $(value FORMAT)
override INPUT := $(value INPUT)
override OUTPUT := $(value OUTPUT)
export PROJECT PROFILE FAMILY ENGINE FORMAT INPUT OUTPUT

.PHONY: help build check test tests tests-install renderer-build docker-smoke mcp-env mcp-build mcp-init mcp-check mcp-stdio mcp-smoke mcp-down render-image clean

help:
	@printf '%s\n' \
	  'make build          Build the Python wheel' \
	  'make check          Validate packaged assets and both example sources' \
	  'make tests          Run fast tests with mocked Docker execution' \
	  'make tests-install  Verify a non-editable wheel and packaged assets' \
	  'make renderer-build Build the pinned vl-convert Docker image' \
	  'make docker-smoke   Render SVG, PNG, and PDF with both engines when Docker is available' \
	  'make mcp-build      Bootstrap the MCP environment and renderer for PROJECT' \
	  'make mcp-init       Initialize PROJECT without overwriting existing files' \
	  'make mcp-check      Validate the MCP install, factory, and PROJECT' \
	  'make mcp-stdio      Serve MCP over stdio for PROJECT' \
	  'make mcp-smoke      Exercise MCP stdio and both real Docker engines' \
	  'make render-image INPUT=... OUTPUT=... [FORMAT=svg]'

build:
	@rm -rf dist
	@rm -rf .tmp/build-venv
	@mkdir -p .tmp
	@$(PYTHON) -m venv .tmp/build-venv
	@.tmp/build-venv/bin/python -m pip install --disable-pip-version-check build==1.3.0 >/dev/null
	@.tmp/build-venv/bin/python -m build --sdist --wheel --outdir dist .

check:
	@PYTHONPATH=src $(PYTHON) -m py_compile src/vegavisuals/*.py src/vegavisuals/assets/docker/worker.py
	@PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project . factory-check >/dev/null
	@PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project . validate examples/vega-lite/bar.vl.json >/dev/null
	@PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project . validate examples/vega/raw.vg.json >/dev/null
	@PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project . check >/dev/null

tests: check
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests

test: tests

tests-install: build
	@rm -rf .tmp/install-venv
	@mkdir -p .tmp
	@sdists=(dist/vegavisuals-*.tar.gz); test -f "$${sdists[0]}"
	@wheels=(dist/vegavisuals-*.whl); case "$${wheels[0]}" in *-linux_*.whl) ;; *) printf 'Wheel is not Linux-tagged: %s\n' "$${wheels[0]}" >&2; exit 1 ;; esac
	@wheels=(dist/vegavisuals-*.whl); WHEEL_PATH="$${wheels[0]}" $(PYTHON) -c 'import os,zipfile; wheel=os.environ["WHEEL_PATH"]; archive=zipfile.ZipFile(wheel); metadata=archive.read(next(name for name in archive.namelist() if name.endswith(".dist-info/WHEEL"))).decode(); assert "Root-Is-Purelib: false" in metadata; assert "Tag: py3-none-linux_" in metadata'
	@wheels=(dist/vegavisuals-*.whl); WHEEL_PATH="$${wheels[0]}" $(PYTHON) -c 'import os,zipfile; archive=zipfile.ZipFile(os.environ["WHEEL_PATH"]); names=archive.namelist(); metadata=archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))).decode(); assert "License-Expression: GPL-3.0-only" in metadata; assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names); assert any(name.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md") for name in names)'
	@sdists=(dist/vegavisuals-*.tar.gz); SDIST_PATH="$${sdists[0]}" $(PYTHON) -c 'import os,tarfile; names=tarfile.open(os.environ["SDIST_PATH"]).getnames(); assert any(name.endswith("/LICENSE") for name in names); assert any(name.endswith("/THIRD_PARTY_NOTICES.md") for name in names)'
	@rm -rf .tmp/windows-wheel-check
	@if $(PYTHON) -m pip download --disable-pip-version-check --no-deps --no-index --find-links dist --only-binary=:all: --dest .tmp/windows-wheel-check --platform win_amd64 --implementation cp --python-version 312 --abi cp312 vegavisuals >/dev/null 2>&1; then printf 'Linux-only wheel was selected for Windows.\n' >&2; exit 1; fi
	@rm -rf .tmp/sdist-wheel
	@sdists=(dist/vegavisuals-*.tar.gz); $(PYTHON) -m pip wheel --disable-pip-version-check --no-deps --wheel-dir .tmp/sdist-wheel "$${sdists[0]}" >/dev/null
	@$(PYTHON) -m venv .tmp/install-venv
	@wheels=(.tmp/sdist-wheel/vegavisuals-*.whl); .tmp/install-venv/bin/python -m pip install --disable-pip-version-check "$${wheels[0]}[mcp]" >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/vegavisuals --project . version >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/vegavisuals --project . install-check --command "$${PWD}/.tmp/install-venv/bin/vegavisuals" >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/python -m vegavisuals.cli --project . install-check >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/python -m vegavisuals.cli --project . lifecycle-check >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/python -c 'import sys; from vegavisuals import Registry; manifest=Registry(".").factory_manifest(); command=manifest["transport"]["command"]; assert command[:3] == [sys.executable, "-m", "vegavisuals.cli"]; assert "make" not in command'
	@env -u PYTHONPATH .tmp/install-venv/bin/vegavisuals --project . factory-check >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/vegavisuals --project . check >/dev/null
	@env -u PYTHONPATH .tmp/install-venv/bin/vegavisuals --project . render examples/vega-lite/bar.vl.json .cache/vegavisuals/install-check.svg --dry-run >/dev/null
	@env -u PYTHONPATH VEGAVISUALS_MCP_SMOKE=1 VEGAVISUALS_MCP_COMMAND="$${PWD}/.tmp/install-venv/bin/vegavisuals" .tmp/install-venv/bin/python -m unittest tests.test_mcp_stdio

renderer-build:
	@PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project "$${PROJECT}" build-renderer --profile "$${PROFILE}" >/dev/null

docker-smoke:
	@if docker info >/dev/null 2>&1; then \
	  VEGAVISUALS_DOCKER_SMOKE=1 PYTHONPATH=src $(PYTHON) -m unittest tests.test_docker_smoke; \
	elif [[ "$(REQUIRE_DOCKER)" == "1" ]]; then \
	  printf '%s\n' 'Docker is required but unavailable.' >&2; exit 1; \
	else \
	  printf '%s\n' 'Docker is unavailable; real renderer smoke skipped.'; \
	fi

mcp-env: $(MCP_STAMP)

$(MCP_STAMP): pyproject.toml
	@mkdir -p "$(dir $(MCP_VENV))"
	@$(PYTHON) -m venv "$(MCP_VENV)"
	@"$(MCP_PYTHON)" -m pip install --disable-pip-version-check --editable '.[mcp]' "mcp==$(MCP_VERSION)" >/dev/null
	@touch "$@"

mcp-build: mcp-env
	@"$(MCP_CLI)" --project "$${PROJECT}" install-check --command "$${PWD}/$(MCP_CLI)" >/dev/null
	@"$(MCP_CLI)" --project "$${PROJECT}" ensure-renderer --profile "$${PROFILE}" >/dev/null

mcp-init: mcp-env
	@"$(MCP_CLI)" --project "$${PROJECT}" init >/dev/null

mcp-check: mcp-env
	@"$(MCP_CLI)" --project "$${PROJECT}" install-check --command "$${PWD}/$(MCP_CLI)" >/dev/null
	@"$(MCP_CLI)" --project "$${PROJECT}" factory-check --profile "$${PROFILE}" --family "$${FAMILY}" >/dev/null
	@"$(MCP_CLI)" --project "$${PROJECT}" check >/dev/null

mcp-stdio: mcp-build
	@"$(MCP_CLI)" --project "$${PROJECT}" mcp serve

mcp-smoke: mcp-build
	@VEGAVISUALS_MCP_SMOKE=1 VEGAVISUALS_MCP_COMMAND="$${PWD}/$(MCP_CLI)" "$(MCP_PYTHON)" -m unittest tests.test_mcp_stdio
	@VEGAVISUALS_MCP_SMOKE=1 VEGAVISUALS_MCP_COMMAND=bash VEGAVISUALS_MCP_FACTORY_ROOT="$${PWD}" "$(MCP_PYTHON)" -m unittest tests.test_mcp_stdio
	@if docker info >/dev/null 2>&1; then \
	  VEGAVISUALS_DOCKER_SMOKE=1 "$(MCP_PYTHON)" -m unittest tests.test_docker_smoke; \
	elif [[ "$(REQUIRE_DOCKER)" == "1" ]]; then \
	  printf '%s\n' 'Docker is required but unavailable.' >&2; exit 1; \
	else \
	  printf '%s\n' 'Docker is unavailable; real renderer smoke skipped.'; \
	fi

mcp-down:
	@containers="$$(docker container ls --all --quiet --filter 'label=$(CONTAINER_LABEL)')"; \
	if [[ -n "$$containers" ]]; then docker container rm --force $$containers >/dev/null; fi

render-image:
	@test -n "$${INPUT}" || (printf '%s\n' 'INPUT is required' >&2; exit 2)
	@test -n "$${OUTPUT}" || (printf '%s\n' 'OUTPUT is required' >&2; exit 2)
	@format_args=(); if [[ -n "$${FORMAT}" ]]; then format_args=(--format "$${FORMAT}"); fi; \
	PYTHONPATH=src $(PYTHON) -m vegavisuals.cli --project "$${PROJECT}" render \
	  "$${INPUT}" "$${OUTPUT}" --engine "$${ENGINE}" --profile "$${PROFILE}" --family "$${FAMILY}" "$${format_args[@]}"

clean:
	@rm -rf build dist .tmp .cache src/vegavisuals/__pycache__ src/vegavisuals/assets/docker/__pycache__ tests/__pycache__
