import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
_django = get_wsgi_application()

# Pela Tailscale o prefixo /conciliacao chega já retirado; pelo túnel da Cloudflare ele chega no caminho.
# Retira aqui para os dois caminhos ficarem iguais (o Django recoloca o prefixo nos links via PREFIXO_URL).
_PREFIXO = os.environ.get("PREFIXO_URL", "").rstrip("/")


def application(environ, start_response):
    caminho = environ.get("PATH_INFO", "")
    if _PREFIXO and (caminho == _PREFIXO or caminho.startswith(_PREFIXO + "/")):
        environ["PATH_INFO"] = caminho[len(_PREFIXO):] or "/"
    return _django(environ, start_response)
