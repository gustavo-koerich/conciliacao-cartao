# Atalhos de operação. Em produção (servidor da Central) use: make up PROD=1
COMPOSE_ARQS := -f docker-compose.yml
ifdef PROD
COMPOSE_ARQS += -f docker-compose.central.yml
endif
DC := docker compose $(COMPOSE_ARQS)

.PHONY: up down logs test migrate build shell backup-agora restaurar testar-restauracao

up:
	$(DC) up -d --build

down:
	$(DC) down

logs:
	$(DC) logs -f --tail=200

build:
	$(DC) build

# Roda os testes num container descartável, contra o Postgres do compose.
test: build
	$(DC) run --rm -e DJANGO_DEBUG=true -e LOG_DIR=/tmp/logs-teste web pytest $(ARGS)

migrate:
	$(DC) run --rm web python manage.py migrate --noinput

shell:
	$(DC) exec web python manage.py shell

# ---------- Backup (implementados na etapa 3) ----------
backup-agora:
	@echo "Etapa 3: serviço de backup ainda não implementado." && exit 1

restaurar:
	@test -n "$(ARQUIVO)" || (echo "Uso: make restaurar ARQUIVO=caminho/do/backup.tar.age" && exit 1)
	@echo "Etapa 3: restauração ainda não implementada." && exit 1

testar-restauracao:
	@echo "Etapa 3: teste de restauração ainda não implementado." && exit 1
