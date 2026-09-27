SHELL := /bin/bash
COMPOSE ?= docker compose
PORT    ?= 8090
PGUSER  ?= laya
PGDB    ?= laya

.DEFAULT_GOAL := help

.PHONY: help up down reset build logs ps gen-force psql size health scan stream \
        laya-health laya-questions decide probe bench bench-report fingerprint

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

laya-health: ## is the decision model loaded and reachable
	@curl -fsS localhost:$(PORT)/api/laya/health | python3 -m json.tool

laya-questions: ## the typed question schema sent to the model
	@curl -fsS localhost:$(PORT)/api/laya/questions | python3 -m json.tool

decide: ## decide one product-day with Laya and with the rules (PID=...)
	@test -n "$(PID)" || { echo "usage: make decide PID=2177"; exit 1; }
	@curl -fsS "localhost:$(PORT)/api/decide/$(PID)" | python3 scripts/decide_report.py

probe: ## measure Laya latency inside the model image
	docker run --rm -v laya-research_layamodels:/models -v "$(PWD)/scripts:/app/scripts:ro" \
	  --entrypoint python laya-research-laya:dev /app/scripts/laya_probe.py --mode sdk-pinned

bench: ## benchmark Laya vs the rules (LIMIT=150 CHECKPOINT=english)
	@echo "stopping sim so labels and states cannot drift during the run"
	-$(COMPOSE) stop sim
	python3 scripts/bench_laya.py --limit $(or $(LIMIT),150) --checkpoint $(or $(CHECKPOINT),english) \
	  $(if $(FRESH),--fresh,)
	-$(COMPOSE) start sim

fingerprint: ## content hash of the newest day of facts (detects a moving dataset)
	@python3 scripts/fingerprint.py --watch 3

bench-report: ## reprint the newest benchmark report from disk
	@ls -t bench/*.md 2>/dev/null | head -1 | xargs -r cat
