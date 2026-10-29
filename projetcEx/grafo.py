"""Orquestração do FinGuard em LangGraph (nível 2 do desafio, PDF p. 6 e 7).

Grafo por reclamação:
    guardrail_entrada ─ bloqueada ─► resposta_bloqueio
                      └ liberada ─► agente 1 ─► agente 2 ─► ... ─► guardrail_saida
    Em cada agente: JSON inválido volta para o próprio agente (laço de validação, até TENTATIVAS chamadas);
    sem resposta válida vai para registrar_falha; "bloqueado": true vai para resposta_bloqueio.
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
    saida: dict | None
    erro: str | None


class EstadoLote(TypedDict, total=False):
    linhas: int


def grafo_linha(plan: dict, job: proc.Job | None = None, call_model=None):
    agentes = plan["agentes"]
    nomes = {c["nome"] for c in plan["config"]["campos"]}
    ordem = [a["name"] for a in agentes]

    def guardrail_entrada(s: EstadoLinha) -> dict:
        row, inicio = s["row"], time.time()
        motivo = proc.guardrail(row["dados"][plan["csv"]["coluna_texto"]])
        if motivo:
            job.log({"id": row["id"], "agente": "guardrail_entrada", "entrada": "texto da reclamação",
                     "saida": f"bloqueada: {motivo}", "duracao_s": round(time.time() - inicio, 3),
                     "erro": None, "modelo": None})
        return {"motivo": motivo, "anteriores": {}, "estado": {}, "tentativas": 0, "trechos": None, "saida": None}

    def no_agente(i: int, agente: dict):
        def executar(s: EstadoLinha) -> dict:
            if job.cancel.is_set():
                raise proc.Cancelled
            row, tentativas = s["row"], s.get("tentativas", 0)
            job.set_etapa(f"{agente['name']} · id {row['id']}")
            trechos, rag_s = s.get("trechos"), None
            if tentativas == 0:
                inicio = time.time()
                try:
                    trechos = proc.recuperar_politica(plan, agente, row, s["estado"])
                except proc.ProcError as problema:
                    raise proc.JobAbort(str(problema)) from None
                rag_s = round(time.time() - inicio, 3)
            rastreio = {"tentativa": tentativas + 1}
            if agente["politica"] and job.politica:
                rastreio.update({"politica": job.politica["rotulo"], "rag_s": rag_s,
                                 "trechos": [{k: t[k] for k in ("secao", "origem", "score")} for t in trechos]})
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
                     "detalhes": proc.detalhes_de(s["anteriores"], nomes)})
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
    g.add_node("guardrail_entrada", guardrail_entrada)
    for i, agente in enumerate(agentes):
        g.add_node(agente["name"], no_agente(i, agente))
    g.add_node("guardrail_saida", guardrail_saida)
    g.add_node("resposta_bloqueio", resposta_bloqueio)
    g.add_node("registrar_falha", registrar_falha)
    g.add_edge(START, "guardrail_entrada")
    g.add_conditional_edges("guardrail_entrada", lambda s: "bloqueada" if s["motivo"] else "liberada",
                            {"bloqueada": "resposta_bloqueio", "liberada": ordem[0] if ordem else "guardrail_saida"})
    for i, nome in enumerate(ordem):
        g.add_conditional_edges(nome, rota_agente, {
            "ok": ordem[i + 1] if i + 1 < len(ordem) else "guardrail_saida",
            "nova_tentativa": nome, "sem_resposta_valida": "registrar_falha", "nao_e_reclamacao": "resposta_bloqueio"})
    for fim in ("guardrail_saida", "resposta_bloqueio", "registrar_falha"):
        g.add_edge(fim, END)
    return g.compile()


def _limite(plan: dict) -> dict:
    # guardrail_entrada + até TENTATIVAS chamadas por agente + o nó final, com folga.
    return {"recursion_limit": len(plan["agentes"]) * proc.TENTATIVAS + 5}


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
    config = proc.load_pipeline(Path(__file__).resolve().parent)
    print(mermaid({"agentes": [{"name": nome} for nome in config["agentes"]], "config": config}))
