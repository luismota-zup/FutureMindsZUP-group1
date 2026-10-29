"""Orquestração do FinGuard em LangGraph (nível 2 do desafio, PDF p. 6 e 7).

Grafo por reclamação:
    guardrail_entrada ─ bloqueada ─► resposta_bloqueio
                      └ liberada ─► [classificador_entrada] ─► [politica_<agente>] ─► agente 1 ─► ... ─► [verificar_grounding] ─► guardrail_saida
    classificador_entrada: modelo leve, opcional em pipeline.json. Manda injeção, ameaça e conteúdo fora do escopo
    para resposta_bloqueio e, sem resposta válida do modelo, para registrar_falha.
    politica_<agente>: nó de RAG (Knowledge Source) antes de cada agente que declara `politica:` no cabeçalho.
    Em cada agente: JSON inválido volta para o próprio agente (laço de validação, até TENTATIVAS chamadas) e
    reaproveita os trechos; sem resposta válida vai para registrar_falha; "bloqueado": true vai para resposta_bloqueio.
    verificar_grounding: confere se as seções citadas estavam entre as recuperadas (só marca, não chama o modelo).
Grafo do lote: reclamacoes (o grafo acima, linha a linha) ─► relatorio.

Os agentes são os de regras/pipeline.json, na ordem. As regras (máscara, RAG, contrato, tom, log) ficam em
processador.py; aqui ficam só os nós e as arestas. `python grafo.py` imprime o diagrama Mermaid do pipeline atual.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import processador as proc


class EstadoLinha(TypedDict, total=False):
    row: dict
    motivo: str | None     # por que a linha foi bloqueada (guardrail ou agente)
    anteriores: dict       # saída completa e sanitizada de cada agente, repassada ao próximo
    estado: dict           # campos do contrato (pipeline.json) acumulados
    agente: str            # agente que respondeu por último
    tentativas: int        # chamadas já feitas pelo agente atual sem JSON válido
    trechos: list | None   # política recuperada para o agente atual (a nova tentativa reaproveita)
    fontes: dict           # seções recuperadas por agente, para a verificação de grounding
    grounding: list        # citações sem respaldo nos trechos (proc.verificar_grounding)
    saida: dict | None
    erro: str | None


class EstadoLote(TypedDict, total=False):
    linhas: int


def grafo_linha(plan: dict, job: proc.Job | None = None, call_model=None):
    agentes = plan["agentes"]
    nomes = {c["nome"] for c in plan["config"]["campos"]}
    ordem = [a["name"] for a in agentes]
    com_rag = {a["name"] for a in agentes if a.get("politica")}

    def entrada(nome: str) -> str:
        return f"politica_{nome}" if nome in com_rag else nome

    def guardrail_entrada(s: EstadoLinha) -> dict:
        row, inicio = s["row"], time.time()
        motivo = proc.guardrail(row["dados"][plan["csv"]["coluna_texto"]])
        if motivo:
            job.log({"id": row["id"], "agente": "guardrail_entrada", "entrada": "texto da reclamação",
                     "saida": f"bloqueada: {motivo}", "duracao_s": round(time.time() - inicio, 3),
                     "erro": None, "modelo": None})
        return {"motivo": motivo, "anteriores": {}, "estado": {}, "tentativas": 0, "trechos": None, "saida": None,
                "fontes": {}, "grounding": []}

    def no_politica(agente: dict):
        """Knowledge Source: recupera os trechos da política para o agente seguinte (consulta já mascarada)."""
        def executar(s: EstadoLinha) -> dict:
            if job.cancel.is_set():
                raise proc.Cancelled
            row, inicio = s["row"], time.time()
            job.set_etapa(f"política para {agente['name']} · id {row['id']}")
            try:
                trechos = proc.recuperar_politica(plan, agente, row, s["estado"])
            except proc.ProcError as problema:
                raise proc.JobAbort(str(problema)) from None
            if not job.politica:
                return {"trechos": trechos}
            secoes = [t["secao"] for t in trechos]
            job.log({"id": row["id"], "agente": f"politica_{agente['name']}",
                     "entrada": "consulta: texto mascarado, canal, produto e urgência",
                     "saida": "seções: " + (", ".join(secoes) or "nenhuma"),
                     "duracao_s": round(time.time() - inicio, 3), "erro": None, "modelo": None,
                     "politica": job.politica["rotulo"]})
            return {"trechos": trechos, "fontes": {**s.get("fontes", {}), agente["name"]: secoes}}
        return executar

    def classificador_entrada(s: EstadoLinha) -> dict:
        row = s["row"]
        job.set_etapa(f"classificador_entrada · id {row['id']}")
        motivo, erro = proc.classificar_entrada(job, call_model, row, plan)
        return {"motivo": motivo, "erro": erro, "agente": "classificador_entrada"}

    def no_agente(i: int, agente: dict):
        def executar(s: EstadoLinha) -> dict:
            if job.cancel.is_set():
                raise proc.Cancelled
            row, tentativas, trechos = s["row"], s.get("tentativas", 0), s.get("trechos")
            job.set_etapa(f"{agente['name']} · id {row['id']}")
            rastreio = {"tentativa": tentativas + 1}
            if agente["politica"] and job.politica:
                rastreio.update({"politica": job.politica["rotulo"],
                                 "trechos": [{k: t[k] for k in ("secao", "origem", "score")} for t in trechos or []]})
            system = proc.system_for(plan, i, trechos)
            user = proc.build_user(row, plan["csv"]["coluna_texto"], i + 1, len(agentes), s["anteriores"])
            saida, erro = proc.tentar_agente(job, call_model, agente, system, user, row["id"], rastreio)
            if saida is None:
                return {"agente": agente["name"], "saida": None, "erro": erro,
                        "tentativas": tentativas + 1, "trechos": trechos}
            seguinte = {"agente": agente["name"], "saida": saida, "erro": None, "tentativas": 0, "trechos": None}
            if saida.get("bloqueado") is True:  # o agente concluiu que não é uma reclamação (PDF, p. 8)
                return {**seguinte, "motivo": proc.MOTIVO_AGENTE}
            if saida.get("ignorado") is True:  # agente seguinte a um bloqueio: nada a acrescentar
                return seguinte
            anteriores, estado = proc.acumular(agente["name"], saida, s["anteriores"], s["estado"],
                                               nomes, job.improprios)
            return {**seguinte, "anteriores": anteriores, "estado": estado}
        return executar

    def rota_agente(s: EstadoLinha) -> str:
        if s["saida"] is None:
            return "nova_tentativa" if s["tentativas"] < proc.TENTATIVAS else "sem_resposta_valida"
        return "nao_e_reclamacao" if s["saida"].get("bloqueado") is True else "ok"

    def verificar_grounding(s: EstadoLinha) -> dict:
        row, inicio = s["row"], time.time()
        problemas = proc.verificar_grounding(s["anteriores"], s.get("fontes", {}))
        if problemas:
            job.log({"id": row["id"], "agente": "verificar_grounding", "entrada": "citações da política nas saídas",
                     "saida": " | ".join(proc.descrever_grounding(p) for p in problemas)[:1000],
                     "duracao_s": round(time.time() - inicio, 3), "erro": None, "modelo": None})
        return {"grounding": problemas}

    def guardrail_saida(s: EstadoLinha) -> dict:
        row, tom, inicio = s["row"], [], time.time()
        valores, erros = proc.normalize(s["estado"], plan["config"]["campos"], job.improprios, tom)
        if tom:
            job.log({"id": row["id"], "agente": "guardrail_saida", "entrada": "tom da resposta",
                     "saida": "ajustado: " + ", ".join(sorted(set(tom))),
                     "duracao_s": round(time.time() - inicio, 3), "erro": None, "modelo": None})
        origem = job.origem(row["dados"])
        if erros:
            job.add({"id": row["id"], "status": "falha", "campos": None, "origem": origem, "motivo": "; ".join(erros)})
        else:
            job.add({"id": row["id"], "status": "ok", "campos": valores, "origem": origem, "motivo": None,
                     "detalhes": proc.detalhes_de(s["anteriores"], nomes), "grounding": s.get("grounding") or []})
        return {}

    def resposta_bloqueio(s: EstadoLinha) -> dict:
        row, motivo = s["row"], s["motivo"]
        job.add({"id": row["id"], "status": "bloqueada", "campos": None, "origem": job.origem(row["dados"]),
                 "motivo": motivo, "resposta": proc.resposta_bloqueio(motivo)})
        return {}

    def registrar_falha(s: EstadoLinha) -> dict:
        row = s["row"]
        job.add({"id": row["id"], "status": "falha", "campos": None, "origem": job.origem(row["dados"]),
                 "motivo": f"agente {s['agente']}: {s['erro']}"})
        return {}

    g = StateGraph(EstadoLinha)
    usa_classificador = plan["config"]["classificador_entrada"]["ativo"]
    g.add_node("guardrail_entrada", guardrail_entrada)
    if usa_classificador:
        g.add_node("classificador_entrada", classificador_entrada)
    for i, agente in enumerate(agentes):
        if agente["name"] in com_rag:
            g.add_node(entrada(agente["name"]), no_politica(agente))
            g.add_edge(entrada(agente["name"]), agente["name"])
        g.add_node(agente["name"], no_agente(i, agente))
    final = "verificar_grounding" if com_rag else "guardrail_saida"
    if com_rag:
        g.add_node("verificar_grounding", verificar_grounding)
        g.add_edge("verificar_grounding", "guardrail_saida")
    g.add_node("guardrail_saida", guardrail_saida)
    g.add_node("resposta_bloqueio", resposta_bloqueio)
    g.add_node("registrar_falha", registrar_falha)
    primeiro = entrada(ordem[0]) if ordem else final
    g.add_edge(START, "guardrail_entrada")
    g.add_conditional_edges("guardrail_entrada", lambda s: "bloqueada" if s["motivo"] else "liberada",
                            {"bloqueada": "resposta_bloqueio",
                             "liberada": "classificador_entrada" if usa_classificador else primeiro})
    if usa_classificador:
        g.add_conditional_edges(
            "classificador_entrada", lambda s: "falha" if s["erro"] else ("bloqueada" if s["motivo"] else "liberada"),
            {"falha": "registrar_falha", "bloqueada": "resposta_bloqueio", "liberada": primeiro})
    for i, nome in enumerate(ordem):
        g.add_conditional_edges(nome, rota_agente, {
            "ok": entrada(ordem[i + 1]) if i + 1 < len(ordem) else final,
            "nova_tentativa": nome, "sem_resposta_valida": "registrar_falha", "nao_e_reclamacao": "resposta_bloqueio"})
    for fim in ("guardrail_saida", "resposta_bloqueio", "registrar_falha"):
        g.add_edge(fim, END)
    return g.compile()


def _limite(plan: dict) -> dict:
    # guardrail_entrada, classificador, nó de RAG e até TENTATIVAS chamadas por agente, grounding e o nó final, com folga.
    return {"recursion_limit": len(plan["agentes"]) * (proc.TENTATIVAS + 1) + 7}


def grafo_lote(plan: dict, job: proc.Job | None = None, call_model=None):
    linha = grafo_linha(plan, job, call_model)

    def reclamacoes(s: EstadoLote) -> dict:
        linhas = (*plan["bloqueadas"], *plan["executaveis"])
        for row in linhas:
            if job.cancel.is_set():
                raise proc.Cancelled
            linha.invoke({"row": row}, _limite(plan))
        return {"linhas": len(linhas)}

    def relatorio(s: EstadoLote) -> dict:
        proc.executar_relatorio(job, plan, call_model)
        return {}

    g = StateGraph(EstadoLote)
    g.add_node("reclamacoes", reclamacoes)
    g.add_node("relatorio", relatorio)
    g.add_edge(START, "reclamacoes")
    g.add_edge("reclamacoes", "relatorio")
    g.add_edge("relatorio", END)
    return g.compile()


def executar_lote(job: proc.Job, plan: dict, call_model) -> None:
    grafo_lote(plan, job, call_model).invoke({})


def mermaid(plan: dict) -> str:
    """Diagramas dos dois grafos, para o ADR e o README. Não executa nada."""
    return ("%% Grafo por reclamação\n" + grafo_linha(plan).get_graph().draw_mermaid()
            + "\n%% Grafo do lote\n" + grafo_lote(plan).get_graph().draw_mermaid())


if __name__ == "__main__":
    raiz = Path(__file__).resolve().parent
    config = proc.load_pipeline(raiz)
    agentes = [proc._carregar_agente(raiz, nome, "agente") for nome in config["agentes"]]
    print(mermaid({"agentes": agentes, "config": config}))
