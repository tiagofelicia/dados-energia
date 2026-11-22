"""
Consumo mensal de gás natural em Portugal continental, segundo a REN (Data Hub → Gás → Evolução do
Consumo, modelo 2840): o total, o mercado convencional (consumo doméstico e industrial) e o mercado
elétrico (as centrais de ciclo combinado), em GWh, desde janeiro de 2014.

  python scripts/atualizar_consumo_gas_ren.py          # o ano corrente e o anterior
  python scripts/atualizar_consumo_gas_ren.py --tudo   # tudo desde 2014

  -> data/gas/consumo_gas_mensal.csv   (mes, consumo_gwh, convencional_gwh, eletrico_gwh)

Notas sobre a fonte:
  • O CSV do modelo 2840 traz os meses do ANO da data pedida: um pedido por ano. Antes de 2014 a REN
    responde «Empty table».
  • Vem em UTF-8 com BOM e com os meses por extenso («março»). Os valores são GWh inteiros.
  • A REN revê os meses recentes: cada corrida volta a pedir o ano corrente e o anterior.
  • A página da REN marca com (*) os valores previstos; se um dia vierem no CSV, essas linhas ficam de
    fora (só entram valores reais).
O script só reescreve o ficheiro quando alguma coisa muda. Não precisa de secrets, e o datahub.ren.pt
aceita ligações dos runners do GitHub.
"""
import argparse
import csv
import io
import os
import sys
import time
from datetime import date

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FICHEIRO = os.path.join(ROOT_DIR, "data", "gas", "consumo_gas_mensal.csv")
URL = "https://datahub.ren.pt/service/exports/csv"
MODELO = 2840
ANO_INICIAL = 2014
COLUNAS = ["mes", "consumo_gwh", "convencional_gwh", "eletrico_gwh"]
MESES = {"janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
         "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; dados-energia; +https://dados.tiagofelicia.pt)"}
TENTATIVAS = 3


def pedir_ano(ano):
    """Texto do CSV do modelo 2840 para um ano (str), ou None se a REN não tiver esse ano."""
    params = {"startDateString": f"{ano}-01-31", "endDateString": f"{ano}-01-31",
              "culture": "pt-PT", "country": "PT", "modelId": MODELO}
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            try:
                texto = r.content.decode("utf-8-sig")
            except UnicodeDecodeError:            # por precaução: a REN já mandou CSVs em Windows-1252
                texto = r.content.decode("cp1252")
            return None if texto.strip().startswith("Empty table") else texto
        except requests.RequestException as e:
            if tentativa == TENTATIVAS:
                raise
            print(f"  {ano}: {e} — nova tentativa daqui a {5 * tentativa} s")
            time.sleep(5 * tentativa)


def num(s):
    s = (s or "").strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    return float(s.replace(",", "."))


def ler_ano(ano, texto):
    """{'AAAA-MM': (consumo, convencional, elétrico)} dos meses reais de um ano."""
    linhas = list(csv.reader(io.StringIO(texto)))
    out, previstos = {}, 0
    for c in linhas[2:]:                      # as duas primeiras linhas são o cabeçalho
        if len(c) < 10 or not c[0].strip():
            continue
        nome = c[0].strip().lower()
        if "*" in nome or any("*" in x for x in (c[1], c[5], c[9])):
            previstos += 1
            continue
        mes = MESES.get(nome)
        if mes is None:
            raise ValueError(f"{ano}: mês desconhecido {c[0]!r} — mudou o formato da REN?")
        total, conv, elet = num(c[1]), num(c[5]), num(c[9])
        if total is None or conv is None or elet is None:
            continue
        # O total é a soma dos dois mercados (arredondados à unidade, por isso com 2 GWh de folga)
        if abs(total - conv - elet) > 2:
            print(f"  aviso: {ano}-{mes:02d} total {total:g} ≠ convencional {conv:g} + elétrico {elet:g}")
        out[f"{ano}-{mes:02d}"] = (total, conv, elet)
    if previstos:
        print(f"  {ano}: {previstos} mês(es) com valores previstos (*) ficaram de fora")
    return out


def fmt(v):
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def ler_ficheiro():
    if not os.path.exists(FICHEIRO):
        return {}
    with open(FICHEIRO, encoding="utf-8-sig", newline="") as f:
        return {r["mes"]: (num(r["consumo_gwh"]), num(r["convencional_gwh"]), num(r["eletrico_gwh"]))
                for r in csv.DictReader(f)}


def escrever(dados):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUNAS)
    for mes in sorted(dados):
        w.writerow([mes] + [fmt(v) for v in dados[mes]])
    novo = "﻿" + buf.getvalue()
    if os.path.exists(FICHEIRO):
        with open(FICHEIRO, encoding="utf-8", newline="") as f:
            if f.read() == novo:
                return False
    os.makedirs(os.path.dirname(FICHEIRO), exist_ok=True)
    with open(FICHEIRO, "w", encoding="utf-8", newline="") as f:
        f.write(novo)
    return True


def main():
    ap = argparse.ArgumentParser(description="Consumo mensal de gás natural (REN, modelo 2840).")
    ap.add_argument("--tudo", action="store_true", help=f"pedir tudo desde {ANO_INICIAL}")
    args = ap.parse_args()

    atual = ler_ficheiro()
    hoje = date.today()
    inicio = ANO_INICIAL if (args.tudo or not atual) else hoje.year - 1
    print(f"🔥 Consumo de gás (REN, modelo {MODELO}): {inicio}–{hoje.year}"
          f"{' (sem ficheiro: tudo)' if not atual else ''}")

    novos = dict(atual)
    mudados = 0
    for ano in range(inicio, hoje.year + 1):
        texto = pedir_ano(ano)
        if texto is None:
            print(f"  {ano}: sem dados na REN")
            continue
        meses = ler_ano(ano, texto)
        for k, v in meses.items():
            if novos.get(k) != v:
                mudados += 1
            novos[k] = v
        print(f"  {ano}: {len(meses)} meses")
        time.sleep(1)

    if not novos:
        print("::error::A REN não devolveu nenhum mês — nada foi escrito.")
        return 1
    if escrever(novos):
        ultimo = max(novos)
        print(f"✓ {os.path.relpath(FICHEIRO, ROOT_DIR)}: {len(novos)} meses, até {ultimo} "
              f"({mudados} novo(s) ou revisto(s))")
    else:
        print("= sem alterações")
    return 0


if __name__ == "__main__":
    sys.exit(main())
