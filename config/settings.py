"""Configuração do Django. Tudo que muda entre ambientes vem do .env (django-environ)."""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
APP_VERSAO = env("APP_VERSAO", default="dev")  # commit do git, gravado na imagem pelo publicar.ps1
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
# Link da internet (Cloudflare Tunnel + Access): endereço público, porta só do túnel e dados do Access.
HOST_EXTERNO = env("HOST_EXTERNO", default="").strip()
if HOST_EXTERNO:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, HOST_EXTERNO]
    CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, f"https://{HOST_EXTERNO}"]
PORTA_EXTERNA = env("PORTA_EXTERNA", default="8001")
# como a internet chega na porta externa: "cloudflare" (token do Access) ou "funnel" (Tailscale Funnel; ver externo.py)
ACESSO_EXTERNO = env("ACESSO_EXTERNO", default="cloudflare").strip().lower()
CF_ACCESS_TIME = env("CF_ACCESS_TIME", default="").strip()
CF_ACCESS_AUD = env("CF_ACCESS_AUD", default="").strip()
# Portal de pendências dos representantes: porta própria (publicada só para o Tailscale Funnel), que responde
# apenas a /pendencias/; PORTAL_URL_BASE é o endereço público usado nos links (ex.: https://…ts.net:8443).
PORTA_PORTAL = env("PORTA_PORTAL", default="8002")
PORTAL_URL_BASE = env("PORTAL_URL_BASE", default="").strip().rstrip("/")
if PORTAL_URL_BASE:
    CSRF_TRUSTED_ORIGINS = [*CSRF_TRUSTED_ORIGINS, PORTAL_URL_BASE]

# Publicação sob https://<central>/conciliacao: o Tailscale Serve remove o prefixo antes de repassar.
# PREFIXO_URL (e não SCRIPT_NAME): o gunicorn também lê SCRIPT_NAME e recusaria os pedidos, que chegam sem o
# prefixo (a Tailscale tira o /conciliacao antes de repassar). O Django só o recoloca nos links.
FORCE_SCRIPT_NAME = env("PREFIXO_URL", default="") or None
_PREFIXO = FORCE_SCRIPT_NAME or ""

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "django_rq",
    "conciliacao",
]

# ---------- Acesso ----------
ACESSO_MODO = env("ACESSO_MODO", default="central")
if ACESSO_MODO not in {"central", "rede", "django"}:
    raise ValueError(f"ACESSO_MODO inválido: {ACESSO_MODO!r} (use central, rede ou django)")
ALLOWED_NETWORKS = env.list("ALLOWED_NETWORKS", default=["127.0.0.1/32"])
TRUSTED_PROXIES = env.list("TRUSTED_PROXIES", default=["127.0.0.1/32"])
CENTRAL_VERIFY_URL = env("CENTRAL_VERIFY_URL", default="http://central:8080/api/auth/verificar")
CENTRAL_LOGIN_URL = env("CENTRAL_LOGIN_URL", default="/login")
CENTRAL_COOKIE_NAME = env("CENTRAL_COOKIE_NAME", default="central_sessao")
CENTRAL_APP_ID = env("CENTRAL_APP_ID", default="conciliacao")
CENTRAL_CACHE_SEGUNDOS = env.int("CENTRAL_CACHE_SEGUNDOS", default=30)
# Rotas que não passam pelo controle de acesso (healthcheck do Docker).
ROTAS_LIVRES = ("/healthz",)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "conciliacao.middleware.RedeMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "conciliacao.middleware.AcessoMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
LOGIN_URL = "admin:login"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "conciliacao.context_processors.acesso",
            ],
        },
    },
]

# ---------- Banco ----------
# O schema "conciliacao" vem primeiro; "public" só é usado no banco de teste criado pelo pytest.
DATABASES = {"default": env.db("DATABASE_URL", default="postgres://conc_app:conc_app@db:5432/conciliacao")}
DATABASES["default"]["OPTIONS"] = {"options": "-c search_path=conciliacao,public"}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = 60
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------- Fila e cache ----------
REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
RQ_QUEUES = {"default": {"URL": REDIS_URL, "DEFAULT_TIMEOUT": 3600}}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL.rsplit("/", 1)[0] + "/1",
        "KEY_PREFIX": "conc",
    }
}

# ---------- Sessão e segurança ----------
# Cookie próprio com nome distinto, para não colidir com o cookie da Central no mesmo domínio.
SESSION_COOKIE_NAME = "conc_sessao"
SESSION_COOKIE_PATH = _PREFIXO or "/"
CSRF_COOKIE_NAME = "conc_csrf"
CSRF_COOKIE_PATH = _PREFIXO or "/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# ---------- Idioma ----------
LANGUAGE_CODE = "pt-br"
TIME_ZONE = env("TZ", default="America/Sao_Paulo")
USE_I18N = True
USE_TZ = True

# ---------- Arquivos ----------
STATIC_URL = f"{_PREFIXO}/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# Faturas, planilhas e recibos: fora do diretório estático, nome = hash SHA-256.
ARQUIVOS_DIR = Path(env("ARQUIVOS_DIR", default=str(BASE_DIR / "dados" / "arquivos")))
UPLOAD_MAX_MB = env.int("UPLOAD_MAX_MB", default=25)
DATA_UPLOAD_MAX_MEMORY_SIZE = UPLOAD_MAX_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
# Notas fiscais em lote: uma pasta de notas (XML + PDF) passa fácil de 100 arquivos (padrão do Django).
DATA_UPLOAD_MAX_NUMBER_FILES = env.int("NOTAS_MAX_ARQUIVOS", default=2000)

# ---------- Integrações ----------
GOOGLE_SERVICE_ACCOUNT_FILE = env("GOOGLE_SERVICE_ACCOUNT_FILE", default="")
LEITURA_IA_RESERVA = env.bool("LEITURA_IA_RESERVA", default=False)
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", default="claude-sonnet-5-5")
EXPORT_BI_SHAREPOINT = env.bool("EXPORT_BI_SHAREPOINT", default=False)
EXPORT_BI_INTERVALO_MIN = env.int("EXPORT_BI_INTERVALO_MIN", default=60)

# ---------- Logging estruturado (JSON em stdout + arquivo rotativo) ----------
LOG_DIR = Path(env("LOG_DIR", default=str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "conciliacao.logs.FormatadorJson"}},
    "handlers": {
        "stdout": {"class": "logging.StreamHandler", "formatter": "json"},
        "arquivo": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "conciliacao.log"),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 10,
            "encoding": "utf-8",
            "formatter": "json",
        },
    },
    "root": {"handlers": ["stdout", "arquivo"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "rq.worker": {"level": "INFO"},
    },
}
