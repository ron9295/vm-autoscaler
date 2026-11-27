# vm-autoscaler developer tasks. Requires Python 3.12 (or `uv`, which can fetch it).
PYTHON ?= python3.12
VENV   ?= .venv
BIN     = $(VENV)/bin
IMAGE  ?= vm-autoscaler:dev
ENGINE ?= $(shell command -v podman 2>/dev/null || command -v docker)
PORT   ?= 8080

.PHONY: help venv install lint format typecheck test check run sample image lock clean

help:
	@echo "make install | lint | format | typecheck | test | check | run | sample | image | lock"

$(BIN)/python:
	@if command -v uv >/dev/null 2>&1; then uv venv --python 3.12 $(VENV); \
	else $(PYTHON) -m venv $(VENV); fi

venv: $(BIN)/python

install: venv
	@if command -v uv >/dev/null 2>&1; then uv pip install --python $(BIN)/python -r requirements-dev.txt; \
	else $(BIN)/pip install -r requirements-dev.txt; fi

lint:
	$(BIN)/ruff check app tests
	$(BIN)/ruff format --check app tests

format:
	$(BIN)/ruff format app tests
	$(BIN)/ruff check --fix app tests

typecheck:
	$(BIN)/mypy --strict app

test:
	$(BIN)/pytest -q

check: lint typecheck test

# Local run: fake GitOps backend seeded from examples/, fake Argo, dry_run config.
run:
	WEBHOOK_TOKEN=dev-token \
	CONFIG_PATH=config.example.yaml \
	GITOPS_BACKEND=fake \
	GITOPS_FAKE_SEED_PATH=examples/fake-state.yaml \
	ARGOCD_SYNC_METHOD=fake \
	LOG_LEVEL=INFO \
	$(BIN)/uvicorn app.main:create_app --factory --host 127.0.0.1 --port $(PORT)

# Send the sample firing CPU alert to a running `make run`.
sample:
	curl -sS -X POST http://127.0.0.1:$(PORT)/webhook/grafana \
	  -H 'Authorization: Bearer dev-token' \
	  -H 'Content-Type: application/json' \
	  --data @tests/fixtures/grafana_firing_cpu.json | $(BIN)/python -m json.tool

image:
	$(ENGINE) build -t $(IMAGE) .

# Re-pin dependencies (needs network access to an index / mirror).
lock:
	uv pip compile --python-version 3.12 --generate-hashes requirements.in -o requirements.txt
	uv pip compile --python-version 3.12 requirements-dev.in -o requirements-dev.txt

clean:
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache
