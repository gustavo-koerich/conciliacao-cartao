#!/bin/bash
# Roda só na primeira inicialização do volume pgdata.
# Cria o usuário da aplicação (dono do schema "conciliacao") e o usuário somente leitura do BI.
# As permissões do BI nas views (schema "conciliacao_bi") são concedidas pela migration das views (etapa 11).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres --dbname "$POSTGRES_DB" \
  -v app_user="$DB_APP_USER" -v app_pass="$DB_APP_PASSWORD" \
  -v bi_user="$DB_BI_USER" -v bi_pass="$DB_BI_PASSWORD" \
  -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_pass';
CREATE ROLE :"bi_user" LOGIN PASSWORD :'bi_pass';

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"app_user";
GRANT CONNECT ON DATABASE :"db" TO :"bi_user";
-- O pytest-django cria o banco de teste com o usuário da aplicação.
ALTER ROLE :"app_user" CREATEDB;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA conciliacao AUTHORIZATION :"app_user";
CREATE SCHEMA conciliacao_bi AUTHORIZATION :"app_user";
GRANT USAGE ON SCHEMA conciliacao_bi TO :"bi_user";

ALTER ROLE :"app_user" SET search_path = conciliacao;
ALTER ROLE :"bi_user" SET search_path = conciliacao_bi;
ALTER ROLE :"bi_user" SET default_transaction_read_only = on;
SQL
