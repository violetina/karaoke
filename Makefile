SHELL := /bin/bash

VENV ?= .venv
PYTHON := $(VENV)/bin/python
MKDOCS := $(VENV)/bin/mkdocs

TOOLS_DIR := .tools/bin
CACHE_DIR := .cache
MAKE2GRAPH_DIR := $(CACHE_DIR)/makefile2graph
MAKE2GRAPH_BIN := $(TOOLS_DIR)/make2graph
MAKE2GRAPH_REPO := https://github.com/lindenb/makefile2graph.git
MAKE2GRAPH_REF ?= master

.DEFAULT_GOAL := help

.PHONY: help venv install install-confluence docs docs-live docs-write docs-confluence-prep \
        docs-confluence-publish deps-make2graph view_makeflow lint format \
        test test-audio mic-test stats clean clean-tools browse

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_.-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
	awk 'BEGIN {FS = ":.*?## "}; {printf "%-28s %s\n", $$1, $$2}'

venv: ## Create virtual environment
	python3 -m venv $(VENV)

install: venv ## Install dependencies and the karaoke package
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e ".[dev]"

install-confluence: install ## Install optional Confluence publishing dependencies
	$(PYTHON) -m pip install -r requirements-confluence.txt

deps-make2graph: ## Fetch and build makefile2graph locally
	@mkdir -p "$(TOOLS_DIR)" "$(CACHE_DIR)"
	@if [ ! -d "$(MAKE2GRAPH_DIR)/.git" ]; then \
		git clone "$(MAKE2GRAPH_REPO)" "$(MAKE2GRAPH_DIR)"; \
	fi
	@git -C "$(MAKE2GRAPH_DIR)" fetch --tags --force origin
	@git -C "$(MAKE2GRAPH_DIR)" checkout "$(MAKE2GRAPH_REF)"
	@$(MAKE) -C "$(MAKE2GRAPH_DIR)"
	@cp "$(MAKE2GRAPH_DIR)/make2graph" "$(MAKE2GRAPH_BIN)"
	@chmod +x "$(MAKE2GRAPH_BIN)"
	@echo "[ok] make2graph ready at $(MAKE2GRAPH_BIN)"

docs: ## Build MkDocs site
	$(MKDOCS) build

docs-live: ## Serve MkDocs locally
	$(MKDOCS) serve

docs-write: deps-make2graph ## Regenerate generated docs
	@mkdir -p docs/generated docs/assets || true
	echo  '# Makefile targets' > docs/makefile-targets.md
	echo 'Generated from make help' >> docs/makefile-targets.md
	echo '```text' >> docs/makefile-targets.md
	make help >> docs/makefile-targets.md
	echo '```' >> docs/makefile-targets.md
	@LC_ALL=C $(MAKE) -Bnd | "$(MAKE2GRAPH_BIN)" --format M > docs/generated/makeflow.mmd
	@LC_ALL=C $(MAKE) -Bnd | "$(MAKE2GRAPH_BIN)" --format d > docs/generated/makeflow.dot

	@if command -v dot >/dev/null 2>&1; then \
		dot -Tsvg docs/generated/makeflow.dot > docs/assets/makeflow.svg; \
		echo "[ok] wrote docs/assets/makeflow.svg"; \
	else \
		echo "[warn] graphviz 'dot' not found; skipping SVG render"; \
	fi

docs-confluence-prep: ## Generate Confluence-friendly docs tree
	$(PYTHON) tools/snippet_expander.py

docs-confluence-publish: docs-confluence-prep ## Build Confluence export site
	$(MKDOCS) build -f mkdocs-confluence.yml

view_makeflow: ## Open generated SVG locally
	xdg-open docs/assets/makeflow.svg

lint: ## Run lint checks
	$(PYTHON) -m compileall -q src tests

format: ## Run formatters
	@echo "Add ruff format / prettier here"

test: ## Run tests
	$(PYTHON) -m pytest -v

test-audio: ## Verify the audio + identify + lyrics stack (mic, songrec, LRCLIB)
	scripts/soundcheck.sh

mic-test: ## Live mic VU meter to confirm capture level (SECS=4)
	scripts/soundcheck.sh --meter $(or $(SECS),4)

stats: ## Show play + radio-discovery stats from the local cache
	$(PYTHON) -c "import sys; from karaoke.cli import stats_main; sys.argv=['karaoke-stats']; raise SystemExit(stats_main())"

clean-tools: ## Remove cached helper tools
	rm -rf "$(TOOLS_DIR)" "$(CACHE_DIR)"

clean: ## Remove build artifacts
	rm -rf site docs_confluence build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	
browse: ## Launch the interactive song browser TUI
	$(PYTHON) -m karaoke.browse
	
	
	
	