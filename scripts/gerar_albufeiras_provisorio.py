#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_albufeiras_provisorio.py

Valores provisórios da água guardada em cada albufeira, para os dias que o SNIRH ainda não publicou.

O albufeiras_snirh_diario.csv (o valor das 23:00 de cada dia, em hm3) é recolhido num PC em Portugal, porque o SNIRH
só aceita ligações de Portugal (ver atualizar_albufeiras_snirh.py). Quando essa recolha para — a 26/11/2026 o servidor
da APA deixou de aceitar ligações desse PC —, os dias seguintes ficam sem valor. O InfoÁgua, também da APA, publica de
hora a hora a percentagem de cada albufeira e aceita ligações de fora de Portugal (atualizar_albufeiras_agora.py, que
guarda o histórico em albufeiras-horario/). Este script converte a percentagem das 23:00 (hora de Lisboa) de cada dia
em hm3, para os dias depois do último valor do SNIRH de cada albufeira.

CONVERSÃO
---------
A percentagem do InfoÁgua é a do volume face à capacidade que a APA usa, que nem sempre é a do albufeiras_snirh.csv
(no Alto Lindoso, na Caniçada e na Daivões é outra). Por isso a capacidade sai dos próprios dados: em cada dia em que
há as duas coisas, hm3 do SNIRH ÷ (% das 23:00 do InfoÁgua ÷ 100); fica a mediana dos últimos 30 dias em comum (pelo
menos 3). Às 23:00 de Lisboa, os dois batem: nos 7 dias de 19 a 25/11/2026, o erro mediano do valor assim calculado
foi de 0,003 p.p. (as de fio de água pequenas, como Belver, Picote e Miranda, chegaram a desviar-se vários p.p.
num dia).

Só as albufeiras com um valor por dia no SNIRH (frequencia = diária): as de rega só têm um valor por semana, e no
InfoÁgua várias só dão um valor às 07:00. Se a capacidade calculada se afastar mais de 25% da do
albufeiras_snirh.csv, a albufeira fica de fora (e o log diz porquê).

Quando o SNIRH publicar os dias em falta, os valores provisórios desses dias saem do ficheiro na corrida seguinte:
o ficheiro só tem os dias depois do último valor do SNIRH de cada albufeira (com o SNIRH em dia, fica só o cabeçalho).

Output:
  data/producao/albufeiras_provisorio_diario.csv
      data, albufeira (a coluna do albufeiras_snirh_diario.csv), volume_hm3, volume_pct (a % do InfoÁgua às 23:00)
      e capacidade_hm3 (a capacidade calculada, que converte a % em hm3)

Corre no workflow atualizar_albufeiras_agora.yml, a seguir à recolha do InfoÁgua. Só grava se o conteúdo mudar.

Uso:
  python gerar_albufeiras_provisorio.py
"""
import csv
import glob
import os
import statistics
import sys
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA = os.path.join(ROOT_DIR, "data", "producao")
CAMINHO_LISTA = os.path.join(PASTA, "albufeiras_snirh.csv")
CAMINHO_DIARIO = os.path.join(PASTA, "albufeiras_snirh_diario.csv")
PADRAO_HORARIO = os.path.join(PASTA, "albufeiras-horario", "albufeiras_horario_*.csv")
CAMINHO_SAIDA = os.path.join(PASTA, "albufeiras_provisorio_diario.csv")
COLUNAS = ["data", "albufeira", "volume_hm3", "volume_pct", "capacidade_hm3"]

HORA = "23:00"          # a hora do valor diário do SNIRH («volume armazenado na última hora»), em hora de Lisboa
DIAS_CALIBRACAO = 30    # os últimos N dias em que há SNIRH e InfoÁgua
MIN_DIAS = 3            # com menos dias em comum, a albufeira fica de fora
DESVIO_MAX = 0.25       # capacidade calculada face à do albufeiras_snirh.csv


def ler_lista():
    with open(CAMINHO_LISTA, encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("coluna")]


def ler_diario(colunas):
    """{coluna: {dia: hm3}} (os dias com valor) a partir de 120 dias antes do último dia do ficheiro."""
    with open(CAMINHO_DIARIO, encoding="utf-8-sig", newline="") as f:
        linhas = list(csv.reader(f))
    cab = linhas[0]
    pos = {c: cab.index(c) for c in colunas if c in cab}
    desde = (date.fromisoformat(linhas[-1][0]) - timedelta(days=120)).isoformat()
    out = {c: {} for c in pos}
    for l in linhas[1:]:
        if l[0] < desde:
            continue
        for c, p in pos.items():
            if p < len(l) and l[p] != "":
                out[c][l[0]] = float(l[p])
    return out


def ler_horario():
    """{albufeira: {dia: % das 23:00}} de todos os ficheiros do histórico do InfoÁgua."""
    out = {}
    for caminho in sorted(glob.glob(PADRAO_HORARIO)):
        with open(caminho, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["data"][11:16] == HORA and r.get("volume_pct") not in (None, ""):
                    out.setdefault(r["albufeira"], {})[r["data"][:10]] = float(r["volume_pct"])
    return out


def num(v, casas=2):
    return f"{v:.{casas}f}"


def main():
    lista = ler_lista()
    diarias = [a for a in lista if a.get("frequencia") == "diária"]
    snirh = ler_diario([a["coluna"] for a in diarias])
    p23 = ler_horario()

    linhas, fora = [], []
    for a in diarias:
        col = a["coluna"]
        pct, val = p23.get(col), snirh.get(col)
        if not pct or not val:
            continue
        ult = max(val)
        comuns = sorted(d for d in pct if d in val and pct[d] > 0)[-DIAS_CALIBRACAO:]
        if len(comuns) < MIN_DIAS:
            fora.append(f"{col}: só {len(comuns)} dia(s) em comum com o SNIRH")
            continue
        cap = statistics.median(val[d] / (pct[d] / 100) for d in comuns)
        ref = float(a.get("capacidade_hm3") or a.get("referencia_hm3") or 0)
        if ref > 0 and abs(cap / ref - 1) > DESVIO_MAX:
            fora.append(f"{col}: capacidade calculada {cap:.1f} hm3, longe dos {ref:.1f} hm3 da lista")
            continue
        for d in sorted(x for x in pct if x > ult):
            linhas.append([d, col, num(pct[d] / 100 * cap), num(pct[d]), num(cap)])

    linhas.sort(key=lambda l: l[0])   # estável: dentro de cada dia, fica a ordem do albufeiras_snirh.csv
    texto = ",".join(COLUNAS) + "\n" + "".join(",".join(l) + "\n" for l in linhas)
    antigo = None
    if os.path.exists(CAMINHO_SAIDA):
        with open(CAMINHO_SAIDA, encoding="utf-8", newline="") as f:
            antigo = f.read()

    dias = sorted({l[0] for l in linhas})
    if linhas:
        print(f"Provisórios: {len({l[1] for l in linhas})} albufeiras, {len(dias)} dia(s), de {dias[0]} a {dias[-1]}")
    else:
        print("Provisórios: nenhum (o SNIRH está em dia nas albufeiras com InfoÁgua)")
    for x in fora:
        print(f"  de fora — {x}")

    if texto == antigo:
        print(f"= {os.path.relpath(CAMINHO_SAIDA, ROOT_DIR)} sem alterações")
        return 0
    with open(CAMINHO_SAIDA, "w", encoding="utf-8", newline="") as f:
        f.write(texto)
    print(f"✓ {os.path.relpath(CAMINHO_SAIDA, ROOT_DIR)} ({len(linhas)} linhas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
