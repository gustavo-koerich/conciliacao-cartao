# Backup da Conciliação: mesma versão do PostgreSQL do banco (pg_dump 16) + age (criptografia) + curl (aviso no Teams).
FROM postgres:16.15-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends age curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY docker/backup/backup.sh /usr/local/bin/backup.sh
RUN chmod 755 /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["agendado"]
