.PHONY: env down run resume status test image net web

# Override with make run CONFIG=conf/qwen.yaml REPO=path/to/skill.
CONFIG ?= config.yaml
REPO ?= ./skillrepos
NPM ?= npm
PORT ?= 8000

env: net ## Start the local Phoenix tracing service.
	docker compose up -d

image: ## Build the verification sandbox image.
	docker build -t skillspec-sandbox:latest -f Dockerfile .

net: ## Create the shared sandbox network.
	docker network inspect skillspec-net >/dev/null 2>&1 || \
	docker network create --driver bridge --subnet 172.28.0.0/16 skillspec-net

down: ## Stop the local tracing service.
	docker compose stop

run: ## Analyze a skill repository.
	uv run skillspec --repo "$(REPO)" --config "$(CONFIG)"

resume: ## Explicitly resume saved progress.
	uv run skillspec --repo "$(REPO)" --config "$(CONFIG)" --resume

status: ## Read saved state without running analysis.
	uv run skillspec --repo "$(REPO)" --config "$(CONFIG)" --status

test: ## Run the test suite.
	uv run pytest -q

web-build:
	$(NPM) --prefix web run build

web:
	$(NPM) --prefix web run dev -- --port $(PORT)

web-check:
	$(NPM) --prefix web run check
	$(NPM) --prefix web test
