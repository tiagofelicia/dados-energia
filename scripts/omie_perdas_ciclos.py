#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
omie_perdas_ciclos.py
Ficheiros por ano da tabela OMIE_PERDAS_CICLOS do simulador de eletricidade.

O QUE HÁ
--------
  base/OMIE_PERDAS_CICLOS_base_AAAA.csv   exportada pelo autor, uma vez por ano:
                                          o calendário (Data, Hora), os ciclos
                                          horários, os perfis BTN e as perdas
  csv/OMIE_PERDAS_CICLOS_AAAA.csv         publicada: a base + OMIE + ERC
  csv/OMIE_PERDAS_CICLOS.csv              transição: ano anterior + ano atual num
                                          só ficheiro, para as versões publicadas
                                          dos simuladores que ainda o leem
  csv/manifest.json → "anos"              {"2025": "fechado", "2026": "aberto"}

Um ano ABERTO é reescrito pelo bot a cada corrida a partir da base: OMIE real e
futuros, ERC real e previsão própria. Um ano FECHADO já não tem estimativas: o
OMIE fica congelado (não é revisto), mas o ERC acompanha as correções que a REN
faz meses depois e a base do ano, se for corrigida — o ficheiro só é reescrito
quando um destes muda, por isso na prática deixa de mudar cerca de um ano depois
(o coletor do ERC só revê os últimos 365 dias).

Um ano PASSADO que ainda não exista (ex.: 2024) entra pela base: o bot junta-lhe o
OMIE do histórico (data/omie/historico/omie_historico_AAAA.csv) e o ERC real, e
fecha-o logo.

Partilhado pelo atualizar_tarifarios_eletricidade.py (que escreve os ficheiros),
pelo validar_tarifarios_eletricidade.py, pelo atualizar_precos-horarios_csv.py e
pelo gerar_erc_resumo.py (que leem os períodos horários e as perdas).
"""

import glob
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_SIMULADOR = os.path.join(ROOT_DIR, "data", "simuladores", "simulador-tarifarios-eletricidade")
PASTA_CSV = os.path.join(PASTA_SIMULADOR, "csv")
PASTA_BASE = os.path.join(PASTA_SIMULADOR, "base")

NOME = "OMIE_PERDAS_CICLOS"
FICHEIRO_COMBINADO = NOME + ".csv"

# Ordem das colunas igual à da antiga folha do xlsx (os simuladores não dependem
# dela, mas assim os ficheiros por ano são fatias exatas do combinado)
COLUNAS_BASE = ["Data", "Hora", "Simp", "BD", "BS", "TD", "TS", "BTN_A", "BTN_B", "BTN_C", "Perdas"]
COLUNAS_ANO = ["Data", "Hora", "Simp", "BD", "BS", "TD", "TS", "BTN_A", "BTN_B", "BTN_C", "OMIE", "Perdas", "ERC"]
COLUNAS_TEXTO = ["Data", "Hora", "Simp", "BD", "BS", "TD", "TS"]

FECHADO, ABERTO = "fechado", "aberto"
FUSO = "Europe/Lisbon"
# 1.º dia com ERC da REN (atualizar_erc_ren.DATA_INICIAL): antes, um ano fechado
# fica sem ERC e os simuladores usam a constante, como em qualquer dia sem ERC
ERC_DESDE = date(2024, 3, 14)
PASTA_HISTORICO_OMIE = os.path.join(ROOT_DIR, "data", "omie", "historico")
# Etiqueta da hora como a dos anos que os simuladores já usam: "00:15" … "23:59"
# (o simulador procura o OMIE por "data_HH:MM"; "24:00" ou "01:00a" não batiam)
RE_HORA = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def caminho_historico(ano):
    return os.path.join(PASTA_HISTORICO_OMIE, f"omie_historico_{ano}.csv")


def caminho_ano(ano, pasta=None):
    return os.path.join(pasta or PASTA_CSV, f"{NOME}_{ano}.csv")


def caminho_base(ano, pasta=None):
    return os.path.join(pasta or PASTA_BASE, f"{NOME}_base_{ano}.csv")


def _anos_no_padrao(padrao, regex):
    anos = []
    for f in glob.glob(padrao):
        m = re.search(regex, os.path.basename(f))
        if m:
            anos.append(int(m.group(1)))
    return sorted(anos)


def anos_publicados(pasta=None):
    return _anos_no_padrao(os.path.join(pasta or PASTA_CSV, f"{NOME}_*.csv"), rf"^{NOME}_(\d{{4}})\.csv$")


def anos_com_base(pasta=None):
    return _anos_no_padrao(os.path.join(pasta or PASTA_BASE, f"{NOME}_base_*.csv"), rf"^{NOME}_base_(\d{{4}})\.csv$")


def ler(caminho):
    """CSV de base ou de ano: as colunas de texto ficam texto (a Data é MM/DD/AAAA,
    a Hora é uma etiqueta) e os números são lidos ao bit (round_trip), para voltarem
    a ser escritos exatamente com os mesmos dígitos."""
    df = pd.read_csv(caminho, encoding="utf-8-sig", float_precision="round_trip",
                     dtype={c: str for c in COLUNAS_TEXTO})
    df = df.dropna(how="all").reset_index(drop=True)
    for c in COLUNAS_TEXTO:
        if c in df.columns:
            df[c] = df[c].str.strip()
    return df


def ler_manifest(pasta=None):
    try:
        with open(os.path.join(pasta or PASTA_CSV, "manifest.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def estado_dos_anos(pasta_csv=None, pasta_base=None):
    """Que anos há e em que estado estão.

    Quem decide se um ano está fechado é o "anos" do manifest publicado — o bot
    escreve-o quando fecha um ano. Na 1.ª corrida (manifest ainda sem "anos"), um
    ano ANTERIOR ao atual publicado sem base conta como fechado (o ano atual nunca:
    sem base, é um erro, para não deixar de ser atualizado sem ninguém dar por
    isso). Um ano está aberto se tem base e não está fechado; um ano passado com
    base mas ainda sem ficheiro é para importar do histórico (e fecha logo).
    """
    manifest = ler_manifest(pasta_csv)
    publicados = set(anos_publicados(pasta_csv))
    bases = set(anos_com_base(pasta_base))
    no_manifest = manifest.get("anos")
    if isinstance(no_manifest, dict):
        fechados_manifest = {int(a) for a, e in no_manifest.items() if e == FECHADO}
        abertos_manifest = {int(a) for a, e in no_manifest.items() if e == ABERTO}
        fechados = fechados_manifest & publicados
    else:
        fechados_manifest, abertos_manifest = set(), set()
        fechados = {a for a in publicados - bases if a < hoje_pt().year}
    importar = {a for a in bases - publicados - fechados if a < hoje_pt().year}
    return {
        "publicados": publicados,
        "bases": bases,
        "fechados": fechados,
        "fechados_manifest": fechados_manifest,
        "abertos_manifest": abertos_manifest,
        "abertos": sorted(bases - fechados - importar),
        "importar": sorted(importar),
    }


def hoje_pt():
    return datetime.now(timezone.utc).astimezone(ZoneInfo(FUSO)).date()


def quartos_do_dia(d):
    """Quartos de hora de um dia civil em Portugal: 96, ou 92/100 nos dias em que o
    relógio muda."""
    tz = ZoneInfo(FUSO)
    ini = datetime(d.year, d.month, d.day, tzinfo=tz)
    seg = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(days=1)
    horas = (seg.astimezone(timezone.utc) - ini.astimezone(timezone.utc)).total_seconds() / 3600
    return int(round(horas * 4))


def ficheiros_para_leitura(pasta=None):
    """Os ficheiros por ano publicados, do mais recente para o mais antigo (a ordem
    do antigo ficheiro único); se ainda não houver nenhum, o ficheiro único."""
    pasta = pasta or PASTA_CSV
    anos = anos_publicados(pasta)
    if anos:
        return [caminho_ano(a, pasta) for a in sorted(anos, reverse=True)]
    combinado = os.path.join(pasta, FICHEIRO_COMBINADO)
    return [combinado] if os.path.exists(combinado) else []
