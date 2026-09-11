#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_intensidade_carbonica.py
Calcula a intensidade carbónica da produção elétrica portuguesa, a 15 minutos,
desde 2010, e publica-a agregada em data/emissoes/.

  intensidade_diaria.csv          ~6100 linhas   por dia
  intensidade_mensal.csv           ~200 linhas   por mês
  intensidade_anual.csv              17 linhas   por ano
  intensidade_perfil_horario.csv    ~408 linhas  média por hora do dia × ano
  fatores_emissao.csv                            os fatores usados, para auditoria
  metadata.json

Nenhuma fonte externa: deriva de data/producao/, que já está publicado.

MÉTODO
------
Para cada intervalo de 15 minutos:

    intensidade (gCO2eq/kWh) = Σ(potência_fonte × fator_fonte) / Σ(potência_fonte)

Os MW cancelam-se, pelo que o resultado é directamente uma intensidade. As
agregações (dia, mês, ano) são SEMPRE ponderadas pela produção — a média das
intensidades daria o mesmo peso a uma hora de madrugada e a uma de ponta.

FATORES DE EMISSÃO
------------------
IPCC AR5 (2014), Working Group III, Annex III, Tabela A.III.2 — medianas de
emissões de CICLO DE VIDA, em gCO2eq/kWh. Inclui construção, fabrico,
operação e desmantelamento.

Isto NÃO são emissões directas de combustão. Um parque solar tem 48 gCO2eq/kWh
de ciclo de vida e zero de combustão. Por isso estes valores **não são
comparáveis** com o Inventário Nacional de Emissões da APA, que contabiliza
apenas emissões directas — os números aqui são, por construção, mais altos.

Os fatores estão em FATORES_LCA, são exportados para fatores_emissao.csv e
podem ser alterados num único sítio.

ASSUNÇÕES, E QUANTO PESAM
-------------------------
  • "Outra Térmica" agrega fuelóleo, gasóleo, resíduos e biogás, sem um valor
    do AR5 que lhe corresponda. Usa-se 700 gCO2eq/kWh, entre o gás (490) e o
    carvão (820). Pesa 0,89 % da produção do período, pelo que variar este
    fator entre 490 e 820 move o resultado global menos de 0,3 gCO2eq/kWh —
    o script imprime essa sensibilidade em cada corrida.
  • Cogeração a gás usa o fator do ciclo combinado (490). A cogeração real tem
    parte das emissões alocada ao calor útil, pelo que este valor SOBRESTIMA a
    parcela eléctrica.
  • Injeção de baterias conta como 0: a emissão foi contabilizada quando a
    bateria carregou da rede. Contá-la outra vez seria dupla contagem.
  • A hídrica usa 24 gCO2eq/kWh na totalidade, incluindo a fração turbinada de
    bombagem. Essa fração tem na verdade a intensidade da rede no momento em
    que bombeou; a coluna bombagem_gwh do ficheiro diário permite a quem quiser
    fazer esse ajuste.

O QUE ESTA MÉTRICA É — E O QUE NÃO É
------------------------------------
É a intensidade da PRODUÇÃO NACIONAL. Não é a intensidade do consumo: Portugal
importa e exporta com Espanha, e a eletricidade importada traz a intensidade do
mix espanhol, não do português.

Por isso cada linha traz também saldo_importador_gwh e importacao_perc_consumo:
quanto maior a importação, menos a intensidade da produção representa o que o
consumidor final realmente consumiu. Em anos de forte importação a diferença é
material.

Calcular a intensidade do consumo exigiria o mix espanhol à mesma granularidade.
O repositório só o tem desde 2026-01 (data/mapas/producao/, diário), o que
cobriria 6 % do período — ficou de fora em vez de se publicar meia série.

USO
---
  python gerar_intensidade_carbonica.py
  python gerar_intensidade_carbonica.py --desde 2020
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from gerar_records_producao import (  # noqa: E402
    ler_csv_producao,
    cortar_dia_incompleto,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PASTA_PROD = os.path.join(ROOT_DIR, "data", "producao")
PASTA_OUT = os.path.join(ROOT_DIR, "data", "emissoes")

# gCO2eq/kWh — IPCC AR5 WG3 Annex III, Tabela A.III.2 (medianas de ciclo de vida)
# coluna do CSV da REN -> (nome curto, fator, proveniência)
FATORES_LCA = {
    "Hídrica":                        ("hidrica",             24,  "IPCC AR5 — Hydropower"),
    "Eólica":                         ("eolica",              11,  "IPCC AR5 — Wind onshore"),
    "Solar":                          ("solar",               48,  "IPCC AR5 — Solar PV utility"),
    "Biomassa":                       ("biomassa",           230,  "IPCC AR5 — Biomass dedicated"),
    "Ondas":                          ("ondas",               17,  "IPCC AR5 — Ocean"),
    "Gás Natural - Ciclo Combinado":  ("gas_ciclo_combinado", 490, "IPCC AR5 — Gas combined cycle"),
    "Gás natural - Cogeração":        ("gas_cogeracao",       490, "IPCC AR5 — Gas combined cycle (ver assunções)"),
    "Carvão":                         ("carvao",             820,  "IPCC AR5 — Coal PC"),
    "Outra Térmica":                  ("outra_termica",      700,  "Assunção: entre gás e carvão (ver assunções)"),
    "Injeção de Baterias":            ("baterias_injecao",     0,  "Emissão contabilizada na carga"),
}

# Limites do teste de sensibilidade da "Outra Térmica" (gás ... carvão)
SENSIBILIDADE_OUTRA = (490, 820)


def carregar():
    """Históricos + ano corrente, a 15 minutos."""
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
        raise FileNotFoundError("nenhum CSV de produção encontrado em data/producao/")

    df = cortar_dia_incompleto(pd.concat(partes, ignore_index=True))
    df["_dt"] = pd.to_datetime(df["dia"], format="%d/%m/%Y", errors="coerce")
    return df.dropna(subset=["_dt"]).reset_index(drop=True)


def calcular(df, fator_outra=None):
    """Acrescenta produção total e emissões por intervalo. Devolve o DataFrame.

    fator_outra permite sobrepor o fator da 'Outra Térmica' para o teste de
    sensibilidade, sem repetir o resto do cálculo.
    """
    prod = pd.Series(0.0, index=df.index)   # MW somados das fontes de geração
    emis = pd.Series(0.0, index=df.index)   # MW × gCO2eq/kWh

    for col, (_curto, fator, _fonte) in FATORES_LCA.items():
        if col not in df.columns:
            continue
        if col == "Outra Térmica" and fator_outra is not None:
            fator = fator_outra
        mw = pd.to_numeric(df[col], errors="coerce").fillna(0).clip(lower=0)
        prod += mw
        emis += mw * fator

    out = df.copy()
    out["_prod_mw"] = prod
    out["_emis"] = emis
    return out


def agregar(df, chave, rotulo):
    """Agrega por 'chave'. A intensidade é sempre ponderada pela produção."""
    QH = 0.25
    linhas = []
    for valor, g in df.groupby(chave, sort=False):
        prod_mwh = float(g["_prod_mw"].sum()) * QH
        if prod_mwh <= 0:
            continue

        # Unidades, passo a passo, porque é fácil enganar-se:
        #   _emis  = Σ(MW × gCO2eq/kWh)
        #   × QH   → MWh × g/kWh = 1000 kWh × g/kWh = kg de CO2eq
        #   / 1000 → toneladas
        emis_t = float(g["_emis"].sum()) * QH / 1000.0

        # Estas já saem em GWh (MW × 0,25 h = MWh; ÷1000 = GWh) — não voltar
        # a dividir ao montar a linha.
        def gwh(col):
            s = pd.to_numeric(g.get(col), errors="coerce")
            return float(s.fillna(0).sum()) * QH / 1000.0 if s is not None else 0.0

        imp, exp = gwh("Importação"), gwh("Exportação")
        cons, bomb = gwh("Consumo"), gwh("Bombagem")

        inten = g["_emis"] / g["_prod_mw"].where(g["_prod_mw"] > 0)
        linha = {
            rotulo: valor,
            "dias": int(g["_dt"].dt.date.nunique()),
            "intensidade_gco2_kwh": round(emis_t * 1000.0 / prod_mwh, 1),
            "emissoes_kt": round(emis_t / 1000.0, 1),
            "producao_gwh": round(prod_mwh / 1000.0, 2),
            "consumo_gwh": round(cons, 2),
            "saldo_importador_gwh": round(imp - exp, 2),
            "bombagem_gwh": round(bomb, 2),
            "importacao_perc_consumo": round(100.0 * imp / cons, 2) if cons > 0 else None,
            "intensidade_min": round(float(inten.min()), 1) if inten.notna().any() else None,
            "intensidade_max": round(float(inten.max()), 1) if inten.notna().any() else None,
        }
        linhas.append(linha)
    return pd.DataFrame(linhas)


def perfil_horario(df):
    """Intensidade média por hora do dia e por ano — 'quando é mais limpa?'."""
    d = df.copy()
    d["_hora"] = d["intervalo"].astype(str).str.strip().str.lstrip("[").str[:2]
    d = d[d["_hora"].str.isdigit()]
    d["_ano"] = d["_dt"].dt.year
    g = d.groupby(["_ano", "_hora"], sort=True).agg(
        emis=("_emis", "sum"), prod=("_prod_mw", "sum"))
    g = g[g["prod"] > 0]
    out = g.reset_index()
    out["intensidade_gco2_kwh"] = (out["emis"] / out["prod"]).round(1)
    return out.rename(columns={"_ano": "ano", "_hora": "hora"})[
        ["ano", "hora", "intensidade_gco2_kwh"]]


def gravar(df, nome):
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
    print(f"   {'✓' if mudou else '=':>2} {nome:32s} {len(df):5d} linhas  {kb:6.1f} KB")
    return mudou


def main():
    p = argparse.ArgumentParser(
        description="Intensidade carbónica da produção elétrica portuguesa.")
    p.add_argument("--desde", type=int, help="ano inicial (default: tudo)")
    args = p.parse_args()

    print("🌱 Intensidade carbónica — produção nacional (IPCC AR5, ciclo de vida)")
    bruto = carregar()
    if args.desde:
        bruto = bruto[bruto["_dt"].dt.year >= args.desde]
    df = calcular(bruto)
    df["_mes"] = df["_dt"].dt.strftime("%m/%Y")
    df["_ano"] = df["_dt"].dt.year

    n_dias = df["_dt"].dt.date.nunique()
    print(f"  {len(df):,} intervalos · {n_dias:,} dias · "
          f"{df['_dt'].min().date()} → {df['_dt'].max().date()}")

    diaria = agregar(df, df["dia"], "dia")
    diaria.insert(1, "data_iso",
                  pd.to_datetime(diaria["dia"], format="%d/%m/%Y").dt.strftime("%Y-%m-%d"))
    diaria = diaria.drop(columns="dias").sort_values("data_iso").reset_index(drop=True)

    mensal = agregar(df, df["_mes"], "mes").sort_values(
        "mes", key=lambda s: pd.to_datetime(s, format="%m/%Y")).reset_index(drop=True)
    anual = agregar(df, df["_ano"], "ano").sort_values("ano").reset_index(drop=True)
    perfil = perfil_horario(df)

    fatores = pd.DataFrame(
        [{"fonte_csv": col, "fonte": curto, "gco2eq_kwh": f, "proveniencia": p_}
         for col, (curto, f, p_) in FATORES_LCA.items()])

    print()
    mudou = any([
        gravar(diaria, "intensidade_diaria.csv"),
        gravar(mensal, "intensidade_mensal.csv"),
        gravar(anual, "intensidade_anual.csv"),
        gravar(perfil, "intensidade_perfil_horario.csv"),
        gravar(fatores, "fatores_emissao.csv"),
    ])

    # Sensibilidade da única assunção com margem real de escolha.
    base = float(anual["emissoes_kt"].sum() * 1e6 / (anual["producao_gwh"].sum() * 1e3))
    variantes = []
    for f in SENSIBILIDADE_OUTRA:
        a = agregar(calcular(bruto, fator_outra=f), bruto["_dt"].dt.year, "ano")
        variantes.append(float(a["emissoes_kt"].sum() * 1e6 / (a["producao_gwh"].sum() * 1e3)))
    amplitude = max(variantes) - min(variantes)
    print(f"\n  Sensibilidade a 'Outra Térmica' ({SENSIBILIDADE_OUTRA[0]}–"
          f"{SENSIBILIDADE_OUTRA[1]} gCO2eq/kWh):")
    print(f"    intensidade média do período: {base:.1f} gCO2eq/kWh "
          f"· amplitude {amplitude:.2f} ({100 * amplitude / base:.2f} %)")

    meta = {
        "metrica": "intensidade carbónica da produção elétrica nacional",
        "unidade": "gCO2eq/kWh",
        "ambito": "ciclo de vida (não comparável com inventários de emissões diretas)",
        "fatores": "IPCC AR5 WG3 Annex III, Tabela A.III.2 (medianas)",
        "primeira_data": str(df["_dt"].min().date()),
        "ultima_data": str(df["_dt"].max().date()),
        "dias": int(n_dias),
        "intensidade_media_periodo": round(base, 1),
        "sensibilidade_outra_termica_pct": round(100 * amplitude / base, 2),
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Só reescrever o metadata se algum CSV mudou: o carimbo gerado_em sozinho
    # faria um commit por corrida sobre dados idênticos.
    caminho_meta = os.path.join(PASTA_OUT, "metadata.json")
    if mudou or not os.path.exists(caminho_meta):
        with open(caminho_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")

    print("\n  Intensidade anual (gCO2eq/kWh):")
    for _, r in anual.iterrows():
        barra = "█" * int(r["intensidade_gco2_kwh"] / 12)
        print(f"    {int(r['ano'])}  {r['intensidade_gco2_kwh']:6.1f}  {barra}")

    print("\n✅ data/emissoes/")


if __name__ == "__main__":
    main()
