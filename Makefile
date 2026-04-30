# Makefile for Wire. Cross-platform-ish — Unix uses make, Windows uses Make.ps1.

.PHONY: help install lint format format-check test test-fast check verify inspect redeploy clean

WIRE_PROJECT_PREFIX := j13i32n8rrvzsxpydl404f6v
SSH_TARGET ?= johan@gary

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## uv sync (runtime + dev deps)
	uv sync

lint:  ## Ruff lint
	uv run ruff check src tests

format:  ## Ruff format (in-place)
	uv run ruff format src tests

format-check:  ## Ruff format --check (no rewrite)
	uv run ruff format --check src tests

test:  ## Full pytest suite
	uv run pytest -q

test-fast:  ## Stop on first failure, short tracebacks
	uv run pytest -q -x --tb=short

typecheck:  ## mypy (non-blocking; useful locally)
	uv run mypy src || true

check: lint format-check test  ## Lint + format check + tests (what CI runs)

verify: install lint format-check test typecheck  ## Full CI equivalent (incl. typecheck)

inspect:  ## Run wire.scripts.inspect 24 against production
	ssh $(SSH_TARGET) 'docker exec $$(docker ps --filter name=$(WIRE_PROJECT_PREFIX) -q) python -m wire.scripts.inspect 24'

logs:  ## Tail production logs (Ctrl+C to stop)
	ssh $(SSH_TARGET) 'docker logs --tail 50 -f $$(docker ps --filter name=$(WIRE_PROJECT_PREFIX) -q)'

redeploy:  ## Push current branch + remind about Coolify Deploy click
	git push
	@echo ""
	@echo "Now click Deploy in Coolify: https://gary.winetrackr.app (or wherever your Coolify lives)"

clean:  ## Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
