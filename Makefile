.DEFAULT_GOAL := help

PYTHON ?= python3
NPM ?= npm
COMPOSE ?= docker compose

.PHONY: help install install-python install-web install-iac validate check \
	test test-backend test-crawler test-web test-iac build build-web \
	up down logs ps sources fixture-batch fixture-publish

help: ## Show the common repository commands.
	@printf '%s\n' \
		'Crowl repository commands' \
		'' \
		'  make install          Install Python, web, and Railway IaC dependencies' \
		'  make validate         Validate catalog data, photos, terminals, and sources' \
		'  make test             Run backend, crawler, web, and Railway IaC tests/checks' \
		'  make check            Run data validation and every test/check' \
		'  make build            Build API, web, and crawler Compose images' \
		'  make up               Build and start PostgreSQL, API, and web; wait for health' \
		'  make down             Stop the Compose stack without deleting database data' \
		'  make logs             Follow logs for the running default stack' \
		'  make fixture-publish  Publish the DB-backed synthetic six-month fixture batch' \
		'  make sources          Print the 14 requested provider slots and their states'

install: install-python install-web install-iac ## Install all development dependencies.

install-python: ## Install data, backend, core, and crawler Python dependencies.
	$(PYTHON) -m pip install -r requirements-dev.txt -r services/api/requirements-dev.txt
	$(PYTHON) -m pip install -e ./packages/crowl-core -e './services/crawler[test]'

install-web: ## Install the locked frontend dependencies.
	$(NPM) ci --prefix services/web

install-iac: ## Install the locked Railway IaC dependencies.
	$(NPM) ci --prefix .railway

validate: ## Validate the version-controlled production data contract.
	$(PYTHON) scripts/validate_data.py --production

test-backend: ## Run FastAPI, domain, database, and migration tests.
	PYTHONPATH=packages/crowl-core:services/api $(PYTHON) -m pytest tests/backend

test-crawler: ## Run crawler contracts, orchestration, fixtures, and persistence tests.
	PYTHONPATH=packages/crowl-core:services/crawler/src $(PYTHON) -m pytest tests/crawler

test-web: ## Run the React unit and API-contract tests.
	$(NPM) --prefix services/web test

test-iac: ## Type-check the Railway project definition.
	$(NPM) --prefix .railway run check

test: test-backend test-crawler test-web test-iac ## Run every application and IaC test/check.

check: validate test ## Run data validation followed by the complete test suite.

build: ## Build every Compose image, including the tools-profile crawler.
	$(COMPOSE) --profile tools build

build-web: ## Build the frontend bundle without Docker.
	$(NPM) --prefix services/web run build

up: ## Start the default local stack and wait for healthy services.
	$(COMPOSE) up --build -d --wait

down: ## Stop containers without deleting the PostgreSQL volume.
	$(COMPOSE) down

logs: ## Follow recent logs from PostgreSQL, API, and web.
	$(COMPOSE) logs --tail=200 -f postgres api web

ps: ## Show local Compose service state.
	$(COMPOSE) ps

sources: ## List the 14 requested provider slots and their current states.
	$(COMPOSE) --profile tools run --rm --no-deps crawler sources

fixture-batch: ## Run the side-effect-free synthetic crawler fixture.
	$(COMPOSE) --profile tools run --rm --no-deps crawler fixture-batch --pretty

fixture-publish: ## Prepare PostgreSQL/API, then publish synthetic fares transactionally.
	CROWL_SEED_FIXTURES=false $(COMPOSE) up --build -d --wait postgres api
	CROWL_SEED_FIXTURES=false $(COMPOSE) --profile tools run --rm crawler fixture-publish --pretty
