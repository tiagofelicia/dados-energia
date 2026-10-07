#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_quotas_agentes.py
Quotas do consumo por agente de mercado (BRP), para a página /quotas-agentes
do site, em data/agregados/agentes/.

DE ONDE SAI O VOLUME
--------------------
A vista brp dos ERC da REN reparte o ERC de cada período pelos agentes, ao
mesmo €/MWh para todos (verificado: em nenhum período um agente tem o sinal
contrário ao do total, e a soma dos agentes dá o total). Por isso a parte do
ERC de um agente num período é a sua parte do consumo do mercado:

    volume = (ERC do agente ÷ ERC total do período) × consumo do período

Nos períodos em que o ERC total é quase zero (|€/MWh| < LIMIAR_EUR_MWH) a conta
não é fiável; aí usa-se a quota do agente nesse dia de mercado (calculada nos
outros períodos). O brp foi horário até 30/09/2025 (coluna minutos = 60): a
hora k do dia de mercado são os períodos 4k-3 a 4k do isp.

Um agente (BRP, o responsável pelos desvios de uma carteira) não é o mesmo que
uma marca comercial: pode representar vários comercializadores. As quotas são
do consumo abastecido em mercado, incluindo o do comercializador de último
recurso.

O QUE ESCREVE
-------------
agentes/metadata.json
  agentes   [{codigo, nome, curto, de, ate, mwh, eur, cor}] — nome oficial da
            REN (data/erc/erc_agentes.csv; sem ele, o código), nome curto para
            os gráficos, primeiro e último dia com consumo, totais da série e
            cor (1–7 para os 7 maiores da série, para a cor seguir o agente)
  anos, de, ate, ultima_data, horario_ate (último dia do brp horário)

agentes/AAAA.json (~120 KB por ano)
  agentes   os códigos usados no ficheiro (i = posição)
  dias      por dia de mercado: [MWh do mercado, € do mercado, i, MWh, €, …]
  perfis    por mês: {"_": [MWh do mercado em cada hora], "i": [MWh do agente
            em cada hora]} — hora de Portugal

USO
---
  python gerar_quotas_agentes.py
"""

import argparse
import glob
import os
import re

import numpy as np
import pandas as pd

from gerar_erc_resumo import FUSO, PASTA_AGREGADOS, PASTA_ISP, escrever_json, r

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA_BRP = os.path.join(ROOT_DIR, "data", "erc", "brp")
FICHEIRO_AGENTES = os.path.join(ROOT_DIR, "data", "erc", "erc_agentes.csv")
PASTA_SAIDA = os.path.join(PASTA_AGREGADOS, "agentes")
LIMIAR_EUR_MWH = 0.5
N_CORES = 7


def nome_curto(nome, codigo):
    if not nome:
        return codigo
    c = nome.split(",")[0].split(" - ")[0].strip()
    c = re.sub(r"\s+(Unipessoal|Unip\.)$", "", c).strip()
    return c or codigo


def ler_isp(pasta):
    f = sorted(glob.glob(os.path.join(pasta, "erc_isp_????-??.csv")))
    i = pd.concat([pd.read_csv(x, encoding="utf-8-sig",
                               usecols=["data_iso", "periodo", "data_utc", "consumo_mwh", "erc_total_eur"])
                   for x in f], ignore_index=True)
    i["consumo_mwh"] = pd.to_numeric(i["consumo_mwh"], errors="coerce")
    i["erc_total_eur"] = pd.to_numeric(i["erc_total_eur"], errors="coerce")
    i = i.dropna(subset=["consumo_mwh", "erc_total_eur"])
    local = pd.to_datetime(i["data_utc"], format="%Y-%m-%d %H:%M", utc=True).dt.tz_convert(FUSO)
    i["mes_local"] = local.dt.strftime("%Y-%m")
    i["hora"] = local.dt.hour
    return i


def ler_brp(pasta):
    f = sorted(glob.glob(os.path.join(pasta, "erc_brp_????-??.csv")))
    b = pd.concat([pd.read_csv(x, encoding="utf-8-sig", usecols=["data_iso", "periodo", "minutos", "brp", "erc_eur"],
                               dtype={"brp": str}) for x in f], ignore_index=True)
    b["brp"] = b["brp"].str.strip()
    b["erc_eur"] = pd.to_numeric(b["erc_eur"], errors="coerce").fillna(0)
    return b


def volumes(isp, brp):
    """Volume (MWh) e ERC (€) de cada agente em cada período (15 min) do isp."""
    isp = isp.copy()
    isp["hora_merc"] = (isp["periodo"] - 1) // 4 + 1
    isp_k = isp[["data_iso", "periodo", "hora_merc", "consumo_mwh", "erc_total_eur", "mes_local", "hora"]]

    # Cada linha do brp no seu período de liquidação: o próprio (15 min) ou a hora (60)
    q = isp[["data_iso", "periodo", "consumo_mwh", "erc_total_eur"]].rename(columns={"periodo": "p"})
    h = (isp.groupby(["data_iso", "hora_merc"])[["consumo_mwh", "erc_total_eur"]].sum()
            .reset_index().rename(columns={"hora_merc": "p"}))
    b15 = brp[brp["minutos"] == 15].rename(columns={"periodo": "p"})
    b60 = brp[brp["minutos"] == 60].rename(columns={"periodo": "p"})
    liq = pd.concat([b15.merge(q, on=["data_iso", "p"]).assign(g="Q"),
                     b60.merge(h, on=["data_iso", "p"]).assign(g="H")], ignore_index=True)
    preco = liq["erc_total_eur"] / liq["consumo_mwh"]
    liq["valido"] = (liq["consumo_mwh"] > 0) & (preco.abs() >= LIMIAR_EUR_MWH)
    liq["vol"] = liq["erc_eur"] / liq["erc_total_eur"] * liq["consumo_mwh"]
    neg = int((liq["valido"] & (liq["vol"] < 0)).sum())

    # Quota de cada agente em cada dia, só com os períodos válidos
    vv = liq[liq["valido"]]
    tv = vv.drop_duplicates(["data_iso", "g", "p"]).groupby("data_iso")["consumo_mwh"].sum().rename("tv")
    qd = vv.groupby(["data_iso", "brp"])["vol"].sum().reset_index().merge(tv, on="data_iso")
    qd["q"] = qd["vol"] / qd["tv"]
    qd = qd[["data_iso", "brp", "q"]]

    # Válidos: para os quartos de hora do isp (a hora do brp horário repartida
    # pelos seus quartos de hora na proporção do consumo)
    partes = []
    for g, chave in (("Q", "periodo"), ("H", "hora_merc")):
        x = vv[vv["g"] == g]
        if x.empty:
            continue
        lado = isp_k.merge(x[["data_iso", "p", "brp", "erc_eur", "vol"]].rename(columns={"p": chave}),
                           on=["data_iso", chave])
        peso = lado["consumo_mwh"] / lado.groupby(["data_iso", chave, "brp"])["consumo_mwh"].transform("sum")
        lado["vol_p"], lado["eur_p"] = lado["vol"] * peso, lado["erc_eur"] * peso
        partes.append(lado[["data_iso", "periodo", "brp", "vol_p", "eur_p", "mes_local", "hora"]])
    v = pd.concat(partes, ignore_index=True)

    # Os outros quartos de hora (ERC ≈ 0 ou sem linhas): quota do dia × consumo,
    # com o ERC que os agentes lá tiverem de facto
    cobertos = v[["data_iso", "periodo"]].drop_duplicates().assign(c=1)
    resto = isp_k.merge(cobertos, on=["data_iso", "periodo"], how="left")
    resto = resto[resto["c"].isna()].drop(columns="c")
    if len(resto):
        base = resto.merge(qd, on="data_iso")
        base["vol_p"] = base["q"] * base["consumo_mwh"]
        inv = liq[~liq["valido"]]
        eur = []
        for g, chave in (("Q", "periodo"), ("H", "hora_merc")):
            x = inv[inv["g"] == g]
            if x.empty:
                continue
            lado = isp_k.merge(x[["data_iso", "p", "brp", "erc_eur"]].rename(columns={"p": chave}),
                               on=["data_iso", chave])
            peso = lado["consumo_mwh"] / lado.groupby(["data_iso", chave, "brp"])["consumo_mwh"].transform("sum")
            lado["eur_p"] = lado["erc_eur"] * peso
            eur.append(lado[["data_iso", "periodo", "brp", "eur_p"]])
        if eur:
            base = base.merge(pd.concat(eur), on=["data_iso", "periodo", "brp"], how="left")
        base["eur_p"] = base.get("eur_p", 0)
        base["eur_p"] = base["eur_p"].fillna(0)
        v = pd.concat([v, base[["data_iso", "periodo", "brp", "vol_p", "eur_p", "mes_local", "hora"]]],
                      ignore_index=True)
    return v, neg, len(resto)


def main():
    p = argparse.ArgumentParser(description="Quotas do consumo por agente (BRP) para a página do site.")
    p.add_argument("--isp", default=PASTA_ISP)
    p.add_argument("--brp", default=PASTA_BRP)
    p.add_argument("--agentes", default=FICHEIRO_AGENTES)
    p.add_argument("--saida", default=PASTA_SAIDA)
    args = p.parse_args()

    isp = ler_isp(args.isp)
    brp = ler_brp(args.brp)
    v, neg, n_inval = volumes(isp, brp)
    print(f"👥 Agentes: {brp['brp'].nunique()} · {len(isp)} períodos · {n_inval} períodos com ERC ≈ 0 "
          f"(quota do dia) · {neg} volumes negativos")

    nomes = {}
    if os.path.exists(args.agentes):
        a = pd.read_csv(args.agentes, encoding="utf-8-sig", dtype=str).fillna("")
        nomes = dict(zip(a["codigo"].str.strip(), a["nome"].str.strip()))
    else:
        print(f"⚠️ Sem {args.agentes}: os agentes ficam só com o código.")

    mercado = isp.groupby("data_iso")[["consumo_mwh", "erc_total_eur"]].sum()
    dia = v.groupby(["data_iso", "brp"])[["vol_p", "eur_p"]].sum()
    perf_ag = v.groupby(["mes_local", "brp", "hora"])["vol_p"].sum()
    perf_nac = isp.groupby(["mes_local", "hora"])["consumo_mwh"].sum()

    tot = dia.groupby(level="brp").sum()
    ativos = dia[dia["vol_p"] > 0].reset_index().groupby("brp")["data_iso"].agg(["min", "max"])
    ordem = tot.sort_values("vol_p", ascending=False).index.tolist()
    curtos = {c: nome_curto(nomes.get(c, ""), c) for c in ordem}
    repetidos = {x for x in curtos.values() if list(curtos.values()).count(x) > 1}
    curtos = {c: (f"{n} ({c})" if n in repetidos else n) for c, n in curtos.items()}

    anos = sorted({int(d[:4]) for d in mercado.index})
    escritos = 0
    for ano in anos:
        dd = dia[dia.index.get_level_values("data_iso").str[:4] == str(ano)]
        lista = sorted(dd.index.get_level_values("brp").unique(), key=ordem.index)
        idx = {c: i for i, c in enumerate(lista)}
        dias = {}
        for d, m in mercado[mercado.index.str[:4] == str(ano)].iterrows():
            linha = [r(m["consumo_mwh"], 1), r(m["erc_total_eur"], 0)]
            if d in dd.index.get_level_values("data_iso"):
                for (_, c), x in dd.loc[[d]].iterrows():
                    if abs(x["vol_p"]) < 0.05 and abs(x["eur_p"]) < 0.5:
                        continue
                    linha += [idx[c], r(x["vol_p"], 1), r(x["eur_p"], 0)]
            dias[d] = linha
        perfis = {}
        for mes in sorted({m for m in perf_nac.index.get_level_values(0) if m.startswith(str(ano))}):
            pm = {"_": [r(x, 0) for x in perf_nac.loc[mes].reindex(range(24), fill_value=0)]}
            if mes in perf_ag.index.get_level_values(0):
                for c, gc in perf_ag.loc[mes].groupby(level="brp"):
                    s = gc.droplevel(0).reindex(range(24), fill_value=0)
                    if c in idx and s.abs().sum() >= 1:
                        pm[str(idx[c])] = [r(x, 0) for x in s]
            perfis[mes] = pm
        if escrever_json(os.path.join(args.saida, f"{ano}.json"),
                         {"ano": ano, "agentes": lista, "dias": dias, "perfis": perfis}):
            escritos += 1

    agentes = [{"codigo": c, "nome": nomes.get(c) or c, "curto": curtos[c],
                "de": ativos.loc[c, "min"] if c in ativos.index else None,
                "ate": ativos.loc[c, "max"] if c in ativos.index else None,
                "mwh": r(tot.loc[c, "vol_p"], 0), "eur": r(tot.loc[c, "eur_p"], 0),
                "cor": (k + 1) if k < N_CORES else None}
               for k, c in enumerate(ordem)]
    horario = brp.loc[brp["minutos"] == 60, "data_iso"]
    meta = {"agentes": agentes, "anos": anos, "de": mercado.index.min(), "ate": mercado.index.max(),
            "horario_ate": horario.max() if len(horario) else None}
    meta["ultima_data"] = meta["ate"]
    escrever_json(os.path.join(args.saida, "metadata.json"), meta)
    tam = sum(os.path.getsize(os.path.join(args.saida, f"{a}.json")) for a in anos) / 1024
    print(f"✅ agentes/: {len(agentes)} agentes, {len(anos)} anos, {escritos} reescritos ({tam:.0f} KB); "
          f"dias de {meta['de']} a {meta['ate']}")


if __name__ == "__main__":
    main()
