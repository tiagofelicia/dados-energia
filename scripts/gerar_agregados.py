#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_agregados.py
Pré-calcula médias e totais do OMIE e da produção REN em data/agregados/.

PORQUÊ
------
Hoje, para saber a média mensal do OMIE em 2023, é preciso descarregar
omie_historico_2023.csv (1,9 MB) e agregar 35 040 linhas. Para ter a série
diária completa 2010-2026 são 17 ficheiros e ~32 MB. Estes agregados respondem
à maioria das perguntas com uma fracção disso:

    omie_anual.csv       ~2 KB     17 linhas
    omie_mensal.csv      ~25 KB    ~200 linhas
    omie_diario.csv      ~700 KB   ~6100 linhas   (45x menos que os 32 MB)

O mesmo para produção. Nenhum destes ficheiros contém informação nova — são
derivados dos CSV que já estão publicados. O que dão é acesso barato.

CONSISTÊNCIA COM OS RECORDES
----------------------------
As definições vêm dos scripts de recordes, importadas e não recopiadas, para
que agregados e recordes nunca divergam:

  • ler_csv_omie / adicionar_colunas  ← gerar_records_omie.py
  • agregar_diario / ler_bombagem     ← gerar_records_producao.py

Daí decorrem, entre outras:
  • MW → MWh com QH = 0,25 (os dados são potência média por quarto de hora)
  • hídrica renovável = hídrica − bombagem turbinada, quando há dado de bombagem
  • produção nacional inclui injeção de baterias, exclui importação
  • "horas negativas" conta HORAS com média horária < 0, não quartos

FUTUROS: A ARMADILHA PRINCIPAL
------------------------------
O omie_dados_atuais.csv contém datas futuras estimadas a partir dos futuros
OMIP — hoje, 255 dias reais e 111 estimados, até 01/01/2027. Agregar sem cortar
publicaria estimativas de mercado como se fossem histórico.

O corte usa a Data_Valores_OMIE declarada em futuros_omip.csv (com recurso à
cauda TABELA_ATUALIZACOES do próprio omie_dados_atuais.csv). Sem essa data, o
script ABORTA em vez de adivinhar: é preferível não publicar agregados a
publicar futuros disfarçados de histórico.

USO
---
  python gerar_agregados.py            # tudo
  python gerar_agregados.py --so-omie
  python gerar_agregados.py --so-producao

SAÍDA
-----
  data/agregados/omie_{diario,mensal,anual}.csv
  data/agregados/producao_{diario,mensal,anual}.csv
  data/agregados/metadata.json

A coluna 'dias' diz quantos dias entraram em cada linha — é o que permite ver
que o mês ou ano corrente ainda está incompleto, sem ter de adivinhar.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from gerar_records_omie import (  # noqa: E402
    ler_csv_omie,
    adicionar_colunas,
    cortar_futuros,
)
from gerar_records_producao import (  # noqa: E402
    ler_csv_producao,
    ler_bombagem,
    agregar_diario,
    cortar_dia_incompleto,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PASTA_OMIE = os.path.join(ROOT_DIR, "data", "omie")
PASTA_PROD = os.path.join(ROOT_DIR, "data", "producao")
PASTA_OUT = os.path.join(ROOT_DIR, "data", "agregados")

# Ciclos horários BTN: coluna do CSV -> {código no CSV: sufixo da coluna de saída}
# V = Vazio, F = Fora de Vazio, C = Cheias, P = Ponta
CICLOS = {
    "BD": {"V": "bd_v", "F": "bd_f"},
    "BS": {"V": "bs_v", "F": "bs_f"},
    "TD": {"V": "td_v", "C": "td_c", "P": "td_p"},
    "TS": {"V": "ts_v", "C": "ts_c", "P": "ts_p"},
}
COLS_CICLO = [c for m in CICLOS.values() for c in m.values()]


# ============================================================
# Leitura
# ============================================================

def carregar_omie():
    """Históricos + ano corrente, com os dias futuros cortados."""
    partes = []
    hist = os.path.join(PASTA_OMIE, "historico")
    if os.path.isdir(hist):
        for nome in sorted(os.listdir(hist)):
            if nome.startswith("omie_historico_") and nome.endswith(".csv"):
                partes.append(ler_csv_omie(os.path.join(hist, nome)))

    atuais = os.path.join(PASTA_OMIE, "omie_dados_atuais.csv")
    tem_atuais = os.path.exists(atuais)
    if tem_atuais:
        partes.append(ler_csv_omie(atuais))

    if not partes:
        raise FileNotFoundError("nenhum CSV OMIE encontrado em data/omie/")

    df = adicionar_colunas(pd.concat(partes, ignore_index=True))

    if tem_atuais:
        # Fonte única da distinção real/estimado: gerar_records_omie.
        df = cortar_futuros(df)

    return df.dropna(subset=["preco_pt"])


def carregar_producao():
    """Históricos + ano corrente, já agregados em diário pela lógica dos records."""
    partes = []
    hist = os.path.join(PASTA_PROD, "historico")
    if os.path.isdir(hist):
        for nome in sorted(os.listdir(hist)):
            if nome.startswith("producao_historico_") and nome.endswith(".csv"):
                partes.append(ler_csv_producao(os.path.join(hist, nome)))

    atuais = os.path.join(PASTA_PROD, "producao_dados_atuais.csv")
    if os.path.exists(atuais):
        partes.append(ler_csv_producao(atuais))

    if not partes:
        raise FileNotFoundError("nenhum CSV de produção encontrado")

    # O dia em curso vem com as fontes despacháveis a zero a partir da hora da
    # recolha; incluí-lo inflaciona a quota renovável e subestima as emissões.
    bruto = cortar_dia_incompleto(pd.concat(partes, ignore_index=True))
    return agregar_diario(bruto, ler_bombagem())


# ============================================================
# Agregação OMIE
# ============================================================

def _horas_negativas(g):
    """Horas com média horária < 0 — mesma definição do gerar_records_omie."""
    return int((g.groupby("dia_hora")["preco_pt"].mean() < 0).sum())


def agregar_omie(df, chave, rotulo):
    """Agrega por 'chave' (série de grupo). Devolve DataFrame ordenado."""
    linhas = []
    for valor, g in df.groupby(chave, sort=False):
        linha = {
            rotulo: valor,
            "dias": int(g["data"].dt.date.nunique()),
            "quartos": int(len(g)),
            "pt_medio": g["preco_pt"].mean(),
            "pt_min": g["preco_pt"].min(),
            "pt_max": g["preco_pt"].max(),
            "pt_desvio": g["preco_pt"].std(),
            "es_medio": g["preco_es"].mean(),
            "spread_pt_es": (g["preco_pt"] - g["preco_es"]).mean(),
            "horas_negativas": _horas_negativas(g),
        }
        for col_ciclo, mapa in CICLOS.items():
            if col_ciclo not in g.columns:
                continue
            medias = g.groupby(col_ciclo)["preco_pt"].mean()
            for codigo, destino in mapa.items():
                linha[destino] = medias.get(codigo)
        linhas.append(linha)

    out = pd.DataFrame(linhas)
    for c in out.columns:
        if c not in (rotulo, "dias", "quartos", "horas_negativas"):
            out[c] = pd.to_numeric(out[c], errors="coerce").round(2)
    return out


# ============================================================
# Agregação produção
# ============================================================

MAPA_PROD = {
    "consumo_gwh": "consumoGwh",
    "producao_nac_gwh": "producaoNacGwh",
    "renovavel_gwh": "renovavelGwh",
    "hidrica_gwh": "hidricaGwh",
    "hidrica_renov_gwh": "hidricaRenovGwh",
    "eolica_gwh": "eolicaGwh",
    "solar_gwh": "solarGwh",
    "biomassa_gwh": "biomassaGwh",
    "gas_natural_gwh": "gasNaturalGwh",
    "carvao_gwh": "carvaoGwh",
    "outra_termica_gwh": "outraGwh",
    "baterias_inj_gwh": "baterIngGwh",
    "bombagem_consumo_gwh": "bombagemConsumoGwh",
    "saldo_importador_gwh": "saldoImpGwh",
}


def agregar_producao(daily, chave, rotulo):
    """Soma energias e tira o pico máximo. % renovável é recalculada do total.

    A percentagem NÃO é a média das percentagens diárias — isso daria peso
    igual a um dia de consumo baixo e a um de consumo alto. É o rácio dos
    totais do período, que é a definição correcta.
    """
    linhas = []
    for valor, g in daily.groupby(chave, sort=False):
        linha = {rotulo: valor, "dias": int(len(g))}
        for destino, origem in MAPA_PROD.items():
            linha[destino] = float(g[origem].sum()) if origem in g.columns else None
        linha["pico_consumo_mw"] = float(g["Consumo_p"].max()) if "Consumo_p" in g.columns else None

        nac, ren = linha["producao_nac_gwh"], linha["renovavel_gwh"]
        linha["perc_renovavel"] = (100 * ren / nac) if nac else None
        linhas.append(linha)

    out = pd.DataFrame(linhas)
    for c in out.columns:
        if c not in (rotulo, "dias"):
            out[c] = pd.to_numeric(out[c], errors="coerce").round(2)
    return out


# ============================================================
# Escrita
# ============================================================

def gravar(df, nome):
    """Escreve só se mudou, com a convenção de formato da casa."""
    os.makedirs(PASTA_OUT, exist_ok=True)
    caminho = os.path.join(PASTA_OUT, nome)
    novo = df.to_csv(index=False, encoding="utf-8", lineterminator="\n")
    antigo = None
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
            antigo = f.read()
    mudou = novo != antigo
    if mudou:
        with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
            f.write(novo)
    kb = len(novo.encode("utf-8")) / 1024
    print(f"   {'✓' if mudou else '=':>2} {nome:24s} {len(df):6d} linhas  {kb:7.1f} KB")
    return mudou


def main():
    p = argparse.ArgumentParser(description="Pré-calcula agregados OMIE e produção.")
    p.add_argument("--so-omie", action="store_true")
    p.add_argument("--so-producao", action="store_true")
    args = p.parse_args()

    fazer_omie = not args.so_producao
    fazer_prod = not args.so_omie
    meta = {}
    mudou = False

    if fazer_omie:
        print("📊 OMIE")
        df = carregar_omie()
        print(f"  {len(df):,} quartos · {df['data'].dt.date.nunique():,} dias · "
              f"{df['data'].min().date()} → {df['data'].max().date()}")

        diario = agregar_omie(df, df["dia"], "dia")
        diario.insert(1, "data_iso",
                      pd.to_datetime(diario["dia"], format="%d/%m/%Y").dt.strftime("%Y-%m-%d"))
        diario = diario.sort_values("data_iso").reset_index(drop=True)

        mensal = agregar_omie(df, df["mes"], "mes").sort_values(
            "mes", key=lambda s: pd.to_datetime(s, format="%m/%Y")).reset_index(drop=True)
        anual = agregar_omie(df, df["ano"], "ano").sort_values("ano").reset_index(drop=True)

        mudou = any([
            gravar(diario, "omie_diario.csv"),
            gravar(mensal, "omie_mensal.csv"),
            gravar(anual, "omie_anual.csv"),
        ]) or mudou
        meta["omie"] = {
            "primeira_data": str(df["data"].min().date()),
            "ultima_data": str(df["data"].max().date()),
            "dias": int(df["data"].dt.date.nunique()),
        }

    if fazer_prod:
        print("\n⚡ Produção")
        daily = carregar_producao()
        daily["mes"] = daily["dt"].dt.strftime("%m/%Y")
        daily["ano"] = daily["dt"].dt.year
        print(f"  {len(daily):,} dias · {daily['dt'].min().date()} → {daily['dt'].max().date()}")

        diario = agregar_producao(daily, daily["dia"], "dia")
        diario.insert(1, "data_iso", daily["dt"].dt.strftime("%Y-%m-%d").values)
        diario = diario.drop(columns="dias").sort_values("data_iso").reset_index(drop=True)

        mensal = agregar_producao(daily, daily["mes"], "mes").sort_values(
            "mes", key=lambda s: pd.to_datetime(s, format="%m/%Y")).reset_index(drop=True)
        anual = agregar_producao(daily, daily["ano"], "ano").sort_values("ano").reset_index(drop=True)

        mudou = any([
            gravar(diario, "producao_diario.csv"),
            gravar(mensal, "producao_mensal.csv"),
            gravar(anual, "producao_anual.csv"),
        ]) or mudou
        meta["producao"] = {
            "primeira_data": str(daily["dt"].min().date()),
            "ultima_data": str(daily["dt"].max().date()),
            "dias": int(len(daily)),
        }

    meta["gerado_em"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["nota"] = ("Derivados de data/omie/ e data/producao/. Os dias futuros "
                    "estimados a partir dos futuros OMIP estão excluídos.")
    # Só reescrever se algum CSV mudou: o carimbo gerado_em sozinho faria um
    # commit por corrida sobre dados idênticos.
    caminho_meta = os.path.join(PASTA_OUT, "metadata.json")
    if mudou or not os.path.exists(caminho_meta):
        with open(caminho_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")

    print("\n✅ data/agregados/")


if __name__ == "__main__":
    main()
