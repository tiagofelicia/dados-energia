#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_mercados_trimestral.py
Médias por trimestre dos mercados de energia, para a página /mercados-trimestre do site.

  python scripts/gerar_mercados_trimestral.py

  -> data/agregados/mercados_trimestral.csv
     trimestre, indicador, valor, unidade, n, de, ate
     (uma linha por trimestre e indicador; trimestre = AAAA-Tn)

Não recolhe nada de fora: deriva dos ficheiros que já estão no repositório, por isso corre no
workflow dos agregados, depois do gerar_agregados.py e do gerar_erc_resumo.py.

Indicadores (todos sem IVA):
  omie_pt                  OMIE PT, média simples de todos os quartos de hora do trimestre (dias em hora de
                           Portugal), de agregados/omie_diario.csv (pt_medio pesado pelos quartos de hora)
  omie_es                  OMIE ES nas mesmas horas (es_medio do mesmo ficheiro)
  omie_pt_horas_negativas  horas com o OMIE PT abaixo de zero (soma das horas_negativas dos dias)
  erc                      ERC da REN pesado pelo consumo (Σ€ ÷ ΣMWh), por dia de mercado, de
                           agregados/erc_diario/AAAA.json (só desde 14/03/2024)
  omip_pt_trimestre        OMIP PT (FPB), contrato do trimestre seguinte: média dos fechos diários durante
                           o trimestre, de omie/historico/omip_historico_AAAA.csv (só desde 20/10/2025)
  omip_pt_ano              OMIP PT (FPB), contrato do ano seguinte, idem
  mibgas_pt, mibgas_es     índices de referência MIBGAS PT e ES (média simples dos dias), de gas/mibgas_spot.csv
  mibgas_es_trimestre      futuro MIBGAS ES do trimestre seguinte: média dos preços de referência diários
                           durante o trimestre, de gas/mibgas_futuros.csv (só Espanha tem futuros a prazo)
  mibgas_es_ano            futuro MIBGAS ES do ano seguinte, idem
  co2_eua                  licenças de emissão de CO2 (EUA) nos leilões da EEX: preço médio pesado pelo volume,
                           de emissoes/eua_co2.csv (só os leilões concluídos; sem as EUAA da aviação)

n = quantas observações entraram (dias, dias de cotação ou leilões); de e ate = a primeira e a última. No
trimestre corrente, e nos indicadores com histórico curto, o ate e o n dizem até onde vai a média. Os
contratos a prazo escolhem-se pela data de entrega, não pelo nome relativo (Q+1, Y+1), para não trocar de
contrato nos últimos dias do trimestre. O script só reescreve o ficheiro quando alguma coisa muda.
"""
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT_DIR, "data")
SAIDA = os.path.join(DATA, "agregados", "mercados_trimestral.csv")
COLUNAS = ["trimestre", "indicador", "valor", "unidade", "n", "de", "ate"]
# Ordem das linhas dentro de cada trimestre (e a ordem em que a página os mostra)
INDICADORES = [
    ("omie_pt", "EUR/MWh"), ("omie_es", "EUR/MWh"), ("omie_pt_horas_negativas", "horas"), ("erc", "EUR/MWh"),
    ("omip_pt_trimestre", "EUR/MWh"), ("omip_pt_ano", "EUR/MWh"),
    ("mibgas_pt", "EUR/MWh"), ("mibgas_es", "EUR/MWh"), ("mibgas_es_trimestre", "EUR/MWh"), ("mibgas_es_ano", "EUR/MWh"),
    ("co2_eua", "EUR/t"),
]
UNIDADE = dict(INDICADORES)


def trimestre(iso):
    a, m = int(iso[:4]), int(iso[5:7])
    return f"{a}-T{(m - 1) // 3 + 1}"


def seguinte(tri):
    """'2026-T4' → (2027, 1)"""
    a, q = int(tri[:4]), int(tri[-1])
    return (a + 1, 1) if q == 4 else (a, q + 1)


def num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ler_csv(caminho):
    if not os.path.exists(caminho):
        print(f"::warning::{os.path.relpath(caminho, ROOT_DIR)} não existe")
        return []
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Media:
    """Média (simples ou pesada) por trimestre, com a contagem e a primeira e a última data."""
    def __init__(self):
        self.s = defaultdict(float)
        self.w = defaultdict(float)
        self.n = defaultdict(int)
        self.dias = defaultdict(set)

    def juntar(self, iso, valor, peso=1.0, tri=None):
        if valor is None or not peso:
            return
        t = tri or trimestre(iso)
        self.s[t] += valor * peso
        self.w[t] += peso
        self.n[t] += 1
        self.dias[t].add(iso)

    def linhas(self, indicador, casas=2, soma=False):
        out = []
        for t in self.s:
            if not self.w[t]:
                continue
            v = self.s[t] if soma else self.s[t] / self.w[t]
            ds = sorted(self.dias[t])
            out.append((t, indicador, round(v, casas), len(ds) if indicador != "co2_eua" else self.n[t], ds[0], ds[-1]))
        return out


def omie():
    pt, es, neg = Media(), Media(), Media()
    for r in ler_csv(os.path.join(DATA, "agregados", "omie_diario.csv")):
        d, q = r.get("data_iso"), num(r.get("quartos"))
        if not d or not q:
            continue
        pt.juntar(d, num(r.get("pt_medio")), q)
        es.juntar(d, num(r.get("es_medio")), q)
        neg.juntar(d, num(r.get("horas_negativas")))
    return pt.linhas("omie_pt") + es.linhas("omie_es") + neg.linhas("omie_pt_horas_negativas", 0, soma=True)


def erc():
    m = Media()
    pasta = os.path.join(DATA, "agregados", "erc_diario")
    if not os.path.isdir(pasta):
        return []
    for nome in sorted(os.listdir(pasta)):
        if not re.fullmatch(r"\d{4}\.json", nome):
            continue
        with open(os.path.join(pasta, nome), encoding="utf-8") as f:
            j = json.load(f)
        for d, v in (j.get("mercado") or {}).items():
            mwh, eur = num(v[0]), num(v[1])
            if mwh:
                m.juntar(d, eur / mwh, mwh)       # Σ€ ÷ ΣMWh
    return m.linhas("erc")


def omip():
    tri, ano = Media(), Media()
    pasta = os.path.join(DATA, "omie", "historico")
    for nome in sorted(os.listdir(pasta)) if os.path.isdir(pasta) else []:
        if not re.fullmatch(r"omip_historico_\d{4}\.csv", nome):
            continue
        for r in ler_csv(os.path.join(pasta, nome)):
            if (r.get("Zona") or "").strip() != "PT":
                continue
            p = (r.get("Data") or "").strip().split("/")
            if len(p) != 3:
                continue
            d = f"{p[2]}-{p[1]}-{p[0]}"
            t = trimestre(d)
            a1, q1 = seguinte(t)
            c = (r.get("Contrato") or "").strip()
            if c == f"FPB Q{q1}-{a1 % 100:02d}":
                tri.juntar(d, num(r.get("Valor")))
            elif c == f"FPB YR-{(int(d[:4]) + 1) % 100:02d}":
                ano.juntar(d, num(r.get("Valor")))
    return tri.linhas("omip_pt_trimestre") + ano.linhas("omip_pt_ano")


def mibgas():
    pt, es = Media(), Media()
    for r in ler_csv(os.path.join(DATA, "gas", "mibgas_spot.csv")):
        d = r.get("data_iso")
        if d:
            pt.juntar(d, num(r.get("mibgas_pt")))
            es.juntar(d, num(r.get("mibgas_es")))
    tri, ano = Media(), Media()
    vistos = set()
    for r in ler_csv(os.path.join(DATA, "gas", "mibgas_futuros.csv")):
        d, h, ent = r.get("data_iso"), r.get("horizonte"), r.get("entrega_inicio")
        if r.get("area") != "ES" or h not in ("trimestre", "ano") or not d or not ent:
            continue
        v = num(r.get("preco_referencia"))
        if v is None:
            v = num(r.get("preco_ultimo"))
        t = trimestre(d)
        if h == "trimestre":
            a1, q1 = seguinte(t)
            if ent != f"{a1}-{3 * q1 - 2:02d}-01":
                continue
        elif ent != f"{int(d[:4]) + 1}-01-01":
            continue
        if (d, h) in vistos:
            continue
        vistos.add((d, h))
        (tri if h == "trimestre" else ano).juntar(d, v)
    return (pt.linhas("mibgas_pt") + es.linhas("mibgas_es")
            + tri.linhas("mibgas_es_trimestre") + ano.linhas("mibgas_es_ano"))


def co2():
    m = Media()
    for r in ler_csv(os.path.join(DATA, "emissoes", "eua_co2.csv")):
        if r.get("tipo") != "EUA" or r.get("estado") != "successful":
            continue
        m.juntar(r.get("data_iso"), num(r.get("preco_eur_t")), num(r.get("volume_tco2")) or 0)
    return m.linhas("co2_eua")


def fmt(v, casas):
    if casas == 0:
        return str(int(round(v)))
    return f"{v:.{casas}f}"


def main():
    linhas = omie() + erc() + omip() + mibgas() + co2()
    if not linhas:
        print("::error::Nenhum dado para os trimestres — nada foi escrito.")
        return 1
    ordem = {k: i for i, (k, _) in enumerate(INDICADORES)}
    linhas.sort(key=lambda l: (l[0], ordem[l[1]]))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUNAS)
    for t, ind, v, n, de, ate in linhas:
        w.writerow([t, ind, fmt(v, 0 if UNIDADE[ind] == "horas" else 2), UNIDADE[ind], n, de, ate])
    novo = "﻿" + buf.getvalue()
    if os.path.exists(SAIDA):
        with open(SAIDA, encoding="utf-8", newline="") as f:
            if f.read() == novo:
                print("= mercados_trimestral.csv sem alterações")
                return 0
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "w", encoding="utf-8", newline="") as f:
        f.write(novo)
    tris = sorted({l[0] for l in linhas})
    print(f"✓ data/agregados/mercados_trimestral.csv: {len(linhas)} linhas, {tris[0]} a {tris[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
