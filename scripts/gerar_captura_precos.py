#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_captura_precos.py
Preço capturado solar e eólico por zona e dia: o preço day-ahead pesado pela
produção, ou seja, o preço médio a que essa produção foi de facto vendida. Com
muito solar, o preço das horas de sol cai, e o preço capturado solar fica abaixo
da média do dia — é a "canibalização" que este número mostra.

DE ONDE VEM
-----------
  preços    data/mapas/precos_qh/AAAA-MM.json (quartos de hora do dia local da zona)
  produção  data/producao/producao-entsoe/<país>/AAAA-Www.json (ENTSO-E, MW a 15,
            30 ou 60 min)

Cada quarto de hora com preço p e produção P MW pesa P × 0,25 MWh. A produção é a
do país: nas zonas de um mesmo país (Itália, Noruega, Suécia, Dinamarca) usa-se a
produção do país com o preço de cada zona. Os nulos da produção contam como zero
(o solar vem a null de noite). Eólica = onshore + offshore. Só entram os dias em
que a produção real (até last_actual_ts) cobre o dia inteiro da zona; até 4 quartos
de hora sem produção ou sem preço (falhas pontuais das fontes) ficam de fora da conta.
Um dia com mais falhas fica sem valor até as fontes serem completadas.

O QUE ESCREVE
-------------
  data/mapas/captura/AAAA-MM.json   {dia: {zona: {"s": [€/MWh, GWh], "w": [€/MWh, GWh]}}}
  data/mapas/captura/metadata.json  {"ultima_data": "AAAA-MM-DD"}

Para um período: preço capturado = Σ(preço × GWh) / Σ GWh.

USO
---
  python gerar_captura_precos.py          # os meses dos últimos 62 dias (tudo, na 1.ª vez)
  python gerar_captura_precos.py --tudo   # desde 2026-01
"""

import argparse
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PRECOS_DIR = os.path.join(ROOT_DIR, "data", "mapas", "precos_qh")
PRODUCAO_DIR = os.path.join(ROOT_DIR, "data", "producao", "producao-entsoe")
SAIDA_DIR = os.path.join(ROOT_DIR, "data", "mapas", "captura")
INICIO = "2026-01"   # a produção da ENTSO-E começa na semana 1 de 2026

# Zonas de preço de cada país (as virtuais — IT-SACOAC, IT-SACODC, NO2NSL — ficam de fora)
PAIS_ZONAS = {
    "al": ["AL"], "at": ["AT"], "be": ["BE"], "bg": ["BG"], "ch": ["CH"], "cz": ["CZ"],
    "de": ["DE-LU"], "dk": ["DK1", "DK2"], "ee": ["EE"], "es": ["ES"], "fi": ["FI"],
    "fr": ["FR"], "gr": ["GR"], "hr": ["HR"], "hu": ["HU"], "ie": ["IE(SEM)"],
    "it": ["IT-North", "IT-Centre-North", "IT-Centre-South", "IT-South", "IT-Sicily",
           "IT-Sardinia", "IT-Calabria"],
    "lt": ["LT"], "lv": ["LV"], "me": ["ME"], "mk": ["MK"], "nl": ["NL"],
    "no": ["NO1", "NO2", "NO3", "NO4", "NO5"], "pl": ["PL"], "pt": ["PT"], "ro": ["RO"],
    "rs": ["RS"], "se": ["SE1", "SE2", "SE3", "SE4"], "si": ["SI"], "sk": ["SK"], "xk": ["XK"],
}
SOLAR = ("Solar",)
EOLICA = ("Wind onshore", "Wind offshore")


def ler_producao(pais, dias):
    """({instante UTC em s: (solar MW, eólica MW)}, passo em s, fim da produção real em s)
    a partir das semanas ISO que cobrem 'dias'."""
    semanas = sorted({"%d-W%02d" % d.isocalendar()[:2] for d in dias})
    serie, passo, fim_real = {}, None, 0
    for sem in semanas:
        f = os.path.join(PRODUCAO_DIR, pais, f"{sem}.json")
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            j = json.load(fh)
        p = int(j.get("interval_minutes") or 60) * 60
        passo = passo or p
        ultimo = j.get("last_actual_ts")
        if ultimo is None:
            continue
        fim_real = max(fim_real, ultimo + p)
        tipos = {t["name"]: t["data"] for t in j.get("production_types", [])}
        for k, ts in enumerate(j.get("unix_seconds", [])):
            if ts > ultimo:
                break
            s = sum((tipos[n][k] or 0) for n in SOLAR if n in tipos)
            w = sum((tipos[n][k] or 0) for n in EOLICA if n in tipos)
            serie[ts] = (s, w)
    return serie, passo, fim_real


# Quartos de hora sem produção ou sem preço que um dia ainda tolera (ficam de fora da conta). Há
# falhas pontuais nas fontes: a Espanha não tem a produção de 00:00 e 00:15 UTC de 25/10/2026, na
# hora que se repete. Um dia só com algumas horas de preço (13/09/2026 na Grécia e na Roménia: 4
# quartos de 96) daria o preço capturado dessas horas, e não o do dia.
QUARTOS_EM_FALTA_MAX = 4


def captura_dia(valores, inicio_utc, serie, passo, fim_real):
    """{"s": [...], "w": [...]} de um dia, ou None se a produção ou o preço não cobrirem o dia."""
    n = len(valores)
    if not passo or inicio_utc + n * 900 > fim_real:
        return None
    ps = es = pw = ew = 0.0
    em_falta = 0
    for i, p in enumerate(valores):
        t = inicio_utc + i * 900
        # O passo pode mudar de uma semana para outra: o quarto de hora, a meia hora ou a hora
        g = serie.get(t) or serie.get(t - t % 1800) or serie.get(t - t % 3600)
        if g is None or p is None:
            em_falta += 1
            if em_falta > QUARTOS_EM_FALTA_MAX:
                return None
            continue
        es += g[0] * 0.25
        ps += p * g[0] * 0.25
        ew += g[1] * 0.25
        pw += p * g[1] * 0.25
    out = {}
    if es > 0:
        out["s"] = [round(ps / es, 2), round(es / 1000, 3)]
    if ew > 0:
        out["w"] = [round(pw / ew, 2), round(ew / 1000, 3)]
    return out or None


def main():
    ap = argparse.ArgumentParser(description="Preço capturado solar e eólico por zona e dia.")
    ap.add_argument("--tudo", action="store_true", help=f"todos os meses desde {INICIO}")
    args = ap.parse_args()

    with open(os.path.join(PRECOS_DIR, "metadata.json"), encoding="utf-8") as fh:
        fusos = json.load(fh).get("fusos", {})
    meses = sorted(os.path.basename(f)[:-5] for f in glob.glob(os.path.join(PRECOS_DIR, "[0-9][0-9][0-9][0-9]-[0-9][0-9].json")))
    meses = [m for m in meses if m >= INICIO]
    # Na primeira corrida (sem metadata.json) calcula-se tudo, para o histórico ficar completo
    if not os.path.exists(os.path.join(SAIDA_DIR, "metadata.json")):
        args.tudo = True
    if not args.tudo:
        limite = (datetime.now(timezone.utc).date() - timedelta(days=62)).isoformat()[:7]
        meses = [m for m in meses if m >= limite]
    os.makedirs(SAIDA_DIR, exist_ok=True)

    ultima, total = None, 0
    for ym in meses:
        with open(os.path.join(PRECOS_DIR, f"{ym}.json"), encoding="utf-8") as fh:
            precos = json.load(fh)
        dias = sorted(d for d in precos if d.startswith(ym))
        saida = {}
        for pais, zonas in PAIS_ZONAS.items():
            serie, passo, fim_real = ler_producao(pais, [date.fromisoformat(d) for d in dias])
            if not serie:
                continue
            for z in zonas:
                tz = ZoneInfo(fusos.get(z, "Europe/Brussels"))
                for d in dias:
                    st = precos[d].get(z)
                    if not st or not st.get("values"):
                        continue
                    y, m, dd = map(int, d.split("-"))
                    ini = int(datetime(y, m, dd, tzinfo=tz).astimezone(timezone.utc).timestamp())
                    c = captura_dia(st["values"], ini, serie, passo, fim_real)
                    if c:
                        saida.setdefault(d, {})[z] = c
        n = sum(len(v) for v in saida.values())
        total += n
        with open(os.path.join(SAIDA_DIR, f"{ym}.json"), "w", encoding="utf-8", newline="") as fh:
            json.dump(saida, fh, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if saida:
            ultima = max(ultima or "", max(saida))
        print(f"  {ym}.json: {len(saida)} dias, {n} zona-dias")

    meta = os.path.join(SAIDA_DIR, "metadata.json")
    anterior = None
    if os.path.exists(meta):
        with open(meta, encoding="utf-8") as fh:
            anterior = json.load(fh).get("ultima_data")
    if ultima and (anterior is None or ultima > anterior or args.tudo):
        with open(meta, "w", encoding="utf-8", newline="") as fh:
            json.dump({"ultima_data": ultima}, fh)
    print(f"gerar_captura_precos: {total} zona-dias em {len(meses)} meses; última data {ultima or anterior}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
