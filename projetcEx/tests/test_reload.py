import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import servidor  # noqa: E402


class FilhoFalso:
    def __init__(self, codigo_saida=None, demora_para_encerrar=False):
        self.returncode = codigo_saida
        self.demora_para_encerrar = demora_para_encerrar
        self.terminado = False
        self.morto = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminado = True

    def kill(self):
        self.morto = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.demora_para_encerrar and not self.morto:
            raise subprocess.TimeoutExpired("filho", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class InstantaneoArquivosTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        raiz = Path(self.tmp.name)
        self.projeto = raiz / "projeto"
        self.projeto.mkdir()
        (raiz / "server.py").write_text("x = 1\n", encoding="utf-8")
        (self.projeto / "servidor.py").write_text("x = 1\n", encoding="utf-8")
        (self.projeto / "politica.md").write_text("politica\n", encoding="utf-8")
        for pasta in (".venv", ".cache", "tests", "__pycache__", "FM3", ".claude"):
            (self.projeto / pasta).mkdir()
            (self.projeto / pasta / "ignorado.py").write_text("x = 1\n", encoding="utf-8")
        (self.projeto / "pacote").mkdir()
        (self.projeto / "pacote" / "modulo.py").write_text("x = 1\n", encoding="utf-8")
        (self.projeto / "pagina.html").write_text("<p>oi</p>", encoding="utf-8")
        patcher = mock.patch.object(servidor, "HERE", self.projeto)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.politica = self.projeto / "politica.md"

    def nomes(self, estado):
        return {Path(caminho).relative_to(self.tmp.name).as_posix() for caminho in estado}

    def test_observa_py_do_projeto_server_da_raiz_e_politica(self):
        estado = servidor.instantaneo_arquivos(self.politica)
        self.assertEqual(self.nomes(estado), {
            "server.py", "projeto/servidor.py", "projeto/pacote/modulo.py", "projeto/politica.md"})

    def test_ignora_pastas_de_ambiente_testes_e_ocultas(self):
        estado = servidor.instantaneo_arquivos(self.politica)
        for pasta in (".venv", ".cache", "tests", "__pycache__", "FM3", ".claude"):
            self.assertFalse(any(f"/{pasta}/" in caminho for caminho in estado), pasta)

    def test_ignora_arquivos_lidos_do_disco_a_cada_uso(self):
        estado = servidor.instantaneo_arquivos(self.politica)
        self.assertFalse(any(caminho.endswith(".html") for caminho in estado))

    def test_detecta_alteracao_em_py(self):
        antes = servidor.instantaneo_arquivos(self.politica)
        alvo = self.projeto / "servidor.py"
        os.utime(alvo, (alvo.stat().st_atime, alvo.stat().st_mtime + 5))
        self.assertNotEqual(servidor.instantaneo_arquivos(self.politica), antes)

    def test_detecta_alteracao_na_politica(self):
        antes = servidor.instantaneo_arquivos(self.politica)
        os.utime(self.politica, (self.politica.stat().st_atime, self.politica.stat().st_mtime + 5))
        self.assertNotEqual(servidor.instantaneo_arquivos(self.politica), antes)

    def test_detecta_arquivo_novo_e_removido(self):
        antes = servidor.instantaneo_arquivos(self.politica)
        novo = self.projeto / "novo.py"
        novo.write_text("x = 2\n", encoding="utf-8")
        com_novo = servidor.instantaneo_arquivos(self.politica)
        self.assertNotEqual(com_novo, antes)
        novo.unlink()
        self.assertEqual(servidor.instantaneo_arquivos(self.politica), antes)

    def test_politica_inexistente_nao_quebra(self):
        estado = servidor.instantaneo_arquivos(self.projeto / "nao_existe.pdf")
        self.assertNotIn("projeto/nao_existe.pdf", self.nomes(estado))

    def test_sem_alteracao_o_instantaneo_e_estavel(self):
        self.assertEqual(servidor.instantaneo_arquivos(self.politica), servidor.instantaneo_arquivos(self.politica))


class EncerrarFilhoTests(unittest.TestCase):
    def test_filho_ativo_e_terminado(self):
        filho = FilhoFalso()
        servidor.encerrar_filho(filho)
        self.assertTrue(filho.terminado)
        self.assertFalse(filho.morto)

    def test_filho_que_nao_encerra_no_prazo_e_morto(self):
        filho = FilhoFalso(demora_para_encerrar=True)
        servidor.encerrar_filho(filho)
        self.assertTrue(filho.terminado)
        self.assertTrue(filho.morto)

    def test_filho_ja_encerrado_nao_e_tocado(self):
        filho = FilhoFalso(codigo_saida=1)
        servidor.encerrar_filho(filho)
        self.assertFalse(filho.terminado)
        self.assertFalse(filho.morto)


class ExecutarComReloadTests(unittest.TestCase):
    def executar(self, estados, filhos, passos, argv=("servidor.py", "--reload", "--port", "9999")):
        criados = []

        def criar_filho(comando, *args, **kwargs):
            criados.append(comando)
            return filhos[len(criados) - 1]

        contagem = {"n": 0}

        def dormir(segundos):
            contagem["n"] += 1
            if contagem["n"] > passos:
                raise KeyboardInterrupt

        saida = io.StringIO()
        with contextlib.redirect_stdout(saida), \
                mock.patch.object(servidor.sys, "argv", list(argv)), \
                mock.patch.object(servidor.signal, "signal") as sinal, \
                mock.patch.object(servidor.subprocess, "Popen", side_effect=criar_filho), \
                mock.patch.object(servidor.time, "sleep", side_effect=dormir), \
                mock.patch.object(servidor, "instantaneo_arquivos", side_effect=estados):
            servidor.executar_com_reload(Path("politica.pdf"))
        return criados, saida.getvalue(), sinal

    def test_reinicia_o_filho_quando_um_arquivo_muda(self):
        primeiro, segundo = FilhoFalso(), FilhoFalso()
        criados, saida, _ = self.executar(
            estados=[{"a": 1}, {"a": 1}, {"a": 2}], filhos=[primeiro, segundo], passos=2)
        self.assertEqual(len(criados), 2)
        self.assertTrue(primeiro.terminado)
        self.assertTrue(segundo.terminado)
        self.assertIn("Reiniciando", saida)

    def test_sem_alteracao_nao_reinicia(self):
        filho = FilhoFalso()
        criados, saida, _ = self.executar(estados=[{"a": 1}] * 4, filhos=[filho], passos=3)
        self.assertEqual(len(criados), 1)
        self.assertNotIn("Reiniciando", saida)

    def test_filho_nao_recebe_reload_e_mantem_os_demais_argumentos(self):
        criados, _, _ = self.executar(
            estados=[{"a": 1}] * 2, filhos=[FilhoFalso()], passos=1,
            argv=("servidor.py", "--reload", "--port", "9999", "--provider", "claude"))
        comando = criados[0]
        self.assertEqual(comando[0], sys.executable)
        self.assertEqual(Path(comando[1]).name, "servidor.py")
        self.assertNotIn("--reload", comando)
        self.assertEqual(comando[2:], ["--port", "9999", "--provider", "claude"])

    def test_filho_que_caiu_avisa_uma_vez_e_aguarda_alteracao(self):
        caiu, novo = FilhoFalso(codigo_saida=1), FilhoFalso()
        criados, saida, _ = self.executar(
            estados=[{"a": 1}] * 4 + [{"a": 2}], filhos=[caiu, novo], passos=4)
        self.assertEqual(saida.count("O servidor encerrou"), 1)
        self.assertIn("código 1", saida)
        self.assertEqual(len(criados), 2)

    def test_sigterm_vira_encerramento_limpo(self):
        _, _, sinal = self.executar(estados=[{"a": 1}] * 2, filhos=[FilhoFalso()], passos=1)
        sinal.assert_called_once_with(servidor.signal.SIGTERM, servidor.encerrar_por_sinal)
        with self.assertRaises(KeyboardInterrupt):
            servidor.encerrar_por_sinal(servidor.signal.SIGTERM, None)

    def test_ctrl_c_encerra_o_filho(self):
        filho = FilhoFalso()
        self.executar(estados=[{"a": 1}] * 2, filhos=[filho], passos=0)
        self.assertTrue(filho.terminado)


class ArgumentosReloadTests(unittest.TestCase):
    def rodar(self, *args):
        with mock.patch.object(servidor.sys, "argv", ["servidor.py", *args]), \
                contextlib.redirect_stderr(io.StringIO()) as erro:
            try:
                servidor.main()
            except SystemExit as saida:
                return saida.code, erro.getvalue()
        return None, erro.getvalue()

    def test_reload_com_configure_e_recusado(self):
        codigo, erro = self.rodar("--reload", "--configure")
        self.assertEqual(codigo, 2)
        self.assertIn("--reload não combina com --configure", erro)

    def test_reload_chama_o_supervisor_e_nao_sobe_o_servidor(self):
        with mock.patch.object(servidor, "executar_com_reload") as supervisor, \
                mock.patch.object(servidor, "ProcServer") as servidor_http:
            codigo, _ = self.rodar("--reload", "--provider", "claude")
        self.assertIsNone(codigo)
        supervisor.assert_called_once()
        servidor_http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
