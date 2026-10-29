import hashlib
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import politica_rag as rag  # noqa: E402

try:
    import faiss  # noqa: F401
    import numpy  # noqa: F401
    TEM_FAISS = True
except ImportError:
    TEM_FAISS = False
try:
    import pypdf  # noqa: F401
    TEM_PYPDF = True
except ImportError:
    TEM_PYPDF = False

PDF = HERE / "Docs - Challenge" / "KS_POLITICA_INTERNA (4).pdf"

POLITICA_MD = """Documento: POL-TESTE-9 | Versão: 3.1

# 1. Objetivo
Esta política define prazos e responsáveis para reclamações de clientes.

# 2. Urgência
## 2.1 Urgência BAIXA
Definição: dúvidas simples sem impacto financeiro. Prazo de cinco dias úteis.
## 2.2 Urgência CRÍTICA
Definição: indícios de fraude ou menção a órgão regulador. Prazo de quatro horas.

# 3. Produtos
## 3.1 Cartão de Crédito
Cobrança indevida no cartão: contestar a fatura e estornar provisoriamente.
## 3.2 Seguros
Negativa de sinistro: reanálise obrigatória por outro perito.

# 4. Canais
## 4.1 Banco Central / Procon
Reclamação recebida por órgão regulador deve ser revisada pelo jurídico.
"""


class EmbeddingsFalsos:
    """Saco de palavras com hash: determinístico, sem rede e sem modelo."""
    nome = "falso"

    def __init__(self):
        self.chamadas = 0

    def _vetor(self, texto):
        vetor = [0.0] * 64
        for palavra in rag.chave(texto).split():
            vetor[int(hashlib.md5(palavra.encode()).hexdigest(), 16) % 64] += 1.0
        return vetor if any(vetor) else [1.0] + [0.0] * 63

    def embed_documents(self, textos):
        return [self._vetor(t) for t in textos]

    def embed_query(self, texto):
        self.chamadas += 1
        return self._vetor(texto)


class SeparacaoTests(unittest.TestCase):
    @unittest.skipUnless(TEM_PYPDF and PDF.is_file(), "pypdf ou PDF da política indisponível")
    def test_pdf_oficial_vira_16_secoes_com_titulos_e_metadados(self):
        texto = rag.extrair_texto(PDF.name, PDF.read_bytes())
        secoes, metodo = rag.separar_secoes(texto)
        self.assertEqual(metodo, "numerada")
        self.assertEqual([s.id for s in secoes], ["1", "2.1", "2.2", "2.3", "2.4", "3.1", "3.2", "3.3", "3.4", "3.5",
                                                  "4.1", "4.2", "4.3", "4.4", "5", "6"])
        titulos = {s.id: s.titulo for s in secoes}
        self.assertEqual((titulos["2.4"], titulos["3.1"], titulos["4.3"]),
                         ("Urgência CRÍTICA", "Cartão de Crédito", "Banco Central / Procon"))
        self.assertIn("PROCEDIMENTOS POR CANAL", next(s for s in secoes if s.id == "4.3").contexto)
        self.assertEqual(rag.DOCUMENTO.search(texto).group(1), "POL-SAC-001")

    def test_markdown_e_documento_sem_numeracao(self):
        secoes, metodo = rag.separar_secoes(rag.normalizar(POLITICA_MD))
        self.assertEqual(metodo, "markdown")
        self.assertEqual([s.id for s in secoes], ["1", "2.1", "2.2", "3.1", "3.2", "4.1"])
        corrido = "Texto sem títulos numerados sobre atendimento ao cliente. " * 40
        blocos, metodo = rag.separar_secoes(corrido)
        self.assertEqual(metodo, "blocos")
        self.assertGreater(len(blocos), 2)
        self.assertTrue(all(len(b.texto) <= rag.BLOCO for b in blocos))

    def test_numeros_soltos_nao_viram_titulo(self):
        texto = ("Versão: 2.0 Uso interno. 1. OBJETIVO Definir prazos de atendimento ao cliente. "
                 "Prazo de 5 Dias para retorno. 2. PRAZOS Até 3 Horas em casos graves. "
                 "2.1 Regra Um Detalhe da regra um com texto suficiente. 2.2 Regra Dois Detalhe da regra dois aqui.")
        secoes, metodo = rag.separar_secoes(texto)
        self.assertEqual(metodo, "numerada")
        self.assertEqual([s.id for s in secoes], ["1", "2", "2.1", "2.2"])

    def test_rejeita_arquivos_invalidos(self):
        casos = [("p.docx", b"x" * 100, 400), ("p.md", b"", 400), ("p.md", b"a" * (rag.MAX_BYTES + 1), 413),
                 ("p.pdf", b"nao e pdf" * 20, 400), ("p.txt", "curto".encode(), 400),
                 ("p.txt", b"\xff\xfe" + b"\x00" * 100, 400)]
        for nome, dados, status in casos:
            with self.assertRaises(rag.PoliticaError) as erro:
                rag.extrair_texto(nome, dados)
            self.assertEqual(erro.exception.status, status, nome)


@unittest.skipUnless(TEM_FAISS, "faiss-cpu indisponível: rode com o Python do finguard_env")
class RecuperacaoTests(unittest.TestCase):
    def setUp(self):
        self.embeddings = EmbeddingsFalsos()
        self.politica = rag.Politica("politica.md", POLITICA_MD.encode(), self.embeddings)

    def test_metadados_e_rotulo(self):
        info = self.politica.info()
        self.assertEqual((info["documento"], info["versao"], info["total_secoes"]), ("POL-TESTE-9", "3.1", 6))
        self.assertTrue(self.politica.rotulo().startswith("POL-TESTE-9 v3.1 · "))

    def test_busca_hibrida_por_canal_produto_e_similaridade(self):
        trechos = self.politica.recuperar(("busca",), "negativa de sinistro pelo perito",
                                          canal="Banco Central", produto="Cartão de Crédito", k=1)
        origens = {t["secao"]: t["origem"] for t in trechos}
        self.assertEqual(origens, {"3.1": "produto", "3.2": "similaridade", "4.1": "canal"})
        self.assertEqual([t["secao"] for t in trechos], ["3.1", "3.2", "4.1"])  # ordem do documento
        self.assertIsNotNone(next(t for t in trechos if t["secao"] == "3.2")["score"])

    def test_rubrica_e_urgencia_decidida(self):
        rubrica = self.politica.recuperar(("urgencia",), "")
        self.assertEqual([t["secao"] for t in rubrica], ["2.1", "2.2"])
        decidida = self.politica.recuperar(("urgencia_decidida",), "", urgencia="Crítica")
        self.assertEqual([t["secao"] for t in decidida], ["2.2"])
        self.assertEqual(self.politica.recuperar(("urgencia_decidida",), ""), [])
        self.assertEqual(self.embeddings.chamadas, 0)  # sem "busca", nenhuma consulta vetorial

    def test_sem_duplicatas_e_cache_da_consulta(self):
        trechos = self.politica.recuperar(("busca", "urgencia"), "fraude órgão regulador", canal="Banco Central")
        ids = [t["secao"] for t in trechos]
        self.assertEqual(len(ids), len(set(ids)))
        self.politica.recuperar(("busca",), "fraude órgão regulador")
        self.assertEqual(self.embeddings.chamadas, 1)

    def test_troca_de_politica_preserva_snapshot_anterior(self):
        gerente = rag.GerenciadorPolitica(embeddings=self.embeddings)
        antiga = gerente.carregar("a.md", POLITICA_MD.encode())
        nova = gerente.carregar("b.md", POLITICA_MD.replace("POL-TESTE-9", "POL-TESTE-10").encode())
        self.assertIs(gerente.atual(), nova)
        self.assertEqual(antiga.documento, "POL-TESTE-9")
        self.assertNotEqual(antiga.hash, nova.hash)
        with self.assertRaises(rag.PoliticaError):
            gerente.carregar("c.docx", b"x" * 100)
        self.assertIs(gerente.atual(), nova)  # falha na troca mantém a vigente

    def test_falha_do_provedor_de_embeddings_vira_erro_controlado(self):
        class Quebrado(EmbeddingsFalsos):
            def embed_query(self, texto):
                raise RuntimeError("segredo interno")
        politica = rag.Politica("p.md", POLITICA_MD.encode(), Quebrado())
        with self.assertRaises(rag.PoliticaError) as erro:
            politica.recuperar(("busca",), "cartão")
        self.assertNotIn("segredo", str(erro.exception))


if __name__ == "__main__":
    unittest.main()
