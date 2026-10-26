#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_consumo_paises.py
Consumo diário de eletricidade por país (GWh/dia), para pesar pelo consumo a
"média europeia" do europe-prices.html.

PORQUÊ
------
A média simples das zonas de preço pesa demais os países com muitas zonas
(Noruega 5, Suécia 4, Itália 7, Dinamarca 2) e trata a Alemanha como a Estónia.
Em 2026 dava em média 111,15 €/MWh contra 107,32 pesada pelo consumo real, com
diferenças diárias de até 41,7 €/MWh.

DE ONDE VEM
-----------
Dos ficheiros mensais do mapa de produção (data/mapas/producao/AAAA-MM.json,
Energy-Charts), o 'load' de cada país e dia:
  - source "real" (ou sem source, nos dias mais antigos): o consumo real;
  - source "forecast" ou "provisional" (o dia seguinte e o dia a meio): a
    previsão day-ahead (forecast.load). Contra o consumo real, a média pesada
    pela previsão desvia em média 0,30 €/MWh.

O QUE ESCREVE
-------------
data/mapas/consumo_paises.json
  ultima_data      último dia com consumo real
  ultima_previsao  último dia com previsão
  quotas           peso fixo de cada país: o seu consumo médio diário real nos
                   últimos 365 dias, a dividir pela soma dos países. Serve os
                   dias sem dados (antes de 2026) e os países sem valor num dia;
                   contra o consumo real, desvia em média 1,13 €/MWh por dia
  dias             {dia: {país: GWh}} com o consumo real
  previsao         {dia: {país: GWh}} só onde não há real

AL, MK e XK não têm consumo no Energy-Charts e ficam fora da média pesada.

USO
---
  python gerar_consumo_paises.py
"""

import glob
import json
import os
import sys
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PRODUCAO_DIR = os.path.join(ROOT_DIR, "data", "mapas", "producao")
SAIDA = os.path.join(ROOT_DIR, "data", "mapas", "consumo_paises.json")
JANELA_QUOTAS = 365


def ler_dias():
    """{dia: {país: (GWh, 'r'|'p')}} a partir dos ficheiros mensais da produção."""
    out = {}
    for f in sorted(glob.glob(os.path.join(PRODUCAO_DIR, "[0-9][0-9][0-9][0-9]-[0-9][0-9].json"))):
        with open(f, encoding="utf-8") as fh:
            mes = json.load(fh)
        for dia, paises in mes.items():
            for pais, v in paises.items():
                if not isinstance(v, dict) or not v:
                    continue
                src = v.get("source")
                if src in (None, "real"):
                    carga, tipo = (v.get("production") or {}).get("load"), "r"
                else:   # "forecast" ou "provisional"
                    carga, tipo = (v.get("forecast") or {}).get("load"), "p"
                if isinstance(carga, (int, float)) and carga > 0:
                    out.setdefault(dia, {})[pais] = (round(float(carga), 2), tipo)
    return out


def main():
    dias = ler_dias()
    reais = sorted(d for d, ps in dias.items() if any(t == "r" for _, t in ps.values()))
    if not reais:
        print("sem consumo real nos ficheiros da produção — nada a escrever")
        return 1
    ultima_real = reais[-1]
    inicio = (date.fromisoformat(ultima_real) - timedelta(days=JANELA_QUOTAS - 1)).isoformat()

    # Quotas: consumo médio diário real de cada país na janela, a dividir pela soma
    soma, n = {}, {}
    for d in reais:
        if d < inicio:
            continue
        for pais, (gwh, tipo) in dias[d].items():
            if tipo == "r":
                soma[pais] = soma.get(pais, 0.0) + gwh
                n[pais] = n.get(pais, 0) + 1
    medias = {p: soma[p] / n[p] for p in soma}
    total = sum(medias.values())
    quotas = {p: round(m / total, 6) for p, m in sorted(medias.items())}

    real = {d: {p: g for p, (g, t) in sorted(ps.items()) if t == "r"} for d, ps in sorted(dias.items())}
    previsao = {d: {p: g for p, (g, t) in sorted(ps.items()) if t == "p"} for d, ps in sorted(dias.items())}
    saida = {
        "ultima_data": ultima_real,
        "ultima_previsao": max((d for d, ps in previsao.items() if ps), default=None),
        "unidade": "GWh/dia",
        "fonte": "Energy-Charts (Fraunhofer ISE), via data/mapas/producao",
        "quotas": quotas,
        "dias": {d: ps for d, ps in real.items() if ps},
        "previsao": {d: ps for d, ps in previsao.items() if ps},
    }
    with open(SAIDA, "w", encoding="utf-8", newline="") as fh:
        json.dump(saida, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"consumo_paises.json: {len(saida['dias'])} dias reais (até {ultima_real}), "
          f"{len(saida['previsao'])} com previsão (até {saida['ultima_previsao']}), "
          f"{len(quotas)} países, {os.path.getsize(SAIDA) / 1024:.0f} KB")
    print("quotas:", ", ".join(f"{p} {100 * q:.1f}%" for p, q in sorted(quotas.items(), key=lambda x: -x[1])[:8]), "…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
