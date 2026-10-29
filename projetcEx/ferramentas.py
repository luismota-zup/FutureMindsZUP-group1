"""Ferramentas que um agente pode chamar durante a análise (tool use).

O agente declara no cabeçalho do `.md`:  ferramentas: consultar_politica, ler_secao_politica
O modelo decide quando chamar; o laço fica em processador.rodar_agente e conta cada rodada no teto de custo.

Regras desta camada:
- só leitura, sem efeito colateral e sem rede: consultam a política já indexada (politica_rag.Politica);
- toda entrada vinda do modelo é tratada como não confiável (tamanho, tipo e formato validados);
- a consulta é mascarada antes de chegar ao RAG e o resultado é sanitizado antes de voltar ao modelo.
"""
from __future__ import annotations

import re
from typing import Callable

MAX_CONSULTA = 300
MAX_RESULTADO = 3000
MAX_TRECHOS = 4
SECAO_ID = re.compile(r"\d{1,2}(?:\.\d{1,2}){0,2}")


class FerramentaError(Exception):
    """Pedido inválido do modelo. A mensagem volta ao modelo como erro da ferramenta (is_error)."""


def _consultar_politica(politica, entrada: dict, mascarar: Callable[[str], str]) -> str:
    consulta = entrada.get("consulta")
    if not isinstance(consulta, str) or not consulta.strip():
        raise FerramentaError("Informe `consulta` como texto não vazio.")
    trechos = politica.recuperar(("busca",), mascarar(consulta)[:MAX_CONSULTA], k=MAX_TRECHOS)
    if not trechos:
        return "Nenhum trecho da política cobre essa consulta."
    return "\n\n".join(f"[{t['secao']}] {t['titulo']}\n{t['texto']}" for t in trechos[:MAX_TRECHOS])


def _ler_secao(politica, entrada: dict, mascarar: Callable[[str], str]) -> str:
    secao = entrada.get("secao")
    if not isinstance(secao, str) or not SECAO_ID.fullmatch(secao.strip()):
        raise FerramentaError("Informe `secao` no formato numérico da política, por exemplo 2.4.")
    for s in politica.secoes:
        if s.id == secao.strip():
            return f"[{s.id}] {s.titulo}\n{s.texto}"
    raise FerramentaError(f"A seção {secao.strip()} não existe nesta política.")


# nome -> (função, definição enviada ao modelo no formato da API Messages da Anthropic)
FERRAMENTAS: dict[str, tuple[Callable, dict]] = {
    "consultar_politica": (_consultar_politica, {
        "name": "consultar_politica",
        "description": ("Busca na política interna os trechos mais próximos de um assunto. Use quando o caso "
                        "tiver um detalhe que os trechos já enviados não cobrem. Devolve até 4 seções numeradas."),
        "input_schema": {"type": "object", "properties": {
            "consulta": {"type": "string", "description": "Assunto a procurar, sem dados pessoais."}},
            "required": ["consulta"]}}),
    "ler_secao_politica": (_ler_secao, {
        "name": "ler_secao_politica",
        "description": "Lê uma seção da política interna pelo número, por exemplo 2.4, para conferir o texto exato.",
        "input_schema": {"type": "object", "properties": {
            "secao": {"type": "string", "description": "Número da seção, como 2.4."}},
            "required": ["secao"]}}),
}


def definicoes(nomes: tuple[str, ...]) -> list[dict]:
    return [FERRAMENTAS[n][1] for n in nomes]


def executar(nome: str, entrada: object, politica, mascarar: Callable[[str], str],
             permitidas: tuple[str, ...]) -> tuple[str, bool]:
    """Roda uma ferramenta pedida pelo modelo. Devolve (texto, é_erro); nunca levanta."""
    if nome not in permitidas or nome not in FERRAMENTAS:
        return "Ferramenta não disponível para este agente.", True
    if not isinstance(entrada, dict):
        return "Argumentos inválidos.", True
    if politica is None:
        return "A política interna não está carregada.", True
    try:
        return FERRAMENTAS[nome][0](politica, entrada, mascarar)[:MAX_RESULTADO], False
    except FerramentaError as problema:
        return str(problema), True
    except Exception:  # noqa: BLE001 - falha do RAG não pode derrubar o lote nem vazar detalhe interno
        return "Falha ao consultar a política.", True
