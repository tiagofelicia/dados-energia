#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_albufeiras_ree.py
Reservas hidroelétricas de Espanha peninsular, mês a mês: energia guardada e enchimento.

Fonte: Red Eléctrica (REData, API pública apidatos.ree.es), indicador
generacion/reservas-hidroelectricas. Três séries por mês: as albufeiras anuais (as que
se enchem e esvaziam dentro do ano), as hiperanuais (as grandes, que guardam água de
um ano para o outro) e o total. Cada uma com a energia que a água pode produzir e o
enchimento, em percentagem da capacidade. A REE só dá esta série ao mês (pedida ao
dia ou à semana, responde que não há dados) e só um ano de cada vez. Sem chave.

USO
---
  python atualizar_albufeiras_ree.py              # o ano corrente e o anterior (revisões incluídas)
  python atualizar_albufeiras_ree.py --backfill   # tudo desde 1990, um pedido por ano

Se o ficheiro ainda não existe, faz sempre o backfill.

SAÍDA
-----
  data/producao/albufeiras_mensal_es.csv

    mes               AAAA-MM
    total_gwh         energia guardada no total das albufeiras (GWh; a REE publica MWh)
    total_pct         enchimento do total (% da capacidade)
    anuais_gwh        idem, só nas albufeiras anuais
    anuais_pct
    hiperanuais_gwh   idem, só nas hiperanuais
    hiperanuais_pct

Os meses novos juntam-se aos que já estão no ficheiro; um valor revisto pela REE
substitui o anterior. Só se reescreve o ficheiro se alguma coisa mudou, e nunca com
um mês em falta pelo meio ou com valores impossíveis.
"""

import csv
import io
import os
import sys
import time
from datetime import datetime, timezone

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

URL = "https://apidatos.ree.es/es/datos/generacion/reservas-hidroelectricas"
CAMINHO = os.path.join(ROOT_DIR, "data", "producao", "albufeiras_mensal_es.csv")
SERIES = ["total", "anuais", "hiperanuais"]
COLUNAS = ["mes"] + [f"{s}_{u}" for s in SERIES for u in ("gwh", "pct")]
INICIO_BACKFILL = 1990   # o primeiro ano que a REE tem
TENTATIVAS = 3


def serie_do_titulo(titulo):
    """«Reservas embalses anuales (GWh) y Llenado…», «… hiperanuales …», «Total reservas …»."""
    t = titulo.lower()
    if "hiperanual" in t:
        return "hiperanuais"
    if "total" in t:
        return "total"
    if "anual" in t:
        return "anuais"
    return None


def pedir_ano(ano):
    """O JSON do ano, ou None se a REE não tiver nenhum mês dele (responde 400 «no están disponibles»:
    em janeiro, antes de sair o primeiro mês). O mesmo 400 pode ser uma falha passageira, por isso
    também se repete; um ano a meio que falte apanha-se depois, na verificação dos meses seguidos."""
    params = {"start_date": f"{ano}-01-01T00:00", "end_date": f"{ano}-12-31T23:59", "time_trunc": "month"}
    erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            r = requests.get(URL, params=params, timeout=60, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            erro = f"HTTP {r.status_code}"
            sem_dados = r.status_code == 400 and "disponibles" in r.text
        except (requests.RequestException, ValueError) as e:
            erro, sem_dados = str(e)[:200], False
        if tentativa < TENTATIVAS:
            time.sleep(5 * tentativa)
    if sem_dados:
        return None
    raise RuntimeError(f"REE, {ano}: {erro}")


def meses_da_resposta(j):
    linhas = {}
    for inc in j.get("included", []):
        a = inc.get("attributes", {})
        s = serie_do_titulo(a.get("title", ""))
        if s is None:
            continue
        for v in a.get("values", []):
            mes = v["datetime"][:7]
            linha = linhas.setdefault(mes, {"mes": mes})
            linha[f"{s}_gwh"] = round(v["value"] / 1000, 2)
            linha[f"{s}_pct"] = round(v["percentage"] * 100, 1)
    return linhas


def problemas(linhas):
    """Meses incompletos, impossíveis ou em falta pelo meio."""
    erros = []
    for mes, r in sorted(linhas.items()):
        if any(c not in r or r[c] in ("", None) for c in COLUNAS):
            erros.append(f"{mes}: faltam séries")
            continue
        v = {c: float(r[c]) for c in COLUNAS[1:]}
        if not all(0 <= v[f"{s}_pct"] <= 105 for s in SERIES) or not all(v[f"{s}_gwh"] >= 0 for s in SERIES):
            erros.append(f"{mes}: valores fora do possível")
        elif abs(v["anuais_gwh"] + v["hiperanuais_gwh"] - v["total_gwh"]) > 2:
            erros.append(f"{mes}: anuais + hiperanuais ≠ total")
    meses = sorted(linhas)
    if meses:
        a, m = int(meses[0][:4]), int(meses[0][5:7])
        esperado = []
        while f"{a:04d}-{m:02d}" <= meses[-1]:
            esperado.append(f"{a:04d}-{m:02d}")
            a, m = (a + 1, 1) if m == 12 else (a, m + 1)
        falta = sorted(set(esperado) - set(meses))
        if falta:
            erros.append(f"{len(falta)} mês(es) em falta: {', '.join(falta[:6])}{'…' if len(falta) > 6 else ''}")
    return erros


def ler_existente():
    if not os.path.exists(CAMINHO):
        return {}
    with open(CAMINHO, "r", encoding="utf-8-sig", newline="") as f:
        return {r["mes"]: r for r in csv.DictReader(f)}


def texto_csv(linhas):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUNAS, lineterminator="\n")
    w.writeheader()
    for k in sorted(linhas):
        w.writerow({c: linhas[k][c] for c in COLUNAS})
    return buf.getvalue()


def main():
    backfill = "--backfill" in sys.argv or not os.path.exists(CAMINHO)
    hoje = datetime.now(timezone.utc).date()
    anos = range(INICIO_BACKFILL if backfill else hoje.year - 1, hoje.year + 1)
    print(f"Albufeiras de Espanha (REE): {anos[0]} → {anos[-1]}{' (backfill)' if backfill else ''}")

    novas = {}
    for ano in anos:
        j = pedir_ano(ano)
        meses = meses_da_resposta(j) if j else {}
        print(f"  {ano}: {len(meses)} mês(es)")
        novas.update(meses)
    if not novas:
        print("Nenhum mês devolvido pela REE.")
        sys.exit(1 if backfill else 0)

    existentes = ler_existente()
    antigo = texto_csv(existentes) if existentes else None
    existentes.update({k: {c: str(v) for c, v in r.items()} for k, r in novas.items()})
    erros = problemas(existentes)
    if erros:
        print("ERRO: o ficheiro não foi escrito.")
        for e in erros[:20]:
            print(f"  {e}")
        sys.exit(1)
    novo = texto_csv(existentes)
    if novo == antigo:
        print(f"= {os.path.relpath(CAMINHO, ROOT_DIR)} sem alterações ({len(existentes)} meses)")
        return
    os.makedirs(os.path.dirname(CAMINHO), exist_ok=True)
    with open(CAMINHO, "w", encoding="utf-8-sig", newline="") as f:
        f.write(novo)
    ultimo = max(existentes)
    print(f"✓ {os.path.relpath(CAMINHO, ROOT_DIR)}: {len(existentes)} meses, o último {ultimo} "
          f"({existentes[ultimo]['total_gwh']} GWh, {existentes[ultimo]['total_pct']} %)")


if __name__ == "__main__":
    main()
