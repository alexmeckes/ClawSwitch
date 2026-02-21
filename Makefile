.PHONY: install up down logs doctor print-openclaw

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
