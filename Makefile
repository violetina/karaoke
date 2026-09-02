SHELL := /bin/bash

VENV ?= .venv
PYTHON := PYTHONPATH=src $(VENV)/bin/python
MKDOCS := $(VENV)/bin/mkdocs
AUDIO_VENV ?= .venv-audio
AUDIO_PY := $(AUDIO_VENV)/bin/python

TOOLS_DIR := .tools/bin
CACHE_DIR := .cache
MAKE2GRAPH_DIR := $(CACHE_DIR)/makefile2graph
MAKE2GRAPH_BIN := $(TOOLS_DIR)/make2graph
MAKE2GRAPH_REPO := https://github.com/lindenb/makefile2graph.git
MAKE2GRAPH_REF ?= master

# Deployment (kind cluster)
IMAGE ?= karaoke-api:dev
KIND_CLUSTER ?= karaoke
KUBE_CONTEXT ?= kind-karaoke
K8S_NAMESPACE ?= karaoke

.DEFAULT_GOAL := help

.PHONY: help venv install install-confluence docs docs-live docs-write docs-confluence-prep \
        docs-confluence-publish deps-make2graph view_makeflow lint format \
        test test-audio mic-test stats clean clean-tools browse tui browse-log \
        install-audio analyze api ctrl-api \
        k8s-build k8s-load k8s-deploy k8s-seed-db k8s-status k8s-logs k8s-undeploy \
        index-youtube-cache vector-index vector-index-dry-run

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

install-audio: ## Install the isolated key/tempo analysis stack (essentia, librosa) into $(AUDIO_VENV)
	python3 -m venv $(AUDIO_VENV)
	$(AUDIO_PY) -m pip install --upgrade pip
	$(AUDIO_PY) -m pip install -r requirements-audio.txt
	@echo "[ok] audio stack ready in $(AUDIO_VENV) — set KARAOKE_AUDIO_PYTHON=$(PWD)/$(AUDIO_PY)"

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

tui: ## Launch the clean karaoke control-surface TUI prototype
	$(PYTHON) -m karaoke.tui

analyze: ## Detect + store key/BPM for a file (FILE=... ARTIST=... TITLE=...)
	$(PYTHON) -c "import sys; from karaoke.cli import analyze_main; sys.exit(analyze_main(['--file','$(FILE)','--artist','$(ARTIST)','--title','$(TITLE)']))"

browse-log: ## Follow TUI/open debug logs
	tail -f "$${XDG_DATA_HOME:-$$HOME/.local/share}/karaoke/logs/karaoke.log" "$${XDG_DATA_HOME:-$$HOME/.local/share}/karaoke/logs/xdg-open.stderr.log"

api: ## Launch the FastAPI library backend (read-only: tracks, lyrics, stats)
	$(PYTHON) -m karaoke.api

ctrl-api: ## Launch the host-side control API (playback; needs a desktop session)
	$(PYTHON) -m karaoke.ctrl_api

k8s-build: ## Build the library API container image
	# --network=host: the default docker bridge has no working DNS on this host,
	# so pip cannot resolve pypi.org during the build without it.
	docker build --network=host -t $(IMAGE) -f deploy/Dockerfile .

k8s-load: k8s-build ## Load the image into the kind cluster
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)

k8s-deploy: k8s-load ## Deploy the library API to the kind cluster
	kubectl --context $(KUBE_CONTEXT) apply -k deploy/k8s
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) rollout status deploy/karaoke-api --timeout=120s

k8s-seed-db: ## Copy the local SQLite library into the cluster PVC
	./scripts/seed_db.sh

k8s-status: ## Show deployed karaoke resources
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) get all,pvc

k8s-port-forward: ## Expose the library API on http://localhost:8080
	# The pre-existing kind cluster only maps the OpenSearch NodePort. Recreating
	# it to add a mapping would destroy the OpenSearch release, so forward instead.
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) port-forward svc/karaoke-api 8080:8000

k8s-logs: ## Follow library API pod logs
	kubectl --context $(KUBE_CONTEXT) -n $(K8S_NAMESPACE) logs -f deploy/karaoke-api

k8s-undeploy: ## Remove the karaoke API from the cluster (keeps the PVC)
	kubectl --context $(KUBE_CONTEXT) delete -k deploy/k8s --ignore-not-found

index-youtube-cache: ## Add cached YouTube downloads to SQLite so they show in browse
	$(PYTHON) scripts/index_youtube_cache.py

vector-index-dry-run: ## Preview SQLite -> OpenSearch vector indexing without writing
	$(PYTHON) -m karaoke.vector_index --dry-run --no-embed --lines

vector-index: ## Rebuild OpenSearch vector indexes from SQLite (set LINES=1 for line docs)
	$(PYTHON) -m karaoke.vector_index --rebuild $(if $(LINES),--lines,)