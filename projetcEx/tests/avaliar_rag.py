"""Mede a recuperação da política: a seção esperada aparece entre as k mais similares?

Uso (no venv):  python tests\\avaliar_rag.py [--politica arquivo] [--k 4] [--embeddings local|bedrock]
Sai com código 1 se a taxa de acerto ficar abaixo de --minimo.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAIZ = HERE.parent
sys.path.insert(0, str(RAIZ))

import politica_rag as rag  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--politica", type=Path, default=RAIZ / "Docs - Challenge" / "KS_POLITICA_INTERNA (4).pdf")
    parser.add_argument("--casos", type=Path, default=HERE / "casos_rag.json")
    parser.add_argument("--k", type=int, default=rag.K_PADRAO)
    parser.add_argument("--embeddings", choices=("local", "bedrock"), default="local")
    parser.add_argument("--minimo", type=float, default=0.8, help="taxa mínima de acerto (0 a 1)")
    args = parser.parse_args()

    gerente = rag.GerenciadorPolitica(args.embeddings, RAIZ / ".cache")
    politica = gerente.carregar_arquivo(args.politica)
    casos = json.loads(args.casos.read_text(encoding="utf-8"))
    print(f"{politica.rotulo()} · {len(politica.secoes)} seções · embeddings {politica.info()['embeddings']} · k={args.k}\n")

    acertos = 0
    for caso in casos:
        achados = politica.similares(caso["consulta"], args.k)
        ids = [s.id for s, _ in achados]
        ok = any(e in ids for e in caso["esperadas"])
        acertos += ok
        ranking = ", ".join(f"{s.id}({score:.2f})" for s, score in achados)
        print(f"{'OK ' if ok else 'ERRO'} esperado {'/'.join(caso['esperadas']):8} → {ranking}  | {caso['consulta'][:60]}")

    taxa = acertos / len(casos) if casos else 0.0
    print(f"\nAcerto@{args.k}: {acertos}/{len(casos)} = {taxa:.0%} (mínimo {args.minimo:.0%})")
    return 0 if taxa >= args.minimo else 1


if __name__ == "__main__":
    sys.exit(main())
