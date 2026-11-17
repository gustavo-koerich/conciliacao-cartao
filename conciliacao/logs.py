"""Formatador JSON para o log estruturado (stdout e arquivo rotativo).

Regra do projeto: nome de paciente e dados do formulário nunca entram em log.
Logue ids e contagens, não o conteúdo das linhas.
"""
import json
import logging
import re
from datetime import datetime, timezone

# O link de pendências carrega um código secreto (/pendencias/<código>/): nunca vai para o log.
_CODIGO_PORTAL = re.compile(r"(/pendencias/)[^/\s\"'?<>]+")


def mascarar(texto: str) -> str:
    return _CODIGO_PORTAL.sub(r"\1***", texto)


_PADRAO = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message", "asctime"}


class FormatadorJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        linha = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "nivel": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for chave, valor in vars(record).items():
            if chave not in _PADRAO and not chave.startswith("_"):
                linha[chave] = valor
        if record.exc_info:
            linha["erro"] = self.formatException(record.exc_info)
        return mascarar(json.dumps(linha, ensure_ascii=False, default=str))
