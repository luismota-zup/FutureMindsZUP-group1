"""Política interna (RAG): lê o documento, separa em seções, indexa com embeddings e recupera trechos.

A recuperação é híbrida: similaridade vetorial (FAISS) com o texto da reclamação, mais as seções cujo
título cita o canal ou o produto da linha, mais a rubrica de urgência quando o agente pede.
Nenhuma regra da política fica no código: tudo vem do documento carregado, que pode ser trocado.

Dependências (requirements.txt): pypdf (só para PDF), faiss-cpu e fastembed (embeddings locais).
Para o Titan (Nível 3), `--embeddings bedrock` usa langchain-aws, com a mesma interface
`embed_documents` / `embed_query`. Este módulo nunca chama o modelo de linguagem.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 3_000_000
MAX_SECOES = 300
MAX_TEXTO_SECAO = 3000
MIN_CORPO = 15
BLOCO, SOBREPOSICAO = 800, 150
K_PADRAO = 4
MAX_POR_TERMO = 2
EXTENSOES = (".pdf", ".md", ".txt")
MODELO_LOCAL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODELO_TITAN = "amazon.titan-embed-text-v2:0"
REGIAO_TITAN = "us-east-1"

TRACOS = ("-", "–", "—", "•")
# Título numerado: "2. TÍTULO" no nível 1 (ponto obrigatório) ou "2.1 Título" nos demais.
CANDIDATO = re.compile(r"(?<!\S)(\d{1,2}(?:\.\d{1,2}){0,3})(\.?)\s+(?=[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ])")
MARKDOWN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
DOCUMENTO = re.compile(r"Documento:\s*([A-Z][A-Z0-9-]{2,40})")
VERSAO = re.compile(r"Vers[ãa]o:\s*([0-9][\w.]{0,15})", re.I)

_EMBED_LOCK = threading.Lock()


class PoliticaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Secao:
    id: str
    titulo: str
    contexto: str
    texto: str

    def para_indice(self) -> str:
        return " › ".join(x for x in (self.contexto, self.texto) if x)


# ------------------------------------------------------------------ leitura
def normalizar(texto: str) -> str:
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    linhas = (re.sub(r"[ \t]+", " ", linha).strip() for linha in texto.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()


def extrair_texto(nome: str, dados: bytes) -> str:
    extensao = Path(nome or "").suffix.lower()
    if extensao not in EXTENSOES:
        raise PoliticaError(400, "Use um arquivo .pdf, .md ou .txt.")
    if not dados:
        raise PoliticaError(400, "O arquivo da política está vazio.")
    if len(dados) > MAX_BYTES:
        raise PoliticaError(413, "Arquivo da política grande demais (máximo de 3 MB).")
    if extensao == ".pdf":
        if not dados.startswith(b"%PDF"):
            raise PoliticaError(400, "O arquivo não parece ser um PDF.")
        try:
            from pypdf import PdfReader
        except ImportError:
            raise PoliticaError(503, "Instale as dependências do requirements.txt para ler PDF.") from None
        try:
            texto = "\n".join(pagina.extract_text() or "" for pagina in PdfReader(io.BytesIO(dados)).pages)
        except Exception:  # noqa: BLE001 - PDF malformado ou protegido
            raise PoliticaError(400, "Não foi possível ler o PDF da política.") from None
    else:
        try:
            texto = dados.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise PoliticaError(400, "Salve o arquivo da política em UTF-8.") from None
    texto = normalizar(texto)
    if len(texto) < 50:
        raise PoliticaError(400, "O documento não tem texto legível (PDF escaneado como imagem?).")
    return texto


def chave(texto: str) -> str:
    """Minúsculas, sem acento e sem pontuação, para comparar títulos com canal, produto e urgência."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).split())


def contem(titulo: str, termo: str) -> bool:
    termo = chave(termo)
    return bool(termo) and f" {termo} " in f" {chave(titulo)} "


# ------------------------------------------------------------------ separação em seções
def _maiusculo(palavra: str) -> bool:
    letras = [c for c in palavra if c.isalpha()]
    return all(c.isupper() for c in letras) if letras else palavra not in TRACOS


def titulo_de(corpo: str) -> str:
    palavras, titulo = corpo.split(), []
    if palavras and any(c.isalpha() for c in palavras[0]) and _maiusculo(palavras[0]):
        for palavra in palavras[:12]:
            if not _maiusculo(palavra):
                break
            titulo.append(palavra)
    else:
        for palavra in palavras[:10]:
            if palavra in TRACOS or palavra.endswith(":"):
                break
            titulo.append(palavra)
        else:
            titulo = titulo[:6]  # sem delimitador, não deixa o título engolir o corpo
    return " ".join(titulo).strip(" -–—:")[:90]


def _sucessor(anterior: tuple | None, novo: tuple) -> bool:
    if anterior is None:
        return novo == (1,)
    if novo == anterior + (1,):
        return True
    return any(novo == anterior[:i] + (anterior[i] + 1,) for i in range(len(anterior)))


def _numeradas(texto: str) -> list[tuple[tuple, int, int]]:
    aceitos, anterior = [], None
    for m in CANDIDATO.finditer(texto):
        numero = tuple(int(p) for p in m.group(1).split("."))
        if len(numero) == 1 and not m.group(2):
            continue
        if _sucessor(anterior, numero):
            aceitos.append((numero, m.start(), m.end()))
            anterior = numero
    return aceitos


def _montar(itens: list[tuple[str, int, str, str | None]]) -> list[Secao]:
    """itens: (id, nível, corpo, título conhecido). Seções só com título viram contexto das filhas."""
    secoes, pilha = [], []
    for i, (ident, nivel, corpo, conhecido) in enumerate(itens):
        titulo = conhecido or titulo_de(corpo)
        pilha = [p for p in pilha if p[0] < nivel]
        contexto = " › ".join(t for _, t in pilha)
        resto = corpo[len(titulo):] if corpo.startswith(titulo) else corpo
        tem_filhas = i + 1 < len(itens) and itens[i + 1][1] > nivel
        if corpo and (not tem_filhas or len(re.sub(r"\W", "", resto)) >= MIN_CORPO):
            secoes.append(Secao(ident, titulo or ident, contexto, corpo[:MAX_TEXTO_SECAO]))
        pilha.append((nivel, titulo))
    return secoes


def _por_markdown(texto: str) -> list[Secao]:
    marcas = list(MARKDOWN.finditer(texto))
    itens = []
    for i, m in enumerate(marcas):
        fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        cabecalho = m.group(2).strip()
        numero = re.match(r"(\d+(?:\.\d+)*)\.?\s+(.*)", cabecalho)
        ident = numero.group(1) if numero else f"S{i + 1}"
        titulo = numero.group(2) if numero else cabecalho
        titulo = " ".join(titulo.split())[:90]
        corpo = " ".join((titulo + " " + texto[m.end():fim]).split())
        itens.append((ident, len(m.group(1)), corpo, titulo))
    return _montar(itens)


def _por_blocos(texto: str) -> list[Secao]:
    plano = " ".join(texto.split())
    secoes, inicio, n = [], 0, 0
    while inicio < len(plano):
        fim = min(len(plano), inicio + BLOCO)
        if fim < len(plano):
            espaco = plano.rfind(" ", inicio + BLOCO // 2, fim)
            fim = espaco if espaco > 0 else fim
        n += 1
        corpo = plano[inicio:fim].strip()
        secoes.append(Secao(f"B{n}", " ".join(corpo.split()[:8]), "", corpo))
        if fim >= len(plano):
            break
        proximo = plano.find(" ", max(inicio + 1, fim - SOBREPOSICAO))
        inicio = proximo + 1 if 0 < proximo < fim else fim
    return secoes


def separar_secoes(texto: str) -> tuple[list[Secao], str]:
    """Devolve as seções e o método usado: numerada, markdown ou blocos."""
    if len(MARKDOWN.findall(texto)) >= 3:
        secoes, metodo = _por_markdown(texto), "markdown"
    else:
        marcas = _numeradas(texto)
        secoes, metodo = [], "numerada"
        if len(marcas) >= 3:
            itens = []
            for i, (numero, _, fim) in enumerate(marcas):
                proximo = marcas[i + 1][1] if i + 1 < len(marcas) else len(texto)
                itens.append((".".join(map(str, numero)), len(numero), " ".join(texto[fim:proximo].split()), None))
            secoes = _montar(itens)
    if len(secoes) < 3:
        secoes, metodo = _por_blocos(texto), "blocos"
    return secoes[:MAX_SECOES], metodo


# ------------------------------------------------------------------ embeddings e índice
class EmbeddingsLocais:
    """fastembed (ONNX, sem PyTorch) com a interface de embeddings do LangChain."""

    def __init__(self, cache_dir: Path | None = None, modelo: str = MODELO_LOCAL):
        if sys.platform == "win32" and (Path(sys.prefix) / "msvcp140.dll").is_file():
            os.add_dll_directory(sys.prefix)  # runtime do Visual C++ instalado pelo pacote msvc-runtime
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise PoliticaError(503, "Instale as dependências do requirements.txt para usar o RAG local.") from None
        self.nome = modelo
        pasta = str(cache_dir) if cache_dir else None
        try:  # usa o cache sem tocar a rede; só baixa (~240 MB) se o modelo ainda não estiver em disco
            self._modelo = TextEmbedding(modelo, cache_dir=pasta, local_files_only=True)
        except Exception:
            try:
                self._modelo = TextEmbedding(modelo, cache_dir=pasta)
            except Exception as erro:
                raise PoliticaError(503, f"Não foi possível carregar o modelo de embeddings {modelo}: "
                                         f"{type(erro).__name__}.") from None

    def embed_documents(self, textos: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._modelo.embed(textos)]

    def embed_query(self, texto: str) -> list[float]:
        return list(map(float, next(iter(self._modelo.query_embed(texto)))))


def criar_embeddings(tipo: str, cache_dir: Path | None = None):
    if tipo == "local":
        return EmbeddingsLocais(cache_dir)
    if tipo == "bedrock":
        try:
            from langchain_aws import BedrockEmbeddings
        except ImportError:
            raise PoliticaError(503, "Instale langchain-aws e boto3 para usar o Titan.") from None
        modelo = BedrockEmbeddings(model_id=MODELO_TITAN, region_name=REGIAO_TITAN)
        modelo.nome = MODELO_TITAN
        return modelo
    raise PoliticaError(400, "Embeddings inválidos: use local ou bedrock.")


class Indice:
    """FAISS em memória com produto interno sobre vetores normalizados (similaridade de cosseno)."""

    def __init__(self, vetores: list[list[float]]):
        try:
            import faiss
            import numpy as np
        except ImportError:
            raise PoliticaError(503, "Instale as dependências do requirements.txt (faiss-cpu).") from None
        self._faiss, self._np = faiss, np
        matriz = np.asarray(vetores, dtype="float32")
        faiss.normalize_L2(matriz)
        self._indice = faiss.IndexFlatIP(matriz.shape[1])
        self._indice.add(matriz)

    def buscar(self, vetor: list[float], k: int) -> list[tuple[int, float]]:
        consulta = self._np.asarray([vetor], dtype="float32")
        self._faiss.normalize_L2(consulta)
        distancias, posicoes = self._indice.search(consulta, min(k, self._indice.ntotal))
        return [(int(p), float(d)) for p, d in zip(posicoes[0], distancias[0]) if p >= 0]


# ------------------------------------------------------------------ política carregada
class Politica:
    """Snapshot imutável de um documento indexado. Trocar a política cria outro objeto."""

    def __init__(self, nome: str, dados: bytes, embeddings):
        texto = extrair_texto(nome, dados)
        self.secoes, self.metodo = separar_secoes(texto)
        documento, versao = DOCUMENTO.search(texto[:2000]), VERSAO.search(texto[:2000])
        self.documento = documento.group(1) if documento else None
        self.versao = versao.group(1) if versao else None
        self.nome = Path(nome).name[:120]
        self.hash = hashlib.sha256(dados).hexdigest()
        self.embeddings_nome = getattr(embeddings, "nome", type(embeddings).__name__)
        self._embeddings = embeddings
        try:
            with _EMBED_LOCK:
                vetores = embeddings.embed_documents([s.para_indice() for s in self.secoes])
        except PoliticaError:
            raise
        except Exception:  # noqa: BLE001 - fronteira com o provedor de embeddings
            raise PoliticaError(502, "Falha ao gerar os embeddings da política.") from None
        self._indice = Indice(vetores)
        self._cache: dict[tuple[str, int], list[tuple[Secao, float]]] = {}
        self._cache_lock = threading.Lock()

    def rotulo(self) -> str:
        nome = " ".join(x for x in (self.documento, f"v{self.versao}" if self.versao else None) if x)
        return f"{nome or self.nome} · {self.hash[:12]}"

    def info(self) -> dict:
        return {"nome": self.nome, "documento": self.documento, "versao": self.versao,
                "hash": self.hash[:12], "rotulo": self.rotulo(), "separacao": self.metodo,
                "embeddings": self.embeddings_nome, "total_secoes": len(self.secoes),
                "secoes": [f"{s.id} {s.titulo}" for s in self.secoes]}

    def similares(self, consulta: str, k: int = K_PADRAO) -> list[tuple[Secao, float]]:
        consulta = consulta.strip()[:2000]
        if not consulta:
            return []
        with self._cache_lock:
            if (consulta, k) in self._cache:
                return self._cache[(consulta, k)]
        try:
            with _EMBED_LOCK:
                vetor = self._embeddings.embed_query(consulta)
        except Exception:  # noqa: BLE001 - fronteira com o provedor de embeddings
            raise PoliticaError(502, "Falha ao gerar o embedding da consulta à política.") from None
        achados = [(self.secoes[i], round(score, 3)) for i, score in self._indice.buscar(vetor, k)]
        with self._cache_lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[(consulta, k)] = achados
        return achados

    def por_termo(self, termo: str) -> list[Secao]:
        return [s for s in self.secoes if contem(s.titulo, termo)][:MAX_POR_TERMO]

    def urgencias(self, nivel: str = "") -> list[Secao]:
        return [s for s in self.secoes if contem(s.titulo, "urgencia") and (not nivel or contem(s.titulo, nivel))]

    def recuperar(self, modos: tuple[str, ...], consulta: str, canal: str = "", produto: str = "",
                  urgencia: str = "", k: int = K_PADRAO) -> list[dict]:
        """Trechos para um agente, na ordem do documento, com a origem de cada um.

        modos: busca (vetorial + canal + produto), urgencia (rubrica completa),
        urgencia_decidida (só a seção da urgência já decidida por um agente anterior).
        """
        similares = self.similares(consulta, k) if "busca" in modos else []
        scores = {s.id: score for s, score in similares}
        escolhidas: dict[str, str] = {}

        def incluir(secoes: list[Secao], origem: str):
            for secao in secoes:
                escolhidas.setdefault(secao.id, origem)
        if "urgencia" in modos:
            incluir(self.urgencias(), "rubrica de urgência")
        if "urgencia_decidida" in modos and urgencia:
            incluir(self.urgencias(urgencia), "urgência decidida")
        if "busca" in modos:
            incluir(self.por_termo(canal), "canal")
            incluir(self.por_termo(produto), "produto")
            incluir([s for s, _ in similares], "similaridade")
        return [{"secao": s.id, "titulo": s.titulo, "texto": s.texto, "origem": escolhidas[s.id],
                 "score": scores.get(s.id)} for s in self.secoes if s.id in escolhidas]


class GerenciadorPolitica:
    """Guarda a política vigente. A troca é atômica: lotes já iniciados mantêm o snapshot anterior."""

    def __init__(self, tipo_embeddings: str = "local", cache_dir: Path | None = None, embeddings=None):
        self.tipo = tipo_embeddings
        self._cache_dir = cache_dir
        self._embeddings = embeddings
        self._lock = threading.Lock()
        self._atual: Politica | None = None

    def _modelo(self):
        with self._lock:
            if self._embeddings is None:
                self._embeddings = criar_embeddings(self.tipo, self._cache_dir)
            return self._embeddings

    def carregar(self, nome: str, dados: bytes) -> Politica:
        nova = Politica(nome, dados, self._modelo())
        with self._lock:
            self._atual = nova
        return nova

    def carregar_arquivo(self, caminho: Path) -> Politica:
        try:
            dados = Path(caminho).read_bytes()
        except OSError:
            raise PoliticaError(404, f"Arquivo da política não encontrado: {Path(caminho).name}") from None
        return self.carregar(Path(caminho).name, dados)

    def atual(self) -> Politica | None:
        with self._lock:
            return self._atual
