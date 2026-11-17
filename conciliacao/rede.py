"""IP de origem e nome da máquina, usados no filtro de rede e na auditoria."""
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as TempoEsgotado
from functools import lru_cache

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dns-reverso")


@lru_cache(maxsize=32)
def _redes(faixas: tuple) -> tuple:
    return tuple(ipaddress.ip_network(f.strip(), strict=False) for f in faixas if f.strip())


def _ip(texto: str):
    try:
        return ipaddress.ip_address(texto.strip())
    except ValueError:
        return None


def ip_na_faixa(ip: str, faixas) -> bool:
    endereco = _ip(ip or "")
    if endereco is None:
        return False
    return any(endereco in rede for rede in _redes(tuple(faixas)))


def ip_do_cliente(request) -> str:
    """IP real do cliente.

    O X-Forwarded-For só é considerado quando a conexão vem de um proxy confiável (TRUSTED_PROXIES).
    A cadeia é lida da direita para a esquerda: o primeiro endereço que não é proxy confiável é o cliente.
    Assim um cliente não consegue forjar o próprio IP mandando o cabeçalho.
    """
    remoto = request.META.get("REMOTE_ADDR", "")
    if not ip_na_faixa(remoto, settings.TRUSTED_PROXIES):
        return remoto
    cadeia = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    for candidato in reversed(cadeia):
        if _ip(candidato) is None:
            # Cabeçalho malformado: não dá para confiar no resto da cadeia.
            return remoto
        if not ip_na_faixa(candidato, settings.TRUSTED_PROXIES):
            return candidato
    return cadeia[0] if cadeia else remoto


def _consultar_dns(ip: str) -> str:
    return socket.gethostbyaddr(ip)[0]


def nome_maquina(ip: str, tempo_limite: float = 1.0) -> str:
    """Nome da máquina por DNS reverso, quando houver. Nunca trava a requisição por mais que tempo_limite."""
    if _ip(ip or "") is None:
        return ""
    chave = f"dns-reverso:{ip}"
    guardado = cache.get(chave)
    if guardado is not None:
        return guardado
    try:
        nome = _executor.submit(_consultar_dns, ip).result(timeout=tempo_limite)
    except (OSError, TempoEsgotado):
        nome = ""
    except Exception:  # noqa: BLE001 - DNS reverso é informativo; falha não pode derrubar a ação auditada
        log.warning("falha inesperada no DNS reverso", extra={"ip": ip}, exc_info=True)
        nome = ""
    cache.set(chave, nome, 3600)
    return nome
