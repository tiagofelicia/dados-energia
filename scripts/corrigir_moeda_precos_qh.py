#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corrigir_moeda_precos_qh.py
Converte para euros os preços de RO, PL e BG que ficaram gravados na moeda local.

PORQUÊ
------
Até 17/06/2021 (RO), 19/11/2019 (PL) e 21/01/2022 (BG), a ENTSO-E entregava os
preços destas zonas na moeda local — RON, PLN e BGN — e o
atualizar_mapa_precos_entsoe.py não lia a moeda. Ficaram gravados como se fossem
euros: nas horas em que os mercados estavam acoplados, a Roménia aparecia ~4,9
vezes acima da Hungria e a Bulgária exatamente 1,95583 vezes acima da Grécia.

TAXAS (taxas_cambio_precos_qh.csv, uma linha por zona-dia)
-----
  BGN  1,95583, a taxa fixa do lev face ao euro.
  RON  a do BNR publicada no último dia útil antes do dia do leilão (D-1). É a
       que o OPCOM usou: com ela, o preço romeno fica igual ao da Hungria nas
       horas acopladas em 1217 dos 1220 dias que as têm.
  PLN  a de referência do BCE do dia de entrega. A Polónia quase nunca esteve
       acoplada nestes anos, e não há horas iguais às dos vizinhos que cheguem
       para saber que taxa a TGE usou; as candidatas diferem menos de 0,2 %.

O QUE FAZ
---------
Divide values, avg, min e max de cada zona-dia pela taxa (avg, min e max saem
dos valores, como no compute_day_stats) e acrescenta "moeda_original" e
"taxa_cambio": euros = valor original / taxa_cambio. As horas do mínimo e do
máximo não mudam. Os zona-dias que já tenham "moeda_original" ficam como
estão, por isso correr outra vez não converte nada duas vezes.

No fim conta, nas horas acopladas, os quartos iguais ao vizinho (±0,01) antes
e depois: é a prova de que a moeda e a taxa estão certas.

USO
---
  python corrigir_moeda_precos_qh.py --simular   # só o relatório, não grava
  python corrigir_moeda_precos_qh.py
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import atualizar_mapa_precos_entsoe as mapa  # noqa: E402  (load/save e fusos, para o formato ser o mesmo)

TAXAS_CSV = os.path.join(SCRIPT_DIR, "taxas_cambio_precos_qh.csv")

# Vizinhos com que se confere o resultado (os mercados acoplados dão o mesmo preço em euros)
VIZINHOS = {"RO": ["HU", "SK"], "PL": ["LT", "SE4"], "BG": ["GR"]}
# Dias mostrados no relatório: o primeiro, um a meio e o último de cada zona
EXEMPLOS = {"2018-01-01", "2020-06-10", "2019-11-19", "2021-06-17", "2022-01-21"}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def ler_taxas():
    """{AAAA-MM: [(zona, dia, moeda, taxa)]}"""
    por_mes = defaultdict(list)
    with open(TAXAS_CSV, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            por_mes[r["data"][:7]].append((r["zona"], r["data"], r["moeda"], float(r["taxa"])))
    return por_mes


def por_instante(zona, dia, values):
    """{instante UTC: valor}: a posição i começa i x 15 min depois da meia-noite local."""
    tz = ZoneInfo(mapa.ZONE_TIMEZONES[zona])
    y, m, d = map(int, dia.split("-"))
    ini = datetime(y, m, d, tzinfo=tz).astimezone(timezone.utc)
    return {ini + timedelta(minutes=15 * i): v for i, v in enumerate(values) if v is not None}


def iguais(zona, dia, values, mes):
    """(quartos iguais a um vizinho ±0,01, quartos comparáveis)"""
    z = por_instante(zona, dia, values)
    ws = [por_instante(v, dia, mes[dia][v]["values"]) for v in VIZINHOS[zona]
          if v in mes.get(dia, {}) and mes[dia][v].get("values")]
    comp = [t for t in z if any(t in w for w in ws)]
    return sum(1 for t in comp if any(t in w and abs(z[t] - w[t]) <= 0.01 for w in ws)), len(comp)


def converter(s, moeda, taxa):
    vals = s.get("values")
    conhecidos = [v for v in (vals or []) if v is not None]
    if conhecidos:
        s["values"] = [round(v / taxa, 2) if v is not None else None for v in vals]
        s["avg"] = round(sum(conhecidos) / len(conhecidos) / taxa, 2)
        s["min"] = round(min(conhecidos) / taxa, 2)
        s["max"] = round(max(conhecidos) / taxa, 2)
    else:
        for k in ("avg", "min", "max"):
            if s.get(k) is not None:
                s[k] = round(s[k] / taxa, 2)
    s["moeda_original"] = moeda
    s["taxa_cambio"] = taxa


def main():
    p = argparse.ArgumentParser(description="Converte para euros os preços de RO/PL/BG gravados na moeda local.")
    p.add_argument("--simular", action="store_true", help="só o relatório, sem gravar")
    args = p.parse_args()

    por_mes = ler_taxas()
    cont = defaultdict(lambda: defaultdict(int))
    exemplos = defaultdict(list)
    meses_gravados = 0
    print(f"corrigir_moeda_precos_qh — {sum(len(v) for v in por_mes.values())} zona-dias em "
          f"{len(por_mes)} meses{' (SIMULAÇÃO: nada é gravado)' if args.simular else ''}\n")

    for ym in sorted(por_mes):
        mes = mapa.load_monthly_file(ym)
        mudou = False
        for zona, dia, moeda, taxa in por_mes[ym]:
            s = mes.get(dia, {}).get(zona)
            if s is None:
                cont[zona]["sem dados"] += 1
                continue
            if s.get("moeda_original"):
                cont[zona]["já estava"] += 1
                continue
            antes_avg = s.get("avg")
            if s.get("values"):
                a = iguais(zona, dia, s["values"], mes)
            converter(s, moeda, taxa)
            if s.get("values"):
                b = iguais(zona, dia, s["values"], mes)
                cont[zona]["iguais antes"] += a[0]
                cont[zona]["iguais depois"] += b[0]
                cont[zona]["comparáveis"] += b[1]
            cont[zona]["convertido"] += 1
            if dia in EXEMPLOS:
                viz = [f"{v} {mes[dia][v]['avg']}" for v in VIZINHOS[zona] if v in mes.get(dia, {})]
                exemplos[zona].append(f"{dia}: avg {antes_avg} {moeda} -> {s['avg']} EUR "
                                      f"(taxa {taxa}; {', '.join(viz)})")
            mudou = True
        if mudou:
            meses_gravados += 1
            if not args.simular:
                mapa.save_monthly_file(ym, mes)

    for zona in ("RO", "PL", "BG"):
        c = cont[zona]
        print(f"{zona}: {c['convertido']} convertidos, {c['já estava']} já estavam, {c['sem dados']} sem dados")
        if c["comparáveis"]:
            print(f"    quartos iguais a {'/'.join(VIZINHOS[zona])} (±0,01): antes {c['iguais antes']}, "
                  f"depois {c['iguais depois']}, de {c['comparáveis']} comparáveis "
                  f"({100 * c['iguais depois'] / c['comparáveis']:.1f} %)")
        for e in exemplos[zona]:
            print("    " + e)
    print(f"\n{meses_gravados} ficheiro(s) mensal(is) {'a gravar (simulação)' if args.simular else 'gravado(s)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
