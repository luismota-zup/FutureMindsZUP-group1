import csv
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import processador as proc  # noqa: E402

DATASET = Path(os.path.expanduser("~/Downloads/dataset_finguard_desafio_3.csv"))


class NomeTests(unittest.TestCase):
    def test_mascara_nome_apos_introducao_e_mantem_a_frase(self):
        self.assertEqual(proc.mascarar("Meu nome é Patrícia Mendes da Costa e meu cartão foi recusado."),
                         "Meu nome é [NOME OCULTO] e meu cartão foi recusado.")
        self.assertEqual(proc.mascarar("Me chamo Cláudia Beatriz Mendonça, moro em São Paulo."),
                         "Me chamo [NOME OCULTO], moro em São Paulo.")
        self.assertEqual(proc.mascarar("meu nome completo é João Santos."), "meu nome completo é [NOME OCULTO].")

    def test_nome_de_uma_palavra_so_com_introducao_de_nome(self):
        self.assertEqual(proc.mascarar("Meu nome é Ana, liguei hoje."), "Meu nome é [NOME OCULTO], liguei hoje.")
        self.assertEqual(proc.mascarar("Meu nome é Roberto. Liguei três vezes."),
                         "Meu nome é [NOME OCULTO]. Liguei três vezes.")

    def test_sou_a_exige_nome_completo(self):
        self.assertEqual(proc.mascarar("Sou a Maria da Silva, analista."), "Sou a [NOME OCULTO], analista.")
        self.assertEqual(proc.mascarar("Sou o titular da conta e estou insatisfeito."),
                         "Sou o titular da conta e estou insatisfeito.")

    def test_nao_mascara_bancos_orgaos_e_lugares(self):
        texto = "Procurei o Banco Central, o Reclame Aqui e a Defensoria Pública em Belo Horizonte, na Vila Mariana."
        self.assertEqual(proc.mascarar(texto), texto)

    def test_nome_e_cpf_juntos(self):
        mascarado = proc.mascarar("Meu nome é Roberto Fernandes de Alcântara, CPF 341.872.609-55.")
        self.assertEqual(mascarado, "Meu nome é [NOME OCULTO], CPF [CPF OCULTO].")

    def test_saidas_sanitizadas_nao_trazem_o_nome(self):
        self.assertNotIn("Patrícia", proc.sanitizar("Meu nome é Patrícia Soares Andrade e pediu estorno."))


class CepTests(unittest.TestCase):
    def test_mascara_cep_com_e_sem_hifen(self):
        self.assertEqual(proc.mascarar("Moro no CEP 60314-881."), "Moro no CEP [CEP OCULTO].")
        self.assertEqual(proc.mascarar("Endereço 60318-004, Fortaleza."), "Endereço [CEP OCULTO], Fortaleza.")
        self.assertEqual(proc.mascarar("CEP: 01310100 fica perto."), "[CEP OCULTO] fica perto.")

    def test_nao_confunde_cep_com_telefone_nem_valor(self):
        self.assertIn("TELEFONE", proc.mascarar("Liguei para (11) 91234-5678 ontem."))
        self.assertEqual(proc.mascarar("A tarifa foi de R$ 12345,67 neste mês."), "A tarifa foi de R$ 12345,67 neste mês.")


@unittest.skipUnless(DATASET.exists(), "dataset do desafio não está em ~/Downloads")
class DatasetTests(unittest.TestCase):
    def test_nenhum_nome_declarado_ou_cep_sobra_depois_de_mascarar(self):
        with DATASET.open(encoding="utf-8-sig") as arquivo:
            linhas = list(csv.DictReader(arquivo))
        for linha in linhas:
            mascarado = proc.mascarar(linha["texto_reclamacao"])
            for rotulo, padrao in proc.SENSIVEIS:
                if rotulo in ("NOME", "CEP", "CPF"):
                    self.assertIsNone(padrao.search(mascarado), (linha["id"], rotulo))


if __name__ == "__main__":
    unittest.main()
