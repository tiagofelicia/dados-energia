#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_erc_resumo.py
Prepara os dados do ERC da REN (Encargos de Regulação imputados ao Consumo)
para a página /erc do site, em data/agregados/.

PORQUÊ
------
Os ficheiros de data/erc/isp/ são ~13 MB (um por mês, 96 linhas por dia). A
página deixa escolher o período (7 dias, este mês, 12 meses, personalizado…)
e calcula tudo no browser a partir de resumos diários, muito mais pequenos.

O QUE ESCREVE
-------------
erc_resumo.json (~25 KB)
  primeira_data, ultima_data   o intervalo publicado (dia de mercado)
  componentes                  ids e nomes das componentes do ERC
  previsao     os 14 dias seguintes ao último publicado, por quarto de hora,
               com a previsão própria do erc_previsao.py (a dos simuladores e
               dos Preços Horários); os 7 últimos dias reais, para contexto; e
               o erro dessa previsão em cada horizonte, medido no último ano
  diario       anos e intervalo dos ficheiros por ano (abaixo)
  dias_omie    intervalo dos ficheiros por mês (abaixo)

erc_diario/AAAA.json (~250 KB por ano), um por ano civil:
  mercado  por dia de MERCADO: [MWh, €, € de cada componente] — os totais
           que a REN publica; as médias de um mês feitas daqui (Σ€ ÷ ΣMWh)
           batem com o resumo mensal da REN (GetERCByYear)
  dias     por dia em hora de Portugal:
             e    €/MWh de cada hora (0–23), Σ€ ÷ ΣMWh
             m    MWh de cada hora (o peso para juntar dias)
             o    OMIE PT médio de cada hora
             n    quartos de hora do dia (92, 96 ou 100)
             max, min   o quarto de hora mais caro e o mais barato [€/MWh, "HH:MM"]
             p    fator de perdas médio do dia, pesado pelo perfil ERSE BTN C
             c    por ciclo e período horário, pela ordem de ORDEM_C (no
                  cabeçalho do ficheiro, "ordem_c"): €/MWh pesado pelo BTN C e
                  a soma dos pesos × 1000, alternados [€, peso, €, peso, …]
           p e c só existem desde 2025 (a folha do simulador começa aí).

erc_omie/AAAA-MM.json (~50 KB por mês), que a página só descarrega quando se
escolhe um dia: por quarto de hora, o OMIE PT, o ERC (o real ou, nos dias ainda
sem valor publicado, a previsão própria; erc_previsto = quantos o são) e o fator
de perdas (desde 2025). 'slots' só nos dias de mudança de hora.

COMO SE CALCULA
---------------
• Médias como as da REN: Σ€ ÷ ΣMWh (pesadas pelo consumo do mercado).
• Os totais diários usam o dia de MERCADO (data_iso), como a REN; as horas, os
  períodos horários e os extremos usam a hora de Portugal (data_utc).
• Os períodos horários, os pesos BTN C e as perdas vêm da folha
  OMIE_PERDAS_CICLOS do simulador (data/simuladores/.../csv). Cada linha é
  alinhada pelo instante em que começa (dia + posição no dia), como no
  atualizar_tarifarios_eletricidade.py. O OMIE PT vem de data/omie/
  (histórico + dados atuais), só com dias reais.
• O erro da previsão é medido como o simulador a usaria: a partir de origens
  espaçadas de 5 dias no último ano (o dia da semana roda), prevê-se cada um
  dos 14 dias seguintes só com os dados até à origem e compara-se com o real.
  Demora ~1 min (um preparar() por origem).

Nenhum ficheiro é reescrito se o conteúdo não mudou (o gerado_em não conta),
para não haver commits vazios.

USO
---
  python gerar_erc_resumo.py
  python gerar_erc_resumo.py --isp <pasta> --ciclos <csv> --saida <json>
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import erc_previsao
from gerar_records_omie import ATUAIS_PATH, HISTORICO_GLOB, cortar_futuros, ler_csv_omie

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_ISP = os.path.join(ROOT_DIR, "data", "erc", "isp")
FICHEIRO_CICLOS = os.path.join(ROOT_DIR, "data", "simuladores", "simulador-tarifarios-eletricidade",
                               "csv", "OMIE_PERDAS_CICLOS.csv")
PASTA_AGREGADOS = os.path.join(ROOT_DIR, "data", "agregados")
SAIDA = os.path.join(PASTA_AGREGADOS, "erc_resumo.json")
PASTA_DIARIO = os.path.join(PASTA_AGREGADOS, "erc_diario")
PASTA_DIAS = os.path.join(PASTA_AGREGADOS, "erc_omie")

FUSO = "Europe/Lisbon"
JANELA_ERRO_DIAS = 365
DIAS_PREVISAO = erc_previsao.CURTO_PRAZO_DIAS   # 14: o troço de curto prazo
DIAS_REAIS_CONTEXTO = 7
PASSO_ORIGENS_DIAS = 5         # origens do teste de erro: 5 e 7 são primos entre si
ESCALA_PESOS = 1000            # para os pesos irem como inteiros

COMPONENTES = [
    ("rt_pdbf", "Restrições técnicas (PDBF)"),
    ("rt_pdvd", "Restrições técnicas (PDVD)"),
    ("rt_phf", "Restrições técnicas (PHF)"),
    ("banda_afrr", "Banda de reserva aFRR"),
    ("banda_mfrr", "Banda de reserva mFRR"),
    ("outros", "Outros"),
]
CICLOS = {"BD": ["V", "F"], "BS": ["V", "F"], "TD": ["V", "C", "P"], "TS": ["V", "C", "P"]}
ORDEM_C = [f"{c}.{p}" for c, ps in CICLOS.items() for p in ps]


def r(v, casas=3):
    return None if v is None or not np.isfinite(v) else round(float(v), casas)


# ============================================================
# Leitura
# ============================================================

def ler_isp(pasta):
    ficheiros = sorted(glob.glob(os.path.join(pasta, "erc_isp_????-??.csv")))
    if not ficheiros:
        raise SystemExit(f"❌ Sem ficheiros em {pasta}")
    colunas = (["data_iso", "data_utc", "consumo_mwh", "erc_total_eur", "erc_total_eur_mwh"]
               + [f"{c}_eur" for c, _ in COMPONENTES])
    df = pd.concat([pd.read_csv(f, encoding="utf-8-sig", usecols=colunas) for f in ficheiros],
                   ignore_index=True)
    for c in colunas[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["consumo_mwh", "erc_total_eur"])
    df["inicio_utc"] = pd.to_datetime(df["data_utc"], format="%Y-%m-%d %H:%M", utc=True)
    local = df["inicio_utc"].dt.tz_convert(FUSO)
    df["dia_local"] = local.dt.tz_localize(None).dt.normalize()
    df["hora"] = local.dt.hour
    df["hhmm"] = local.dt.strftime("%H:%M")
    return df.sort_values("inicio_utc").reset_index(drop=True)


def ler_ciclos(caminho):
    """Folha OMIE_PERDAS_CICLOS: períodos horários, pesos BTN C e perdas por
    quarto de hora, indexados pelo instante de início em UTC."""
    if not os.path.exists(caminho):
        print(f"⚠️ Sem {caminho}: períodos horários e perdas ficam de fora.")
        return None
    c = pd.read_csv(caminho, encoding="utf-8-sig", dtype=str)
    c = c[c["Data"].notna() & (c["Data"].str.strip() != "")].copy()
    dia = pd.to_datetime(c["Data"].str.strip(), format="%m/%d/%Y")
    pos = c.groupby(dia).cumcount()
    c["inicio_utc"] = (dia.dt.tz_localize(FUSO) + pd.to_timedelta(15 * pos, unit="m")).dt.tz_convert("UTC")
    for col in ["BTN_C", "Perdas"]:
        c[col] = pd.to_numeric(c[col], errors="coerce")
    return c[["inicio_utc", "BD", "BS", "TD", "TS", "BTN_C", "Perdas"]].drop_duplicates("inicio_utc")


def ler_omie_pt(primeiro_dia):
    """OMIE PT por quarto de hora (€/MWh) com o início em UTC, só dias reais."""
    ficheiros = sorted(glob.glob(HISTORICO_GLOB)) + ([ATUAIS_PATH] if os.path.exists(ATUAIS_PATH) else [])
    partes = []
    for ordem, f in enumerate(ficheiros):
        o = ler_csv_omie(f)
        if "preco_pt" not in o.columns:
            continue
        o = o[["dia", "preco_pt"]].copy()
        o["ordem"] = ordem
        partes.append(o)
    if not partes:
        return None
    o = pd.concat(partes, ignore_index=True)
    o["data"] = pd.to_datetime(o["dia"], format="%d/%m/%Y", errors="coerce")
    o = o.dropna(subset=["data"])
    o = o[o["data"] >= pd.Timestamp(primeiro_dia)]
    # Um dia em dois ficheiros fica com o do último lido (o mais recente)
    o = o[o["ordem"] == o.groupby("data")["ordem"].transform("max")]
    o = cortar_futuros(o, obrigatorio=False)
    o = o.reset_index(drop=True)
    pos = o.groupby("data").cumcount()
    o["inicio_utc"] = (o["data"].dt.tz_localize(FUSO) + pd.to_timedelta(15 * pos, unit="m")).dt.tz_convert("UTC")
    o["omie"] = pd.to_numeric(o["preco_pt"], errors="coerce")
    return o[["data", "inicio_utc", "omie"]]


def escrever_json(caminho, conteudo, ignorar=()):
    """Escreve só se o conteúdo mudou (ignorando as chaves dadas). Devolve True se escreveu."""
    if os.path.exists(caminho):
        try:
            antigo = json.load(open(caminho, encoding="utf-8"))
            novo = json.loads(json.dumps(conteudo))
            for k in ignorar:
                antigo.pop(k, None)
                novo.pop(k, None)
            if antigo == novo:
                return False
        except (OSError, ValueError):
            pass
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8", newline="\n") as f:
        json.dump(conteudo, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return True


# ============================================================
# Ficheiros por ano (a base dos blocos que seguem o período)
# ============================================================

def ficheiros_diarios(df, ciclos, omie, pasta):
    comp = [f"{c}_eur" for c, _ in COMPONENTES]

    # Totais por dia de mercado, como a REN
    merc = df.groupby("data_iso")[["consumo_mwh", "erc_total_eur"] + comp].sum()

    # Por dia e hora de Portugal; o 1.º dia local (13/03/2024) só teria a hora
    # das 23:00, que pertence ao 1.º dia de mercado, e fica de fora
    df = df[df["dia_local"] >= pd.Timestamp(df["data_iso"].min())]
    h = df.groupby(["dia_local", "hora"])[["erc_total_eur", "consumo_mwh"]].sum()
    h["e"] = h["erc_total_eur"] / h["consumo_mwh"]
    e_h = h["e"].unstack().reindex(columns=range(24))
    m_h = h["consumo_mwh"].unstack().reindex(columns=range(24))
    n_qh = df.groupby("dia_local").size()

    o_h = None
    if omie is not None:
        oo = omie.assign(dia_local=omie["data"], hora=omie["inicio_utc"].dt.tz_convert(FUSO).dt.hour)
        o_h = oo.groupby(["dia_local", "hora"])["omie"].mean().unstack().reindex(columns=range(24))

    # Extremos de cada dia (quarto de hora)
    qh = df.dropna(subset=["erc_total_eur_mwh"])
    i_max = qh.groupby("dia_local")["erc_total_eur_mwh"].idxmax()
    i_min = qh.groupby("dia_local")["erc_total_eur_mwh"].idxmin()

    # Perdas e períodos horários (desde 2025), pesados pelo BTN C
    perdas, per = {}, {}
    if ciclos is not None:
        j = df[["inicio_utc", "dia_local", "erc_total_eur_mwh"]].merge(ciclos, on="inicio_utc")
        j = j.dropna(subset=["BTN_C"])
        j["we"] = j["erc_total_eur_mwh"] * j["BTN_C"]
        j["wp"] = j["Perdas"] * j["BTN_C"]
        g = j.groupby("dia_local")[["wp", "BTN_C"]].sum()
        perdas = (g["wp"] / g["BTN_C"]).to_dict()
        for cod, periodos in CICLOS.items():
            s = j.groupby(["dia_local", cod])[["we", "BTN_C"]].sum()
            for (dia, p), linha in s.iterrows():
                if p in periodos and linha["BTN_C"] > 0:
                    per.setdefault(dia, {})[f"{cod}.{p}"] = (r(linha["we"] / linha["BTN_C"], 2),
                                                             int(round(linha["BTN_C"] * ESCALA_PESOS)))

    anos = sorted(set(merc.index.str[:4].astype(int)) | set(e_h.index.year))
    escritos = 0
    for ano in anos:
        mercado = {d: [r(x["consumo_mwh"], 1), r(x["erc_total_eur"], 0)] + [r(x[c], 0) for c in comp]
                   for d, x in merc[merc.index.str[:4] == str(ano)].iterrows()}
        dias = {}
        for dia in e_h.index[e_h.index.year == ano]:
            d = {"e": [r(v, 2) for v in e_h.loc[dia]],
                 "m": [r(v, 0) for v in m_h.loc[dia]],
                 "n": int(n_qh.get(dia, 0))}
            if o_h is not None and dia in o_h.index:
                d["o"] = [r(v, 1) for v in o_h.loc[dia]]
            if dia in i_max.index:
                a, b = qh.loc[i_max[dia]], qh.loc[i_min[dia]]
                d["max"] = [r(a["erc_total_eur_mwh"], 2), a["hhmm"]]
                d["min"] = [r(b["erc_total_eur_mwh"], 2), b["hhmm"]]
            if dia in perdas and np.isfinite(perdas[dia]):
                d["p"] = r(perdas[dia], 4)
            if dia in per:
                d["c"] = [x for k in ORDEM_C for x in per[dia].get(k, (None, 0))]
            dias[dia.strftime("%Y-%m-%d")] = d
        if escrever_json(os.path.join(pasta, f"{ano}.json"),
                         {"ano": ano, "componentes": [c for c, _ in COMPONENTES],
                          "ordem_c": ORDEM_C, "mercado": mercado, "dias": dias}):
            escritos += 1

    meta = {"anos": anos, "de": e_h.index.min().strftime("%Y-%m-%d"),
            "ate": e_h.index.max().strftime("%Y-%m-%d"),
            "mercado_ate": merc.index.max(),
            "periodos_desde": min(per).strftime("%Y-%m-%d") if per else None}
    meta["ultima_data"] = meta["ate"]
    escrever_json(os.path.join(pasta, "metadata.json"), meta)
    print(f"   erc_diario/: {len(anos)} anos, {escritos} reescritos; dias de {meta['de']} a {meta['ate']}")
    return meta


# ============================================================
# Ficheiros por mês (OMIE PT + ERC + perdas de um dia escolhido)
# ============================================================

def ficheiros_dias(pasta_isp, ciclos, omie, pasta_saida):
    if omie is None or omie.empty:
        print("⚠️ Sem OMIE: ficheiros por dia não gerados.")
        return None
    erc = erc_previsao.ler_ficheiros(pasta_isp)
    dados = erc_previsao.preparar(erc)
    valores, contagem = erc_previsao.erc_instantes(dados, omie["inicio_utc"])
    ultimo_real = dados["reais"].index.max()
    t = omie.assign(erc=valores, previsto=(omie["inicio_utc"] > ultimo_real).values)
    if ciclos is not None:
        t = t.merge(ciclos[["inicio_utc", "Perdas"]], on="inicio_utc", how="left")
    else:
        t["Perdas"] = np.nan
    t["hhmm"] = t["inicio_utc"].dt.tz_convert(FUSO).dt.strftime("%H:%M")

    escritos = 0
    for mes, g in t.groupby(t["data"].dt.strftime("%Y-%m")):
        dias = {}
        for dia, gd in g.groupby("data"):
            e = {"omie": [r(v, 2) for v in gd["omie"]], "erc": [r(v, 2) for v in gd["erc"]]}
            if len(gd) != 96:                    # dias de mudança de hora (92 ou 100)
                e["slots"] = list(gd["hhmm"])
            if gd["Perdas"].notna().any():
                e["perdas"] = [r(v, 4) for v in gd["Perdas"]]
            if gd["previsto"].any():             # quantos quartos de hora são previsão
                e["erc_previsto"] = int(gd["previsto"].sum())
            dias[dia.strftime("%Y-%m-%d")] = e
        if escrever_json(os.path.join(pasta_saida, f"{mes}.json"), {"mes": mes, "dias": dias}):
            escritos += 1
    meta = {
        "de": t["data"].min().strftime("%Y-%m-%d"),
        "ate": t["data"].max().strftime("%Y-%m-%d"),
        "erc_real_ate": ultimo_real.tz_convert(FUSO).strftime("%Y-%m-%d"),
        "perdas_desde": (t.loc[t["Perdas"].notna(), "data"].min().strftime("%Y-%m-%d")
                         if t["Perdas"].notna().any() else None),
    }
    meta["ultima_data"] = meta["ate"]
    escrever_json(os.path.join(pasta_saida, "metadata.json"), meta)
    print(f"   erc_omie/: {t['data'].dt.strftime('%Y-%m').nunique()} meses, {escritos} reescritos; "
          f"OMIE PT de {meta['de']} a {meta['ate']}, ERC real até {meta['erc_real_ate']} "
          f"({contagem['curto'] + contagem['longo']} quartos de hora previstos)")
    return meta


# ============================================================
# Previsão própria a 14 dias + erro medido
# ============================================================

def bloco_previsao(df, pasta_isp):
    erc = erc_previsao.ler_ficheiros(pasta_isp)
    if erc is None:
        return None
    dados = erc_previsao.preparar(erc)
    ultimo = dados["ultimo"]

    # Real por dia e slot, sem corte (é o que o comercializador fatura)
    real = df.groupby(["dia_local", "hhmm"])["erc_total_eur_mwh"].mean().unstack()

    dias = []
    if not dados["desatualizado"]:
        for h in range(1, DIAS_PREVISAO + 1):
            d = ultimo + pd.Timedelta(days=h)
            prev, _ = erc_previsao.prever_dia(dados, d)
            if prev is None:
                continue
            prev = prev.sort_index()
            dias.append({"dia": d.strftime("%Y-%m-%d"), "h": h,
                         "slots": list(prev.index), "qh": [r(v, 2) for v in prev.values]})

    reais = []
    for d in sorted(real.index)[-DIAS_REAIS_CONTEXTO:]:
        linha = real.loc[d].dropna()
        reais.append({"dia": d.strftime("%Y-%m-%d"), "slots": list(linha.index),
                      "qh": [r(v, 2) for v in linha.values]})

    # Erro por horizonte, simulando o que haveria disponível em cada origem
    erro_qh = {h: [] for h in range(1, DIAS_PREVISAO + 1)}
    erro_dia = {h: [] for h in range(1, DIAS_PREVISAO + 1)}
    origem = ultimo - pd.Timedelta(days=DIAS_PREVISAO)
    limite = ultimo - pd.Timedelta(days=JANELA_ERRO_DIAS)
    n = 0
    while origem >= limite:
        dd = erc_previsao.preparar(erc, hoje=origem, ate=origem)
        if dd is not None:
            n += 1
            for h in range(1, DIAS_PREVISAO + 1):
                d = origem + pd.Timedelta(days=h)
                if d not in real.index:
                    continue
                prev, _ = erc_previsao.prever_dia(dd, d)
                if prev is None:
                    continue
                par = pd.concat([prev.rename("p"), real.loc[d].rename("r")], axis=1).dropna()
                if len(par) < 80:
                    continue
                erro_qh[h].append(float((par["p"] - par["r"]).abs().mean()))
                erro_dia[h].append(float(abs(par["p"].mean() - par["r"].mean())))
        origem -= pd.Timedelta(days=PASSO_ORIGENS_DIAS)

    return {
        "base": ultimo.strftime("%Y-%m-%d"),
        "desatualizado": bool(dados["desatualizado"]),
        "dias": dias,
        "reais": reais,
        "erro": {
            "origens": n,
            "qh": [r(np.mean(erro_qh[h]), 2) if erro_qh[h] else None for h in range(1, DIAS_PREVISAO + 1)],
            "dia": [r(np.mean(erro_dia[h]), 2) if erro_dia[h] else None for h in range(1, DIAS_PREVISAO + 1)],
        },
    }


def main():
    p = argparse.ArgumentParser(description="Dados do ERC da REN para a página do site.")
    p.add_argument("--isp", default=PASTA_ISP)
    p.add_argument("--ciclos", default=FICHEIRO_CICLOS)
    p.add_argument("--saida", default=SAIDA)
    p.add_argument("--pasta-diario", default=PASTA_DIARIO)
    p.add_argument("--pasta-dias", default=PASTA_DIAS)
    args = p.parse_args()

    df = ler_isp(args.isp)
    ciclos = ler_ciclos(args.ciclos)
    omie = ler_omie_pt(df["data_iso"].min())
    print(f"⚖️ ERC: {len(df)} quartos de hora, {df['data_iso'].min()} → {df['data_iso'].max()}")

    resumo = {
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "REN — SIME, Encargos de Regulação Imputados ao Consumo (ERC)",
        "unidade": "EUR/MWh",
        "primeira_data": df["data_iso"].min(),
        "ultima_data": df["data_iso"].max(),
        "componentes": [{"id": c, "nome": n} for c, n in COMPONENTES],
        "previsao": bloco_previsao(df, args.isp),
        "diario": ficheiros_diarios(df, ciclos, omie, args.pasta_diario),
        "dias_omie": ficheiros_dias(args.isp, ciclos, omie, args.pasta_dias),
    }

    # Sem alterações além do gerado_em → não reescrever (nada de commits vazios)
    if not escrever_json(args.saida, resumo, ignorar=("gerado_em",)):
        print(f"   {os.path.basename(args.saida)}: sem alterações.")
        return
    print(f"✅ {args.saida} ({os.path.getsize(args.saida) / 1024:.1f} KB) — último dia {resumo['ultima_data']}")


if __name__ == "__main__":
    main()
