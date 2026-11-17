"""Registro de auditoria: quem, de onde, o quê e com qual resultado."""
import logging

from .models import Auditoria
from .rede import nome_maquina

log = logging.getLogger(__name__)


def registrar(request, acao, resultado, *, competencia=None, representante=None, cartao=None,
              importacao=None, detalhe=None, usuario=None, ip=None):
    """Grava uma linha em conc_auditoria.

    Em requisições web, IP e usuário vêm do request. Em tarefas de fundo (RQ) ou do serviço de backup,
    passe request=None e informe usuario/ip explicitamente.
    O detalhe nunca deve conter dados do formulário (paciente, descrição): só ids, contagens e totais.
    """
    if request is not None:
        ip = ip or getattr(request, "ip_cliente", None) or request.META.get("REMOTE_ADDR")
        acesso = getattr(request, "usuario_acesso", None)
        usuario = usuario or (acesso or {}).get("usuario", "")
    registro = Auditoria.objects.create(
        acao=acao,
        resultado=resultado,
        competencia=competencia,
        representante=representante,
        cartao=cartao,
        importacao=importacao,
        usuario=usuario or "",
        ip=ip or None,
        maquina=nome_maquina(ip) if ip else "",
        detalhe=detalhe or {},
    )
    log.info("auditoria", extra={"acao": acao, "resultado": resultado, "auditoria_id": registro.pk,
                                 "usuario": registro.usuario, "ip": registro.ip})
    return registro
