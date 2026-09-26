SHELL := /bin/bash
COMPOSE ?= docker compose
PORT    ?= 8090
PGUSER  ?= laya
PGDB    ?= laya

.DEFAULT_GOAL := help

.PHONY: help up down reset build logs ps gen-force psql size health scan stream

help: ## list targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## build + start the whole stack
	$(COMPOSE) up --build -d
	@echo "edge -> http://localhost:$(PORT)"

down: ## stop, keep the datasheet volume
	$(COMPOSE) down --remove-orphans

reset: ## stop and DESTROY the datasheet volume
	$(COMPOSE) down --remove-orphans -v

build: ## build images only
	$(COMPOSE) build

logs: ## follow all logs
	$(COMPOSE) logs -f --tail=100

ps: ## container state
	$(COMPOSE) ps

gen-force: ## regenerate the datasheet from LAYA_SEED (drops facts + signals)
	LAYA_MODE=force $(COMPOSE) up --force-recreate generator
	$(COMPOSE) logs --tail=60 generator

psql: ## open an interactive psql on the datasheet
	$(COMPOSE) exec db psql -U $(PGUSER) -d $(PGDB)

size: ## datasheet row counts, day range, on-disk size
	$(COMPOSE) exec -T db psql -U $(PGUSER) -d $(PGDB) -c "\
	SELECT (SELECT count(*) FROM market_facts) AS facts,\
	       (SELECT count(*) FROM products)     AS products,\
	       (SELECT count(*) FROM stores)       AS stores,\
	       (SELECT min(day) FROM market_facts) AS day_min,\
	       (SELECT max(day) FROM market_facts) AS day_max,\
	       pg_size_pretty(pg_total_relation_size('market_facts')) AS size;"

health: ## probe api + web through the edge
	@curl -fsS localhost:$(PORT)/api/health | python3 -m json.tool
	@curl -fsS -o /dev/null -w 'web   %{http_code}\n' localhost:$(PORT)/

scan: ## full rule scan, persisted, with a readable report
	@curl -fsS -X POST localhost:$(PORT)/api/patterns/scan \
	  -H 'content-type: application/json' -d '{"persist":true}' \
	  | python3 scripts/scan_report.py

stream: ## tail the realtime websocket for 25s
	@LAYA_WS=ws://localhost:$(PORT)/api/stream python3 scripts/ws_probe.py --seconds 25
