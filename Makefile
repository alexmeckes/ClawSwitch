.PHONY: install up down logs doctor print-openclaw install-local run-gateway-local run-local

install:
	./scripts/install.sh

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

doctor:
	./scripts/doctor.sh

print-openclaw:
	./scripts/print-openclaw-settings.sh

install-local:
	./scripts/install-local.sh

run-gateway-local:
	.venv/bin/any-llm-gateway serve --config gateway/config.yml

run-local:
	ANYLLM_BASE_URL=http://localhost:8000 ROUTER_MODEL_MAP_FILE=./router/models.yml .venv/bin/uvicorn router.app:app --host 0.0.0.0 --port 4000
