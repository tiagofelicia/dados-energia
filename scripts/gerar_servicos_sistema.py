#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_servicos_sistema.py
Prepara os dados por hora da página /servicos-sistema do site (reservas,
desvios, restrições técnicas e redução de geração), em data/agregados/.

PORQUÊ
------
A página deixa escolher o período e mostra, para cada serviço, a evolução por
dia ou mês e o perfil por hora do dia. A evolução sai de erc_tipo/AAAA.json (já
gerado pelo gerar_erc_resumo.py, por dia de mercado); o perfil por hora precisa
de cada hora de cada dia, que só está nos CSV de data/erc/tipo/ (~70 MB, por
quarto de hora). Este script junta-os por hora.

O QUE ESCREVE
-------------
servicos_sistema/metadata.json (~25 KB)
  indicadores   [{id, nome, codigos, unidade}] — os grupos de códigos da vista
                Tipo que a página usa (também para a evolução, a partir de
                erc_tipo/); a unidade é a da REN (MWh ou MW)
  omie          {dia: OMIE PT médio do dia} (hora de Portugal, só dias reais)
  meses, de, ate, ultima_data

servicos_sistema/AAAA-MM.json (~110 KB por mês; a página só os descarrega na
vista "Por hora"), por dia em hora de Portugal:
  o    OMIE PT médio de cada hora (0–23), só dias reais
  q    {indicador: [24 quantidades]} — MWh (soma da hora) ou, nas bandas, a
       soma dos MW dos quartos de hora da hora
  e    {indicador: [24 valores em €]} — positivo = custo, negativo = receita
  n    quartos de hora de cada hora, só nos dias de mudança de hora
  Um indicador sem nada no dia não aparece; os números redondos vão sem ".0".

Nenhum ficheiro é reescrito se o conteúdo não mudou.

USO
---
  python gerar_servicos_sistema.py
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

from gerar_erc_resumo import FUSO, PASTA_AGREGADOS, PASTA_ISP, PASTA_TIPO, escrever_json, ler_omie_pt, r

PASTA_SAIDA = os.path.join(PASTA_AGREGADOS, "servicos_sistema")

# Indicadores: (id, nome, códigos da vista Tipo, unidade)
INDICADORES = [
    ("curt", "Redução de geração (curtailment)", ["CURTLMENRG"], "MWh"),
    ("dexc", "Desvios em excesso", ["DSVEVEXC"], "MWh"),
    ("ddef", "Desvios em falta", ["DSVEVDEF"], "MWh"),
    ("asub", "Energia de reserva aFRR a subir", ["EAFRRSUB"], "MWh"),
    ("ades", "Energia de reserva aFRR a descer", ["EAFRRDES"], "MWh"),
    ("msub", "Energia de reserva mFRR a subir", ["EMFRRENSASUB", "EMFRRENDA1SUB", "EMFRRENDA2SUB"], "MWh"),
    ("mdes", "Energia de reserva mFRR a descer", ["EMFRRENSADES", "EMFRRENDA1DES", "EMFRRENDA2DES"], "MWh"),
    ("tsub", "Produto transitório de mFRR a subir", ["RTMFRRTSASUB", "RTMFRRTDA1SUB", "RTMFRRTDA2SUB"], "MWh"),
    ("tdes", "Produto transitório de mFRR a descer", ["RTMFRRTSADES", "RTMFRRTDA1DES", "RTMFRRTDA2DES"], "MWh"),
    ("rsub", "Energia de reserva RR a subir", ["TERREENRGSUB"], "MWh"),
    ("rdes", "Energia de reserva RR a descer", ["TERREENRGDES"], "MWh"),
    ("rt1", "Restrições técnicas no PDBF — restrição", ["RTPDBFF1RST"], "MWh"),
    ("rt2", "Restrições técnicas no PDBF — reequilíbrio", ["RTPDBFF2RST"], "MWh"),
    ("rt0", "Restrições técnicas no PDBF (antes da separação)", ["RTPDBFRST"], "MWh"),
    ("rtv", "Restrições técnicas após o PDVD", ["RTPDVDRST"], "MWh"),
    ("rtfs", "Restrições técnicas após o PHF a subir", ["RTPFSASUB", "RTPFDA1SUB", "RTPFDA2SUB"], "MWh"),
    ("rtfd", "Restrições técnicas após o PHF a descer", ["RTPFSADES", "RTPFDA1DES", "RTPFDA2DES"], "MWh"),
    ("bas", "Banda de reserva aFRR a subir", ["BAFRRMRCSUB"], "MW"),
    ("bad", "Banda de reserva aFRR a descer", ["BAFRRMRCDES"], "MW"),
    ("bm", "Banda de reserva mFRR", ["BMFRRANO", "BMFRRTRI", "BMFRRMES"], "MW"),
]


def c(v, casas):
    """Como r(), mas os números redondos vão como inteiros (sem ".0")."""
    x = r(v, casas)
    return int(x) if x is not None and x == int(x) else x


def ler_periodos(pasta_isp):
    """(data_iso, periodo) → hora de Portugal e dia local, a partir do isp."""
    ficheiros = sorted(glob.glob(os.path.join(pasta_isp, "erc_isp_????-??.csv")))
    p = pd.concat([pd.read_csv(f, encoding="utf-8-sig", usecols=["data_iso", "periodo", "data_utc"])
                   for f in ficheiros], ignore_index=True)
    local = pd.to_datetime(p["data_utc"], format="%Y-%m-%d %H:%M", utc=True).dt.tz_convert(FUSO)
    p["dia_local"] = local.dt.strftime("%Y-%m-%d")
    p["hora"] = local.dt.hour
    return p[["data_iso", "periodo", "dia_local", "hora"]].drop_duplicates(["data_iso", "periodo"])


def ler_tipo(pasta_tipo):
    codigo_ind = {c: i for i, _, cods, _ in INDICADORES for c in cods}
    partes = []
    for f in sorted(glob.glob(os.path.join(pasta_tipo, "erc_tipo_????-??.csv"))):
        t = pd.read_csv(f, encoding="utf-8-sig", dtype={"codigo": str})
        t = t[t["codigo"].isin(codigo_ind)]
        partes.append(t)
    t = pd.concat(partes, ignore_index=True)
    t["ind"] = t["codigo"].map(codigo_ind)
    t["valor_eur"] = pd.to_numeric(t["valor_eur"], errors="coerce").fillna(0)
    t["quantidade"] = pd.to_numeric(t["quantidade"], errors="coerce").fillna(0).abs()
    return t


def main():
    p = argparse.ArgumentParser(description="Dados por hora da página Serviços de Sistema.")
    p.add_argument("--isp", default=PASTA_ISP)
    p.add_argument("--tipo", default=PASTA_TIPO)
    p.add_argument("--saida", default=PASTA_SAIDA)
    args = p.parse_args()

    per = ler_periodos(args.isp)
    t = ler_tipo(args.tipo).merge(per, on=["data_iso", "periodo"], how="inner")
    # O 1.º dia local (13/03/2024) só teria a hora das 23:00, do 1.º dia de mercado
    t = t[t["dia_local"] >= per["data_iso"].min()]
    g = t.groupby(["dia_local", "ind", "hora"])[["quantidade", "valor_eur"]].sum()
    n = per[per["dia_local"] >= per["data_iso"].min()].groupby(["dia_local", "hora"]).size()

    omie = ler_omie_pt(per["data_iso"].min())
    o_h = None
    if omie is not None:
        oo = omie.assign(dia_local=omie["data"].dt.strftime("%Y-%m-%d"),
                         hora=omie["inicio_utc"].dt.tz_convert(FUSO).dt.hour)
        o_h = oo.groupby(["dia_local", "hora"])["omie"].mean().unstack().reindex(columns=range(24))

    dias_todos = sorted(per.loc[per["dia_local"] >= per["data_iso"].min(), "dia_local"].unique())
    por_dia = {d: gd for d, gd in g.groupby(level="dia_local")}
    meses = sorted({d[:7] for d in dias_todos})
    escritos = 0
    for mes in meses:
        dias = {}
        for dia in [d for d in dias_todos if d.startswith(mes)]:
            d = {}
            if o_h is not None and dia in o_h.index:
                d["o"] = [c(v, 1) for v in o_h.loc[dia]]
            nn = n.loc[dia].reindex(range(24), fill_value=0) if dia in n.index.get_level_values(0) else None
            if nn is not None and (nn != 4).any():
                d["n"] = [int(x) for x in nn]
            q, e = {}, {}
            if dia in por_dia:
                for ind, gi in por_dia[dia].groupby(level="ind"):
                    s = gi.droplevel([0, 1]).reindex(range(24), fill_value=0)
                    if not (s["quantidade"].abs().sum() or s["valor_eur"].abs().sum()):
                        continue
                    q[ind] = [c(v, 1) for v in s["quantidade"]]
                    e[ind] = [c(v, 0) for v in s["valor_eur"]]
            d["q"], d["e"] = q, e
            dias[dia] = d
        if escrever_json(os.path.join(args.saida, f"{mes}.json"), {"mes": mes, "dias": dias}):
            escritos += 1

    omie_dia = {}
    if o_h is not None:
        omie_dia = {d: c(v, 2) for d, v in o_h.mean(axis=1).items() if d in set(dias_todos) and np.isfinite(v)}
    meta = {"indicadores": [{"id": i, "nome": nome, "codigos": cods, "unidade": un}
                            for i, nome, cods, un in INDICADORES],
            "omie": omie_dia, "meses": meses, "de": dias_todos[0], "ate": dias_todos[-1]}
    meta["ultima_data"] = meta["ate"]
    escrever_json(os.path.join(args.saida, "metadata.json"), meta)
    tam = sum(os.path.getsize(os.path.join(args.saida, f"{m}.json")) for m in meses) / 1024
    print(f"✅ servicos_sistema/: {len(meses)} meses, {escritos} reescritos ({tam:.0f} KB); "
          f"dias de {meta['de']} a {meta['ate']}")


if __name__ == "__main__":
    main()
