#!/bin/bash
# Backup diário da Conciliação (decisão de 22/10/2026: SharePoint/OneDrive da empresa, pasta
# "1 - FINANCEIRO/1 - CONCILIAÇÃO - NOTAS/Backup Conciliação", sincronizada no servidor pela conta de serviço).
#
#   backup.sh agendado    roda todo dia em BACKUP_HORARIO (padrão 02:00); aos domingos também testa a restauração
#   backup.sh agora       um backup já
#   backup.sh testar      teste de restauração: abre o último backup do banco num banco temporário e confere
#
# O que vai para /destino (criptografado com age; só abre com a chave privada, que fica no cofre):
#   banco/conciliacao-AAAAMMDD-HHMM.dump.age   cópia do banco inteiro (pg_dump), guarda as 30 mais recentes
#   arquivos/<pasta>/<arquivo>.age             faturas, planilhas, notas e comprovantes; os nomes são o hash do conteúdo
#                                              e nunca mudam, então só os novos são enviados (espelho)
# Cópia sem criptografia só no disco do servidor (/backup-local), guardando 14.
# Cada execução grava uma linha em conc_backup (aparece no Painel) e avisa no Teams quando falha.
set -uo pipefail

DESTINO=/destino
LOCAL=/backup-local
PUBLICA=/run/segredos/backup-publica.txt
PRIVADA=/run/segredos/backup-privada.txt
export PGHOST="${PGHOST:-db}" PGUSER="${PGUSER:-postgres}" PGDATABASE="${PGDATABASE:-conciliacao}"
export PGPASSWORD="${POSTGRES_SUPERUSER_PASSWORD:-}"

log() { echo "[backup] $(date '+%d/%m/%Y %H:%M:%S') $*"; }

registrar() {   # tipo status arquivo tamanho hash duracao detalhe_json
  psql -q -v ON_ERROR_STOP=1 -c "INSERT INTO conciliacao.conc_backup
      (momento, tipo, status, arquivo, tamanho, hash_sha256, destino, duracao_seg, detalhe)
      VALUES (now(), '$1', '$2', '$3', ${4:-NULL}, '$5', 'OneDrive/SharePoint (Backup Conciliação)', ${6:-0}, '$7'::jsonb)" \
    || log "não consegui registrar no banco"
}

avisar() {
  [ -n "${TEAMS_WEBHOOK:-}" ] || return 0
  curl -s -m 20 -H "Content-Type: application/json" -d "{\"text\": \"Backup da Conciliação: $1\"}" "$TEAMS_WEBHOOK" >/dev/null || true
}

falhar() {
  log "ERRO: $1"
  registrar BACKUP ERRO "" NULL "" "$(( $(date +%s) - INICIO ))" "{\"erro\": \"$1\"}"
  avisar "FALHOU - $1"
  return 1
}

fazer_backup() {
  INICIO=$(date +%s)
  local dia; dia=$(date +%Y%m%d-%H%M)
  [ -s "$PUBLICA" ] || { falhar "chave pública não encontrada"; return 1; }
  [ -d "$DESTINO" ] && [ -w "$DESTINO" ] || { falhar "pasta do OneDrive inacessível"; return 1; }
  mkdir -p "$DESTINO/banco" "$DESTINO/arquivos" "$LOCAL"

  local dump="$LOCAL/conciliacao-$dia.dump"
  pg_dump -Fc -f "$dump" || { falhar "pg_dump falhou"; return 1; }
  local tamanho hash; tamanho=$(stat -c %s "$dump"); hash=$(sha256sum "$dump" | cut -d' ' -f1)
  local cifrado="$DESTINO/banco/conciliacao-$dia.dump.age"
  age -R "$PUBLICA" -o "$cifrado.tmp" "$dump" && mv "$cifrado.tmp" "$cifrado" || { falhar "criptografia do banco falhou"; return 1; }

  # arquivos: espelho criptografado, só o que ainda não foi
  local novos=0 total=0
  while IFS= read -r -d '' f; do
    total=$((total + 1))
    local rel="${f#/arquivos/}" alvo="$DESTINO/arquivos/${f#/arquivos/}.age"
    [ -f "$alvo" ] && continue
    mkdir -p "$(dirname "$alvo")"
    if age -R "$PUBLICA" -o "$alvo.tmp" "$f" && mv "$alvo.tmp" "$alvo"; then novos=$((novos + 1)); else
      falhar "criptografia do arquivo $rel falhou"; return 1; fi
  done < <(find /arquivos -type f ! -path "*/exportacoes/*" -print0)

  # retenção
  ls -1t "$LOCAL"/conciliacao-*.dump 2>/dev/null | tail -n +15 | xargs -r rm -f
  ls -1t "$DESTINO"/banco/conciliacao-*.dump.age 2>/dev/null | tail -n +31 | xargs -r rm -f

  local duracao=$(( $(date +%s) - INICIO ))
  registrar BACKUP OK "banco/conciliacao-$dia.dump.age" "$tamanho" "$hash" "$duracao" \
    "{\"arquivos_novos\": $novos, \"arquivos_total\": $total}"
  log "ok: banco $(( tamanho / 1024 )) KB, arquivos novos $novos de $total, ${duracao}s"
}

testar_restauracao() {
  INICIO=$(date +%s)
  local ultimo; ultimo=$(ls -1t "$DESTINO"/banco/conciliacao-*.dump.age 2>/dev/null | head -1)
  [ -n "$ultimo" ] || { log "nenhum backup para testar"; return 1; }
  [ -s "$PRIVADA" ] || { log "chave privada não está no servidor: teste só com a chave do cofre"; return 1; }
  local tmp=/tmp/teste-restauracao.dump
  age -d -i "$PRIVADA" -o "$tmp" "$ultimo" || { log "não consegui abrir $ultimo"; return 1; }
  psql -q -d postgres -c "DROP DATABASE IF EXISTS teste_restauracao" -c "CREATE DATABASE teste_restauracao"
  pg_restore -d teste_restauracao --no-owner --no-privileges "$tmp" 2>/tmp/restauracao.log
  local orig rest
  orig=$(psql -tA -c "SELECT count(*) FROM conciliacao.conc_fatura_lanc")
  rest=$(psql -tA -d teste_restauracao -c "SELECT count(*) FROM conciliacao.conc_fatura_lanc")
  psql -q -d postgres -c "DROP DATABASE teste_restauracao"
  rm -f "$tmp"
  local status=OK; [ -n "$rest" ] && [ "$rest" -gt 0 ] || status=ERRO
  registrar TESTE_RESTAURACAO "$status" "${ultimo#$DESTINO/}" NULL "" "$(( $(date +%s) - INICIO ))" \
    "{\"lancamentos_no_backup\": ${rest:-0}, \"lancamentos_hoje\": ${orig:-0}}"
  log "teste de restauração $status: $rest lançamentos no backup (hoje: $orig)"
  [ "$status" = OK ]
}

case "${1:-agendado}" in
  agora) fazer_backup ;;
  testar) testar_restauracao ;;
  agendado)
    HORARIO="${BACKUP_HORARIO:-02:00}"
    log "agendado todo dia às $HORARIO"
    while true; do
      agora=$(date +%s); alvo=$(date -d "today $HORARIO" +%s)
      [ "$alvo" -le "$agora" ] && alvo=$(date -d "tomorrow $HORARIO" +%s)
      # Espera em trechos de até 10 min conferindo o relógio: um "sleep" de 24 h no WSL acordava até 1 h antes
      # (o relógio da máquina virtual se ajusta enquanto dorme) e o backup saía duas vezes por dia.
      while [ "$(date +%s)" -lt "$alvo" ]; do
        resta=$(( alvo - $(date +%s) )); sleep $(( resta < 600 ? resta : 600 ))
      done
      fazer_backup || true
      # Teste de restauração semanal (domingo): avisa no Teams se falhar
      if [ "$(date +%u)" = 7 ]; then
        testar_restauracao || avisar "TESTE DE RESTAURAÇÃO FALHOU - veja o log do container de backup"
      fi
    done ;;
  *) echo "uso: backup.sh agendado|agora|testar"; exit 2 ;;
esac
