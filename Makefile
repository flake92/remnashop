ALEMBIC_INI=src/infrastructure/database/alembic.ini
PYTHON ?= python3
DATABASE_HOST ?= 0.0.0.0
DATABASE_PORT ?= 6767

LOCAL_DB_ENV := DATABASE_HOST=$(DATABASE_HOST) DATABASE_PORT=$(DATABASE_PORT)

RESET := $(filter reset,$(MAKECMDGOALS))

.PHONY: setup-env
setup-env:
	@if [ ! -f .env ]; then cp .env.example .env; fi
	@set -eu; \
	ensure_secret() { \
		key="$$1"; value="$$2"; \
		current=$$(sed -n -E "s|^$${key}=(.*)$$|\1|p" .env | tail -n 1); \
		case "$$current" in \
			""|\"\"|\'\'|change_me|\"change_me\"|replace_with_*) ;; \
			*) return 0 ;; \
		esac; \
		if grep -Eq "^[#[:space:]]*$${key}=" .env; then \
			sed -E -i.bak "s|^[#[:space:]]*$${key}=.*|$${key}=$${value}|" .env; \
			rm -f .env.bak; \
		else \
			printf '%s=%s\n' "$$key" "$$value" >> .env; \
		fi; \
	}; \
	ensure_secret APP_CRYPT_KEY "$$(openssl rand -base64 32 | tr -d '\n')"; \
	ensure_secret APP_JWT_SECRET "$$(openssl rand -hex 32 | tr -d '\n')"; \
	ensure_secret APP_API_KEY "$$(openssl rand -hex 32 | tr -d '\n')"; \
	ensure_secret APP_AUTH_SERVICE_KEY "$$(openssl rand -hex 32 | tr -d '\n')"; \
	ensure_secret BOT_SECRET_TOKEN "$$(openssl rand -hex 64 | tr -d '\n')"; \
	ensure_secret REMNAWAVE_WEBHOOK_SECRET "$$(openssl rand -hex 32 | tr -d '\n')"; \
	ensure_secret DATABASE_PASSWORD "$$(openssl rand -hex 24 | tr -d '\n')"; \
	ensure_secret REDIS_PASSWORD "$$(openssl rand -hex 24 | tr -d '\n')"; \
	ensure_secret TASKIQ_DEPLOYMENT_ID "$$(openssl rand -hex 12 | tr -d '\n')"
	@echo "Environment scaffolding complete. Existing non-empty values were retained, not validated or rotated."
	@echo "This does not make an existing production deployment ready; run 'make production-preflight'."

.PHONY: production-preflight
production-preflight:
	@$(PYTHON) production_secret_preflight.py --env-file .env

# ── Run ────────────────────────────────────────────────────────────────────────

.PHONY: run
run: _run_local

.PHONY: run-local
run-local: _run_local

.PHONY: run-prod
run-prod: _run_prod

.PHONY: _run_local
_run_local:
ifneq ($(RESET),)
	@docker compose -f docker-compose.local.yml down -v
endif
	@docker compose -f docker-compose.local.yml up --build
	@docker compose -f docker-compose.local.yml logs -f

.PHONY: _run_prod
_run_prod: production-preflight
ifneq ($(RESET),)
	@echo "Refusing production reset: volume deletion must be an explicit, reviewed operation." >&2
	@exit 2
endif
	@grep -Eq '^REMNASHOP_IMAGE_REPOSITORY=[a-z0-9._-]+/[a-z0-9._-]+$$' .env || \
		{ echo "Invalid REMNASHOP_IMAGE_REPOSITORY in .env" >&2; exit 2; }
	@grep -Eq '^REMNASHOP_IMAGE_DIGEST=sha256:[0-9a-f]{64}$$' .env || \
		{ echo "Install REMNASHOP_IMAGE_DIGEST from remnashop-release.env" >&2; exit 2; }
	@grep -Eq '^TASKIQ_DEPLOYMENT_ID=.+$$' .env && \
		! grep -Eq '^TASKIQ_DEPLOYMENT_ID=change_me$$' .env || \
		{ echo "Run make setup-env to create TASKIQ_DEPLOYMENT_ID" >&2; exit 2; }
	@docker compose -f docker-compose.prod.external.yml pull
	@docker compose -f docker-compose.prod.external.yml up -d --remove-orphans --wait --wait-timeout 600
	@docker compose -f docker-compose.prod.external.yml logs -f

# ── Migrations ─────────────────────────────────────────────────────────────────

.PHONY: migration
migration:
	alembic -c $(ALEMBIC_INI) revision --autogenerate

.PHONY: migration-local
migration-local:
	$(LOCAL_DB_ENV) alembic -c $(ALEMBIC_INI) revision --autogenerate

.PHONY: migrate
migrate:
	alembic -c $(ALEMBIC_INI) upgrade head

.PHONY: migrate-local
migrate-local:
	$(LOCAL_DB_ENV) alembic -c $(ALEMBIC_INI) upgrade head

.PHONY: downgrade
downgrade:
	@if [ -z "$(rev)" ]; then \
		echo "No revision specified. Downgrading by 1 step."; \
		alembic -c $(ALEMBIC_INI) downgrade -1; \
	else \
		alembic -c $(ALEMBIC_INI) downgrade $(rev); \
	fi

.PHONY: downgrade-local
downgrade-local:
	@if [ -z "$(rev)" ]; then \
		echo "No revision specified. Downgrading by 1 step."; \
		$(LOCAL_DB_ENV) alembic -c $(ALEMBIC_INI) downgrade -1; \
	else \
		$(LOCAL_DB_ENV) alembic -c $(ALEMBIC_INI) downgrade $(rev); \
	fi

# ── Misc ───────────────────────────────────────────────────────────────────────

.PHONY: reset
reset:
	@:
