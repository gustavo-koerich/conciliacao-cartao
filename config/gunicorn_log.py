"""Log de acesso do gunicorn sem o código secreto do link de pendências (/pendencias/<código>/ vira /pendencias/***/).
Usado em docker-compose.yml: --logger-class config.gunicorn_log.LogSemCodigo"""
from gunicorn.glogging import Logger

from conciliacao.logs import mascarar


class LogSemCodigo(Logger):
    def atoms(self, resp, req, environ, request_time):
        atomos = super().atoms(resp, req, environ, request_time)
        for chave in ("r", "U", "f"):           # linha do pedido, caminho, Referer
            if isinstance(atomos.get(chave), str):
                atomos[chave] = mascarar(atomos[chave])
        return atomos
