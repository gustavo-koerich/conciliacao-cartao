FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract com português para o OCR da fatura em imagem; tini para encerrar processos corretamente.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por tini \
 && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégio; o UID fixo facilita as permissões dos volumes no servidor.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /dados/arquivos /dados/logs /app/staticfiles \
 && chown -R app:app /dados /app/staticfiles \
 && DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

USER app
# Versão (commit do git) mostrada no rodapé e no relatório para o suporte.
ARG APP_VERSAO=dev
ENV APP_VERSAO=${APP_VERSAO}
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--access-logfile", "-"]
