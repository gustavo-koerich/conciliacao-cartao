import pytest


@pytest.fixture(autouse=True)
def config_teste(settings, tmp_path):
    """Cache em memória (testes não dependem do Redis) e redes previsíveis."""
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.ALLOWED_NETWORKS = ["127.0.0.1/32", "10.0.0.0/8", "100.64.0.0/10"]
    settings.TRUSTED_PROXIES = ["127.0.0.1/32"]
    settings.ACESSO_MODO = "rede"
    settings.ARQUIVOS_DIR = tmp_path / "arquivos"
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
