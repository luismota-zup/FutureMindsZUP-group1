"""Servidor do processador de CSV. Reaproveita utilidades do servidor do caderno, sem expor o caderno nem o tutor.

Uso, a partir da pasta do projeto:  python servidor.py --provider claude [--politica arquivo.pdf]
A política interna (RAG) usa o Python do finguard_env, com as dependências do requirements.txt.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import errno
import getpass
import json
import socket
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import Request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import server as base  # noqa: E402  (servidor do caderno, na pasta acima)
import processador as proc  # noqa: E402
import politica_rag as rag  # noqa: E402  (só biblioteca padrão no import; dependências sob demanda)

PROC_STATIC = {"/processador": ("processador.html", "text/html"),
               "/adr": ("adr.html", "text/html"),
               "/processador.js": ("processador.js", "text/javascript"),
               "/processador.css": ("processador.css", "text/css")}
# Únicas rotas herdadas do servidor do caderno: o estilo base e o indicador de conexão da página.
# O caderno (/) e o tutor (/api/chat) ficam desligados: qualquer outra rota responde 404.
BASE_GET = ("/api/status", "/styles.css")
EXEMPLO = HERE / "exemplo_reclamacoes.csv"
POLITICA_PADRAO = HERE / "Docs - Challenge" / "KS_POLITICA_INTERNA (4).pdf"
ERROS_CONTROLADOS = (proc.ProcError, rag.PoliticaError)
MAX_PROC_BODY = 4_500_000
MAX_OUTPUT_TOKENS = 1500


class ModelError(Exception):
    """Falha ao chamar o modelo. `fatal` indica que repetir só gastaria tokens."""

    def __init__(self, status: int, message: str, fatal: bool):
        super().__init__(message)
        self.status = status
        self.fatal = fatal


FATAL_MESSAGES = {
    400: "A API recusou a configuração. Verifique modelo, protocolo e limite de tokens do provedor.",
    401: "A API recusou a chave. Confira a credencial e reinicie o servidor.",
    403: "Sua conta não tem permissão para esta operação ou modelo.",
    404: "Modelo não encontrado ou indisponível para a conta. Use --model com um modelo permitido.",
    429: "Limite de uso, saldo ou cota da API atingido. Confira sua conta antes de tentar novamente.",
}


ALIASES_MODELO = ("haiku", "sonnet", "opus")


def resolver_modelo(srv, modelo: str = "") -> str:
    """Modelo de um agente (cabeçalho `model:`). Alias haiku/sonnet/opus usa o ID do settings.json do Claude Code;
    qualquer outro valor é enviado como está; vazio usa o modelo do servidor (--model ou settings.json)."""
    modelo = (modelo or "").strip()
    if not modelo:
        return srv.model
    if srv.provider == "claude" and modelo in ALIASES_MODELO:
        return _id_do_alias(modelo)
    return modelo


def _id_do_alias(alias: str) -> str:
    """ID do modelo para haiku/sonnet/opus, lido de ANTHROPIC_DEFAULT_<ALIAS>_MODEL no settings.json do Claude Code."""
    variavel = f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"
    try:
        env = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")).get("env", {})
    except (OSError, ValueError, AttributeError):
        env = {}
    valor = env.get(variavel) if isinstance(env, dict) else None
    if not isinstance(valor, str) or not valor.strip():
        raise ModelError(400, f"O alias “{alias}” não tem ID em {variavel} no settings.json do Claude Code. "
                              "Defina a variável ou escreva o ID do modelo no cabeçalho model:.", True)
    return valor.strip()


def make_call_model(srv):
    """Cria o `call_model(system, user, modelo="")` usado pelos agentes; usa a credencial só no servidor."""
    def call_model(system: str, user: str, modelo: str = "") -> str:
        if not srv.api_key:
            raise ModelError(503, "Configure a API em ~/.claude/settings.json ou use OPENAI_API_KEY / --configure.", True)
        id_modelo = resolver_modelo(srv, modelo)
        messages = [{"role": "user", "content": user}]
        headers = {srv.auth_header: ("Bearer " if srv.auth_header == "Authorization" else "") + srv.api_key,
                   "Content-Type": "application/json"}
        if srv.provider == "claude":
            payload = {"model": id_modelo, "system": system, "messages": messages,
                       "max_tokens": MAX_OUTPUT_TOKENS}
            headers["anthropic-version"] = "2023-06-01"
        else:
            payload = {"model": id_modelo, "instructions": system, "input": messages,
                       "max_output_tokens": MAX_OUTPUT_TOKENS, "store": False}
        request = Request(srv.endpoint or base.API_URL,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
        try:
            with base.urlopen(request, timeout=90) as response:
                data = json.load(response)
        except HTTPError as error:
            code = error.code
            error.close()  # nunca devolve o corpo bruto: pode conter trechos da credencial
            if code in FATAL_MESSAGES:
                raise ModelError(429 if code == 429 else 502, FATAL_MESSAGES[code], True) from None
            raise ModelError(502, "A API está indisponível. Tente novamente mais tarde.", False) from None
        except (TimeoutError, socket.timeout):
            raise ModelError(504, "A API demorou além do limite.", False) from None
        except (URLError, OSError):
            raise ModelError(502, "Não foi possível conectar à API. Verifique rede, VPN e acesso.", False) from None
        except (ValueError, UnicodeDecodeError):
            raise ModelError(502, "A API retornou uma resposta inválida.", False) from None
        try:
            return base.claude_response_text(data) if srv.provider == "claude" else base.response_text(data)
        except base.ChatError as error:
            raise ModelError(error.status, str(error), False) from None
    return call_model


class ProcServer(base.StudyServer):
    def __init__(self, address, api_key: str, model: str, *, root: Path = HERE, **kwargs):
        super().__init__(address, api_key, model, **kwargs)
        self.RequestHandlerClass = ProcHandler  # o construtor base fixa o handler do caderno
        self.root = root
        self.jobs = proc.JobManager()
        self.call_model = make_call_model(self)
        # GerenciadorPolitica (politica_rag); a política enviada pela tela vale até reiniciar o servidor.
        self.politica = None
        self.politica_aviso = "A política interna não foi carregada."

    def politica_atual(self):
        return self.politica.atual() if self.politica is not None else None


class ProcHandler(base.StudyHandler):
    server_version = "FutureMindsProc/1.0"

    def reply(self, status: int, body: bytes, content_type: str, extra: tuple = ()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                         "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def fail(self, error: Exception):
        self.reply_json(error.status, {"error": str(error)})

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        if path in BASE_GET:
            super().do_GET()
            return
        if not (path in PROC_STATIC or path.startswith("/api/proc/")):
            self.reply_json(404, {"error": "Rota não encontrada."})
            return
        if not self.allowed_host():
            self.reply_json(403, {"error": "Acesse pelo endereço local informado no terminal."})
            return
        if path in PROC_STATIC:
            name, content_type = PROC_STATIC[path]
            try:
                body = (HERE / name).read_bytes()
            except OSError:
                self.reply_json(404, {"error": "Arquivo não encontrado."})
                return
            self.reply(200, body, content_type + "; charset=utf-8")
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            self.route_get(path, query)
        except (*ERROS_CONTROLADOS, ModelError) as error:
            self.fail(error)
        except Exception:  # noqa: BLE001 - fronteira HTTP
            self.reply_json(500, {"error": "Falha local ao processar o pedido."})

    def route_get(self, path: str, query: dict):
        root = self.server.root
        if path == "/api/proc/arquivos":
            self.reply_json(200, proc.list_files(root))
        elif path == "/api/proc/arquivo":
            self.reply_json(200, proc.read_file(root, query.get("path", "")))
        elif path == "/api/proc/exemplo":
            try:
                texto = EXEMPLO.read_text(encoding="utf-8")
            except OSError:
                raise proc.ProcError(404, "CSV de exemplo não encontrado.") from None
            self.reply_json(200, {"nome": EXEMPLO.name, "csv": texto})
        elif path == "/api/proc/politica":
            self.reply_json(200, self.estado_politica())
        elif path == "/api/proc/estado":
            job = self.server.jobs.get(query.get("job", ""))
            try:
                desde = max(0, int(query.get("desde", "0")))
            except ValueError:
                desde = 0
            self.reply_json(200, job.snapshot(desde))
        elif path == "/api/proc/logs":
            job = self.server.jobs.get(query.get("job", ""))
            try:
                desde = max(0, int(query.get("desde", "0")))
            except ValueError:
                desde = 0
            self.reply_json(200, proc.logs_page(job, desde))
        elif path == "/api/proc/baixar":
            job = self.server.jobs.get(query.get("job", ""))
            body, content_type, filename = proc.render_output(job, query.get("formato", ""))
            self.reply(200, body, content_type,
                       (("Content-Disposition", f'attachment; filename="{filename}"'),))
        else:
            self.reply_json(404, {"error": "Rota não encontrada."})

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/proc/"):
            self.reply_json(404, {"error": "Rota não encontrada."})
            return
        if not self.allowed_host():
            self.reply_json(403, {"error": "Host não permitido."})
            return
        if self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
            self.reply_json(403, {"error": "Abra a página pelo servidor local para enviar dados."})
            return
        try:
            payload = self.read_json()
            self.route_post(path, payload)
        except (*ERROS_CONTROLADOS, ModelError) as error:
            self.fail(error)
        except Exception:  # noqa: BLE001 - fronteira HTTP
            self.reply_json(500, {"error": "Falha local ao processar o pedido."})

    def read_json(self) -> object:
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise proc.ProcError(415, "Use JSON.")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise proc.ProcError(400, "Tamanho de requisição inválido.") from None
        if not 0 < size <= MAX_PROC_BODY:
            raise proc.ProcError(413, "Requisição vazia ou grande demais.")
        try:
            raw = self.rfile.read(size)
        except (TimeoutError, OSError):
            raise proc.ProcError(408, "Tempo de recebimento esgotado.") from None
        if len(raw) != size:
            raise proc.ProcError(400, "Requisição incompleta.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise proc.ProcError(400, "JSON inválido.") from None

    def route_post(self, path: str, payload: object):
        root = self.server.root
        if not isinstance(payload, dict):
            raise proc.ProcError(400, "Envie um objeto JSON.")
        if path == "/api/proc/arquivo":
            self.reply_json(200, proc.write_file(root, payload.get("path"), payload.get("conteudo")))
        elif path == "/api/proc/arquivo/apagar":
            self.reply_json(200, proc.delete_file(root, payload.get("path")))
        elif path == "/api/proc/previa":
            self.reply_json(200, proc.preview(proc.prepare(root, payload, self.server.politica_atual())))
        elif path == "/api/proc/iniciar":
            plan = proc.prepare(root, payload, self.server.politica_atual())
            if plan["chamadas_max"] == 0:
                raise proc.ProcError(400, "Nenhuma linha pode ser enviada aos agentes (todas bloqueadas ou inválidas).")
            if payload.get("confirmar_chamadas_max") != plan["chamadas_max"]:
                raise proc.ProcError(409, "O plano mudou desde a prévia. Gere a prévia de novo e confirme.")
            if not self.server.api_key:
                raise proc.ProcError(503, "Configure a API em ~/.claude/settings.json ou use OPENAI_API_KEY / --configure.")
            job = self.server.jobs.start(plan, self.server.call_model)
            self.reply_json(200, job.snapshot())
        elif path == "/api/proc/cancelar":
            job = self.server.jobs.get(payload.get("job") if isinstance(payload.get("job"), str) else "")
            job.cancel.set()
            self.reply_json(200, job.snapshot())
        elif path == "/api/proc/politica":
            self.reply_json(200, self.trocar_politica(payload))
        elif path == "/api/proc/politica/buscar":
            self.reply_json(200, self.buscar_politica(payload))
        else:
            self.reply_json(404, {"error": "Rota não encontrada."})

    # ---------------------------------------------------------------- política interna (RAG)
    def estado_politica(self) -> dict:
        atual = self.server.politica_atual()
        return {"carregada": atual is not None, "info": atual.info() if atual else None,
                "aviso": None if atual else self.server.politica_aviso,
                "embeddings": getattr(self.server.politica, "tipo", None), "max_bytes": rag.MAX_BYTES}

    def trocar_politica(self, payload: dict) -> dict:
        if self.server.politica is None:
            raise proc.ProcError(503, self.server.politica_aviso)
        nome, conteudo = payload.get("nome"), payload.get("conteudo_base64")
        if not isinstance(nome, str) or not nome.strip() or not isinstance(conteudo, str):
            raise proc.ProcError(400, "Envie o nome do arquivo e o conteúdo em base64.")
        if len(conteudo) > (rag.MAX_BYTES * 4) // 3 + 4:
            raise proc.ProcError(413, "Arquivo grande demais (máximo de 3 MB).")
        try:
            dados = base64.b64decode(conteudo, validate=True)
        except (binascii.Error, ValueError):
            raise proc.ProcError(400, "Conteúdo base64 inválido.") from None
        self.server.politica.carregar(nome.strip(), dados)  # falha mantém a política vigente
        registrar(f"Política interna trocada pela tela: {self.server.politica_atual().rotulo()}")
        return self.estado_politica()

    def buscar_politica(self, payload: dict) -> dict:
        atual = self.server.politica_atual()
        if atual is None:
            raise proc.ProcError(503, self.server.politica_aviso)
        consulta = payload.get("consulta")
        canal, produto = payload.get("canal") or "", payload.get("produto") or ""
        if (not isinstance(consulta, str) or not 3 <= len(consulta.strip()) <= proc.MAX_CELL
                or not isinstance(canal, str) or not isinstance(produto, str)):
            raise proc.ProcError(400, f"Escreva uma consulta de 3 a {proc.MAX_CELL} caracteres.")
        trechos = atual.recuperar(("busca",), proc.mascarar(consulta), canal=canal[:80], produto=produto[:80])
        return {"politica": atual.rotulo(), "trechos": trechos}


def registrar(mensagem: str) -> None:
    """Log no terminal; se a saída estiver fechada, a operação já feita não vira erro."""
    try:
        print(mensagem, flush=True)
    except (OSError, ValueError):
        pass


def carregar_politica(server: ProcServer, caminho: Path, embeddings: str) -> None:
    """Indexa a política na inicialização. Se faltar dependência ou arquivo, o servidor sobe e avisa."""
    server.politica = rag.GerenciadorPolitica(embeddings, HERE / ".cache")
    print(f"Indexando a política interna ({caminho.name}, embeddings {embeddings})...", flush=True)
    try:
        politica = server.politica.carregar_arquivo(caminho)
    except Exception as error:  # noqa: BLE001 - o servidor sobe mesmo sem a política
        motivo = str(error) if isinstance(error, rag.PoliticaError) else "erro inesperado na indexação."
        server.politica_aviso = (f"A política interna não foi carregada: {motivo} Envie o arquivo no painel "
                                 "“Política interna” ou corrija e reinicie o servidor.")
        print("AVISO: " + server.politica_aviso, flush=True)
        return
    print(f"Política: {politica.rotulo()} · {len(politica.secoes)} seções ({politica.metodo})", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Processador de CSV com agentes do Claude Code.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", choices=("auto", "openai", "claude"), default="auto")
    parser.add_argument("--configure", action="store_true",
                        help="Solicita a chave da OpenAI com entrada oculta; não a grava.")
    parser.add_argument("--politica", type=Path, default=POLITICA_PADRAO,
                        help="Documento da política interna (.pdf, .md ou .txt) indexado para o RAG.")
    parser.add_argument("--embeddings", choices=("local", "bedrock"), default="local",
                        help="local: fastembed multilíngue em .cache (sem rede após o 1º download); "
                             "bedrock: Amazon Titan v2 (Nível 3, exige credencial AWS).")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("A porta deve estar entre 1 e 65535.")
    if args.model is not None and not args.model.strip():
        parser.error("Informe um modelo válido.")
    if args.configure and args.provider == "claude":
        parser.error("Para Claude, configure a credencial em ~/.claude/settings.json.")
    try:
        config = base.resolve_config("openai" if args.configure else args.provider, args.model)
    except ValueError as error:
        parser.error(str(error))
    if args.configure:
        try:
            config.api_key = getpass.getpass("Chave da OpenAI (entrada oculta; Enter para seguir sem IA): ").strip()
            config.source = "Entrada no terminal"
        except (EOFError, KeyboardInterrupt):
            print("\nConfiguração cancelada.")
            return
    try:
        server = ProcServer(("127.0.0.1", args.port), config.api_key, config.model,
                            provider=config.provider, endpoint=config.endpoint,
                            auth_header=config.auth_header, credential_source=config.source)
    except OSError as error:
        print(f"Não foi possível iniciar na porta {args.port}: {error.strerror}.")
        if error.errno == errno.EADDRINUSE:
            print("Use outra porta: python3 projetcEx/servidor.py --port 8767")
        raise SystemExit(1)
    carregar_politica(server, args.politica, args.embeddings)
    print(f"Processador: http://127.0.0.1:{server.server_port}/processador", flush=True)
    print(f"Modelo: {config.provider} · {config.model} · {config.source}" if config.api_key
          else "Sem chave: edição de regras e prévia funcionam; processar exige --configure ou settings.json.",
          flush=True)
    print("Cada execução pede confirmação do número máximo de chamadas. Encerre com Ctrl+C.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
