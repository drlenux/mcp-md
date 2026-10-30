SHELL := /bin/sh
.DEFAULT_GOAL := install
MAKEFLAGS += --no-print-directory

PYTHON ?= python3
WITH_QMD ?= 1
ARGS ?=

.PHONY: install clear run

install:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -e .
	@if [ "$(WITH_QMD)" = "1" ] && [ ! -x .tools/qmd/node_modules/.bin/qmd ]; then \
		npm install --prefix .tools/qmd --no-audit --no-fund @tobilu/qmd; \
	fi
	@if [ ! -e .env ] && [ ! -L .env ]; then cp .env.example .env; fi

# Preserve campaign data, configuration, QMD and downloaded models.
clear:
	rm -rf .venv build dist .pytest_cache .ruff_cache .mypy_cache htmlcov
	rm -f .coverage .coverage.*
	find src tests -type d \( -name __pycache__ -o -name '*.egg-info' \) -prune -exec rm -rf {} +
	find src tests -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

# Keep stdout reserved for MCP JSON-RPC, including when called with make -C.
run:
	@test -x .venv/bin/python || { echo 'Run make install first.' >&2; exit 1; }
	@exec .venv/bin/python -m gpt_parser_mcp $(ARGS)
