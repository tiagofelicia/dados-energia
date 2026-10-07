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
  solar        por mês e classe de quota solar no quarto de hora (produção
               solar ÷ consumo; limites em "classes"): [classe, quartos de
               hora, MWh, €, € de cada componente…] — o ERC de cada classe
  tipos        anos e intervalo de erc_tipo/, os grupos e, por código,
               [grupo, tipo, subtipo, unidade]

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
             s    quota solar de cada hora (%, inteiro): produção solar ÷ consumo, dos
                  dados de produção da REN (data/producao)
           p e c só existem desde 2025 (a folha do simulador começa aí); s só
           com a produção reportada (balanço fechado).

erc_omie/AAAA-MM.json (~50 KB por mês), que a página só descarrega quando se
escolhe um dia: por quarto de hora, o OMIE PT, o ERC (o real ou, nos dias ainda
sem valor publicado, a previsão própria; erc_previsto = quantos o são) e o fator
de perdas (desde 2025). 'slots' só nos dias de mudança de hora.

erc_tipo/AAAA.json (~150 KB por ano), para o bloco "De onde vem o ERC": por dia
de MERCADO, o valor (€, positivo = custo, negativo = receita) e a quantidade
(MWh ou MW, como na vista Tipo da REN) de cada código, numa lista plana
[quartos de hora do dia, i, €, quantidade, i, €, quantidade, …], em que i é a
posição do código em "codigos" (no cabeçalho do ficheiro). Os nomes, as
unidades e o grupo de cada código vão no resumo (tipos.codigos), e os grupos,
pela ordem do gráfico, em tipos.grupos.

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
import gerar_records_producao as producao

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_ISP = os.path.join(ROOT_DIR, "data", "erc", "isp")
FICHEIRO_CICLOS = os.path.join(ROOT_DIR, "data", "simuladores", "simulador-tarifarios-eletricidade",
                               "csv", "OMIE_PERDAS_CICLOS.csv")
PASTA_AGREGADOS = os.path.join(ROOT_DIR, "data", "agregados")
SAIDA = os.path.join(PASTA_AGREGADOS, "erc_resumo.json")
PASTA_DIARIO = os.path.join(PASTA_AGREGADOS, "erc_diario")
PASTA_DIAS = os.path.join(PASTA_AGREGADOS, "erc_omie")
PASTA_TIPO = os.path.join(ROOT_DIR, "data", "erc", "tipo")
FICHEIRO_CODIGOS = os.path.join(ROOT_DIR, "data", "erc", "erc_tipo_codigos.csv")
PASTA_TIPO_SAIDA = os.path.join(PASTA_AGREGADOS, "erc_tipo")

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
# Classes de quota solar (produção solar ÷ consumo, %) de um quarto de hora:
# limites inferiores; a 1.ª ("noite") é a de quase nenhum sol
CLASSES_SOLAR = [0, 0.5, 20, 40, 60]
ORDEM_C = [f"{c}.{p}" for c, ps in CICLOS.items() for p in ps]

# Grupos dos códigos da vista Tipo, pela ordem do gráfico em cascata: (id, nome,
# componente do isp onde a REN os soma, prefixos dos códigos). O primeiro prefixo
# que serve decide; o que não servir a nenhum vai para "outros_servicos".
GRUPOS_TIPO = [
    ("rt_pdbf_restricao", "Restrições técnicas no PDBF — restrição", "rt_pdbf", ("RTPDBFF1",)),
    ("rt_pdbf_reequilibrio", "Restrições técnicas no PDBF — reequilíbrio", "rt_pdbf", ("RTPDBFF2",)),
    ("rt_pdbf", "Restrições técnicas no PDBF (antes da separação)", "rt_pdbf", ("RTPDBF",)),
    ("rt_pdvd", "Restrições técnicas após o PDVD", "rt_pdvd", ("RTPDVD",)),
    ("rt_phf", "Restrições técnicas após o PHF", "rt_phf", ("RTPF",)),
    ("banda_afrr", "Banda de reserva aFRR", "banda_afrr", ("BAFRR",)),
    ("banda_mfrr", "Banda de reserva mFRR", "banda_mfrr", ("BMFRR",)),
    ("energia_afrr", "Energia de reserva aFRR", "outros", ("EAFRR",)),
    ("energia_mfrr", "Energia de reserva mFRR", "outros", ("EMFRR",)),
    ("mfrr_transitorio", "Produto transitório de mFRR", "outros", ("RTMFRRT",)),
    ("energia_rr", "Energia de reserva RR", "outros", ("TERRE",)),
    ("desvios", "Desvios", "outros", ("DSVEV",)),
    ("igcc", "Coordenação de desvios (IGCC)", "outros", ("IGCC",)),
    ("incumprimentos", "Incumprimentos de instruções de despacho", "outros", ("IDINC",)),
    ("outros_servicos", "Outros serviços e acertos", "outros", ()),
]


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


def ler_producao(primeiro_dia):
    """Solar e consumo (MW médios) de cada quarto de hora, com o início em hora
    de Portugal (sem fuso). Só os intervalos com o balanço fechado: no dia em
    curso, os que ainda não foram reportados vêm com as fontes a zero."""
    ano0 = pd.Timestamp(primeiro_dia).year
    fs = [f for f in sorted(glob.glob(producao.HISTORICO_GLOB)) if int(f[-8:-4]) >= ano0]
    if os.path.exists(producao.ATUAIS_PATH):
        fs.append(producao.ATUAIS_PATH)
    partes = []
    for f in fs:
        x = producao.ler_csv_producao(f)
        if not {"Solar", "Consumo", "dia", "intervalo"} <= set(x.columns):
            continue
        ok = producao._desvio_balanco(x) <= producao.TOLERANCIA_BALANCO_MW
        x = x[ok]
        partes.append(pd.DataFrame({
            "t": pd.to_datetime(x["dia"] + " " + x["intervalo"].str[1:6], format="%d/%m/%Y %H:%M", errors="coerce"),
            "solar": pd.to_numeric(x["Solar"], errors="coerce"),
            "cons_ren": pd.to_numeric(x["Consumo"], errors="coerce")}))
    if not partes:
        return None
    pr = pd.concat(partes, ignore_index=True).dropna()
    pr = pr[(pr["t"] >= pd.Timestamp(primeiro_dia)) & (pr["cons_ren"] > 0)]
    # O mesmo instante em dois ficheiros (ou a hora repetida de outubro): o primeiro
    return pr.drop_duplicates("t", keep="first")


def juntar_solar(df, prod):
    """Os quartos de hora do isp com a produção solar e o consumo da REN."""
    if prod is None or not len(prod):
        return None
    local = df["inicio_utc"].dt.tz_convert(FUSO).dt.tz_localize(None)
    j = df.assign(t=local).merge(prod, on="t")
    j["quota_solar"] = 100 * j["solar"] / j["cons_ren"]
    return j


def tabela_solar(df, prod):
    """Por mês (hora de Portugal) e classe de quota solar: quartos de hora, MWh,
    € e € de cada componente."""
    j = juntar_solar(df, prod)
    if j is None:
        return None
    comp = [f"{c}_eur" for c, _ in COMPONENTES]
    j["classe"] = (np.searchsorted(CLASSES_SOLAR, j["quota_solar"], side="right") - 1).clip(0)
    j["mes"] = j["dia_local"].dt.strftime("%Y-%m")
    g = j.groupby(["mes", "classe"]).agg(n=("consumo_mwh", "size"), mwh=("consumo_mwh", "sum"),
                                          eur=("erc_total_eur", "sum"), **{c: (c, "sum") for c in comp})
    meses = {}
    for (m, k), x in g.iterrows():
        meses.setdefault(m, []).append([int(k), int(x["n"]), r(x["mwh"], 0), r(x["eur"], 0)] + [r(x[c], 0) for c in comp])
    return {"classes": CLASSES_SOLAR, "componentes": [c for c, _ in COMPONENTES],
            "de": j["dia_local"].min().strftime("%Y-%m-%d"), "ate": j["dia_local"].max().strftime("%Y-%m-%d"),
            "meses": meses}


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

def ficheiros_diarios(df, ciclos, omie, prod, pasta):
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

    # Quota solar de cada hora (produção solar ÷ consumo da REN)
    s_h = None
    j = juntar_solar(df, prod)
    if j is not None:
        hh = j.groupby(["dia_local", "hora"])[["solar", "cons_ren"]].sum()
        s_h = (100 * hh["solar"] / hh["cons_ren"]).unstack().reindex(columns=range(24))

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
            if s_h is not None and dia in s_h.index and s_h.loc[dia].notna().any():
                d["s"] = [None if (v is None or not np.isfinite(v)) else int(round(v)) for v in s_h.loc[dia]]
            dias[dia.strftime("%Y-%m-%d")] = d
        if escrever_json(os.path.join(pasta, f"{ano}.json"),
                         {"ano": ano, "componentes": [c for c, _ in COMPONENTES],
                          "ordem_c": ORDEM_C, "mercado": mercado, "dias": dias}):
            escritos += 1

    meta = {"anos": anos, "de": e_h.index.min().strftime("%Y-%m-%d"),
            "ate": e_h.index.max().strftime("%Y-%m-%d"),
            "mercado_ate": merc.index.max(),
            "periodos_desde": min(per).strftime("%Y-%m-%d") if per else None,
            "solar_ate": s_h.dropna(how="all").index.max().strftime("%Y-%m-%d") if s_h is not None else None}
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
# Ficheiros por ano da vista Tipo (de onde vem o ERC)
# ============================================================

def grupo_do_codigo(codigo):
    for gid, _, _, prefixos in GRUPOS_TIPO:
        if any(codigo.startswith(p) for p in prefixos):
            return gid
    return "outros_servicos"


def ficheiros_tipo(pasta_tipo, ficheiro_codigos, df, pasta_saida):
    """Por dia de mercado, o valor (€) e a quantidade de cada código da vista
    Tipo da REN. A soma dos códigos de um dia dá a soma das componentes do isp
    (o total publicado fica abaixo dela nalguns períodos, na fonte)."""
    ficheiros = sorted(glob.glob(os.path.join(pasta_tipo, "erc_tipo_????-??.csv")))
    if not ficheiros:
        print(f"⚠️ Sem ficheiros em {pasta_tipo}: detalhe por tipo não gerado.")
        return None
    t = pd.concat([pd.read_csv(f, encoding="utf-8-sig", usecols=["data_iso", "codigo", "quantidade", "valor_eur"],
                               dtype={"codigo": str}) for f in ficheiros], ignore_index=True)
    t["valor_eur"] = pd.to_numeric(t["valor_eur"], errors="coerce").fillna(0)
    t["quantidade"] = pd.to_numeric(t["quantidade"], errors="coerce").fillna(0).abs()
    g = t.groupby(["data_iso", "codigo"])[["valor_eur", "quantidade"]].sum()
    n_qh = df.groupby("data_iso").size()           # quartos de hora de cada dia de mercado

    nomes = {}
    if os.path.exists(ficheiro_codigos):
        cod = pd.read_csv(ficheiro_codigos, encoding="utf-8-sig", dtype=str).fillna("")
        nomes = {x["codigo"]: (x["tipo"], x["subtipo"], x["unidade"]) for _, x in cod.iterrows()}
    presentes = sorted(g.index.get_level_values("codigo").unique())
    codigos = {c: [grupo_do_codigo(c)] + list(nomes.get(c, (c, "", ""))) for c in presentes}

    dias_todos = g.index.get_level_values("data_iso").unique()
    anos = sorted({int(d[:4]) for d in dias_todos})
    escritos = 0
    for ano in anos:
        gg = g[g.index.get_level_values("data_iso").str[:4] == str(ano)]
        lista = sorted(gg.index.get_level_values("codigo").unique())
        idx = {c: i for i, c in enumerate(lista)}
        dias = {}
        for dia, gd in gg.groupby(level="data_iso"):
            linha = [int(n_qh.get(dia, 0))]
            for (_, c), x in gd.iterrows():
                if x["valor_eur"] == 0 and x["quantidade"] == 0:
                    continue
                linha += [idx[c], r(x["valor_eur"], 0), r(x["quantidade"], 1)]
            dias[dia] = linha
        if escrever_json(os.path.join(pasta_saida, f"{ano}.json"),
                         {"ano": ano, "codigos": lista, "dias": dias}):
            escritos += 1

    meta = {"anos": anos, "de": min(dias_todos), "ate": max(dias_todos)}
    meta["ultima_data"] = meta["ate"]
    escrever_json(os.path.join(pasta_saida, "metadata.json"), meta)
    print(f"   erc_tipo/: {len(anos)} anos, {escritos} reescritos; {len(presentes)} códigos, "
          f"dias de {meta['de']} a {meta['ate']}")
    return dict(meta, grupos=[{"id": gid, "nome": nome, "comp": comp} for gid, nome, comp, _ in GRUPOS_TIPO],
                codigos=codigos)


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
    p.add_argument("--tipo", default=PASTA_TIPO)
    p.add_argument("--codigos", default=FICHEIRO_CODIGOS)
    p.add_argument("--pasta-tipo", default=PASTA_TIPO_SAIDA)
    args = p.parse_args()

    df = ler_isp(args.isp)
    ciclos = ler_ciclos(args.ciclos)
    omie = ler_omie_pt(df["data_iso"].min())
    prod = ler_producao(df["data_iso"].min())
    print(f"⚖️ ERC: {len(df)} quartos de hora, {df['data_iso'].min()} → {df['data_iso'].max()}")

    resumo = {
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "REN — SIME, Encargos de Regulação Imputados ao Consumo (ERC)",
        "unidade": "EUR/MWh",
        "primeira_data": df["data_iso"].min(),
        "ultima_data": df["data_iso"].max(),
        "componentes": [{"id": c, "nome": n} for c, n in COMPONENTES],
        "previsao": bloco_previsao(df, args.isp),
        "diario": ficheiros_diarios(df, ciclos, omie, prod, args.pasta_diario),
        "solar": tabela_solar(df, prod),
        "dias_omie": ficheiros_dias(args.isp, ciclos, omie, args.pasta_dias),
        "tipos": ficheiros_tipo(args.tipo, args.codigos, df, args.pasta_tipo),
    }

    # Sem alterações além do gerado_em → não reescrever (nada de commits vazios)
    if not escrever_json(args.saida, resumo, ignorar=("gerado_em",)):
        print(f"   {os.path.basename(args.saida)}: sem alterações.")
        return
    print(f"✅ {args.saida} ({os.path.getsize(args.saida) / 1024:.1f} KB) — último dia {resumo['ultima_data']}")


if __name__ == "__main__":
    main()
