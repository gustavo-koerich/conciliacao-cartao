"""Acesso pelo link da internet (Cloudflare Tunnel + Cloudflare Access) — mesmas regras da Central.

Só o container cloudflared alcança a porta externa (PORTA_EXTERNA, dentro da rede Docker, nunca publicada).
Todo pedido que chega por ela precisa do token assinado do Access, conferido aqui com a chave pública da
Cloudflare; sem configuração ou com token inválido, nada passa. Nessa porta o filtro de faixas de rede não se
aplica (quem vem da internet tem IP público): a segurança é o Access + o login da Central, que é avisada de que o
pedido veio de fora (sessão aberta pela internet, pessoa liberada para entrar pela internet, no máximo 8 h).
"""
import threading
import time

from django.conf import settings

_chaves = {"em": 0.0, "jwks": None}
_lock = threading.Lock()


class AcessoNegado(Exception):
    pass


def eh_portal(request) -> bool:
    """Conexão pela porta do portal de pendências (Tailscale Funnel). Mesma regra: a porta real do socket."""
    sock = request.META.get("gunicorn.socket")
    if sock is not None:
        try:
            return str(sock.getsockname()[1]) == str(settings.PORTA_PORTAL)
        except OSError:
            return False
    return str(request.META.get("SERVER_PORT", "")) == str(settings.PORTA_PORTAL)   # servidor de testes


def eh_externo(request) -> bool:
    """Pela porta em que a conexão chegou (socket do gunicorn), nunca pelo cabeçalho Host."""
    sock = request.META.get("gunicorn.socket")
    if sock is not None:
        try:
            return str(sock.getsockname()[1]) == str(settings.PORTA_EXTERNA)
        except OSError:
            return True   # na dúvida, trata como externo (exige o token do Access)
    return str(request.META.get("SERVER_PORT", "")) == str(settings.PORTA_EXTERNA)   # servidor de testes


def _emissor():
    return f"https://{settings.CF_ACCESS_TIME}.cloudflareaccess.com"


def _cliente_jwks():
    import jwt
    with _lock:
        if _chaves["jwks"] is None or time.time() - _chaves["em"] > 3600:
            _chaves["jwks"] = jwt.PyJWKClient(_emissor() + "/cdn-cgi/access/certs", cache_keys=True)
            _chaves["em"] = time.time()
        return _chaves["jwks"]


def validar_token(token: str) -> str:
    """Confere assinatura, emissor, público e validade do token do Access. Devolve o e-mail de quem passou."""
    import jwt
    if not (settings.CF_ACCESS_TIME and settings.CF_ACCESS_AUD):
        raise AcessoNegado("acesso externo não configurado")
    if not token:
        raise AcessoNegado("sem token do Cloudflare Access")
    try:
        chave = _cliente_jwks().get_signing_key_from_jwt(token).key
        dados = jwt.decode(token, chave, algorithms=["RS256"], audience=settings.CF_ACCESS_AUD, issuer=_emissor(),
                           options={"require": ["exp", "iat", "aud", "iss"]})
    except Exception as exc:  # noqa: BLE001 - qualquer falha = não passa
        raise AcessoNegado(f"token do Access inválido ({exc.__class__.__name__})") from exc
    return str(dados.get("email") or "")


def ultimo_encaminhado(request) -> str:
    """IP real de quem chega pela Tailscale (Funnel): ela acrescenta o IP no fim do X-Forwarded-For; o que vem antes
    o próprio visitante pode ter escrito."""
    return (request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            or request.META.get("REMOTE_ADDR", ""))


def conferir(request) -> str:
    """Portão da porta externa. "cloudflare": token do Access em todo pedido. "funnel": sem portão (a segurança é o
    login da Central com senha + autenticador e a liberação "externo" da pessoa). Outro valor: recusa tudo."""
    if settings.ACESSO_EXTERNO == "funnel":
        return ""
    if settings.ACESSO_EXTERNO == "cloudflare":
        return validar_token(request.headers.get("Cf-Access-Jwt-Assertion", ""))
    raise AcessoNegado("acesso externo desligado")


def ip_externo(request) -> str:
    if settings.ACESSO_EXTERNO == "funnel":
        return ultimo_encaminhado(request)
    return (request.headers.get("Cf-Connecting-IP") or request.META.get("REMOTE_ADDR", "")).strip()
