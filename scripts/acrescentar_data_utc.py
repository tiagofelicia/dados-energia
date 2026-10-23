#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
acrescentar_data_utc.py
Acrescenta a coluna 'data_utc' aos históricos anuais do OMIE e da produção
(data/omie/historico/omie_historico_AAAA.csv e data/producao/historico/producao_historico_AAAA.csv).

Corre-se uma vez. Os ficheiros do ano corrente já a trazem dos scripts que os geram
(atualizar_omie_dados_atuais.py e atualizar_producao_REN.py) e levam-na para o
histórico no fecho do ano.

'data_utc' é o início do intervalo em UTC, 'AAAA-MM-DD HH:MM' (o formato do erc/isp).
Calcula-se a partir do 'dia' e do início do 'intervalo'. Na hora que se repete no último
domingo de outubro, a 1.ª ocorrência de cada etiqueta no dia é a de verão (UTC+1) e a
2.ª a de inverno (UTC+0).

Isso pressupõe as linhas pela ordem em que aconteceram. Nos históricos da produção de
2024 e 2025 não estão: o atualizar_producao_REN.py ordenava pela hora local e intercalava
as duas passagens (e em 27/10/2024 o par das 01:45 ficou ao contrário). Quando um dia da
produção não fica por ordem, o script pede esse dia à REN e atribui cada linha à passagem
cujos valores mais se aproximam. Os valores gravados não mudam, só a ordem. No OMIE um
dia fora de ordem é erro.

Tudo o resto fica byte a byte: BOM, fins de linha e o texto de cada campo. Um ficheiro
que já tenha a coluna é saltado.

Uso:
  python scripts/acrescentar_data_utc.py                  # todos os históricos
  python scripts/acrescentar_data_utc.py FICHEIRO...      # só estes
  python scripts/acrescentar_data_utc.py --verificar      # não grava; só diz o que faria
"""

import argparse
import glob
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)  # para importar o atualizar_producao_REN.py ao lado

FUSO_PT = ZoneInfo("Europe/Lisbon")
QUARTO = timedelta(minutes=15)
FORMATO_UTC = "%Y-%m-%d %H:%M"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


def instante_utc(dia, inicio, ocorrencia):
    """
    Instante UTC de 'dia' (DD/MM/AAAA) às 'inicio' (HH:MM, hora de Portugal), na 1.ª
    (ocorrencia=0) ou 2.ª (1) passagem por essa hora. None se a hora não existe (a que
    salta em março) ou se não se repete e se pediu a 2.ª.
    """
    d = datetime.strptime(f"{dia} {inicio}", "%d/%m/%Y %H:%M")
    if ocorrencia not in (0, 1):
        return None
    utc = d.replace(tzinfo=FUSO_PT, fold=ocorrencia).astimezone(timezone.utc)
    volta = utc.astimezone(FUSO_PT)
    if volta.replace(tzinfo=None) != d or volta.fold != ocorrencia:
        return None
    return utc


def instantes_pelas_etiquetas(dia, linhas):
    """[(linha, inicio)] de um dia → lista de instantes UTC (None onde não dá)."""
    vistos = defaultdict(int)
    out = []
    for _, inicio in linhas:
        out.append(instante_utc(dia, inicio, vistos[inicio]))
        vistos[inicio] += 1
    return out


def por_ordem(instantes):
    return all(u is not None for u in instantes) and \
        all(a < b for a, b in zip(instantes, instantes[1:]))


def num(txt):
    try:
        v = float(txt)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def reordenar_com_ren(dia, linhas, cabecalho):
    """
    Dia da produção fora de ordem: pede-o à REN (que vem por ordem) e, em cada hora que
    se repete, dá a cada uma das duas linhas gravadas a passagem cujos valores mais se
    aproximam. Devolve [(linha, instante)] ordenado pelo instante.
    """
    from atualizar_producao_REN import buscar_dados_ren, COL_GAS_CC, COL_GAS_COG

    d_iso = datetime.strptime(dia, "%d/%m/%Y").strftime("%Y-%m-%d")
    df = buscar_dados_ren(d_iso, d_iso)
    if df is None:
        sys.exit(f"[ERR] {dia}: a REN não respondeu; sem ela não se sabe a ordem da hora repetida.")

    # O gás fica de fora: o export atual da REN junta as duas parcelas numa só coluna
    fora = ("dia", "hora", "intervalo", COL_GAS_CC, COL_GAS_COG)
    cols = [c for c in cabecalho if c in df.columns and c not in fora]
    idx = [cabecalho.index(c) for c in cols]
    ren = defaultdict(list)  # 'HH:MM' → [(instante, valores)] pela ordem da REN
    for _, r in df.iterrows():
        inicio = r["datetime"].strftime("%H:%M")
        ut = instante_utc(dia, inicio, len(ren[inicio]))
        ren[inicio].append((ut, [num(str(r[c])) for c in cols]))

    def distancia(campos, valores):
        return sum(abs(num(campos[i]) - v) for i, v in zip(idx, valores)
                   if v is not None and num(campos[i]) is not None)

    grupos = defaultdict(list)
    for linha, inicio in linhas:
        grupos[inicio].append(linha)

    resultado = []
    for inicio, gravadas in grupos.items():
        cand = ren.get(inicio, [])
        if len(gravadas) == 1:
            resultado.append((gravadas[0], instante_utc(dia, inicio, 0)))
            continue
        if len(gravadas) != 2 or len(cand) != 2:
            sys.exit(f"[ERR] {dia} {inicio}: {len(gravadas)} linhas gravadas e {len(cand)} na REN.")
        a, b = (l.split(",") for l in gravadas)
        direto = distancia(a, cand[0][1]) + distancia(b, cand[1][1])
        trocado = distancia(a, cand[1][1]) + distancia(b, cand[0][1])
        if min(direto, trocado) * 2 >= max(direto, trocado):
            sys.exit(f"[ERR] {dia} {inicio}: os valores não decidem a ordem "
                     f"(distâncias {direto:.1f} e {trocado:.1f}).")
        if direto <= trocado:
            resultado += [(gravadas[0], cand[0][0]), (gravadas[1], cand[1][0])]
        else:
            resultado += [(gravadas[0], cand[1][0]), (gravadas[1], cand[0][0])]
        print(f"    {dia} {inicio}: {'por ordem' if direto <= trocado else 'TROCADAS'} "
              f"(distância {min(direto, trocado):.1f} contra {max(direto, trocado):.1f})")

    if any(u is None for _, u in resultado):
        sys.exit(f"[ERR] {dia}: há linhas sem instante depois de usar a REN.")
    return sorted(resultado, key=lambda p: p[1])


def processar(caminho, verificar):
    nome = os.path.basename(caminho)
    bruto = open(caminho, "rb").read()
    bom = bruto.startswith(b"\xef\xbb\xbf")
    texto = bruto.decode("utf-8-sig")
    linhas = texto.split("\n")
    if linhas and linhas[-1] == "":
        linhas.pop()
    else:
        sys.exit(f"[ERR] {nome}: não acaba em fim de linha.")
    eol = "\r\n" if linhas[0].endswith("\r") else "\n"
    linhas = [l[:-1] if l.endswith("\r") else l for l in linhas]

    cabecalho = linhas[0].split(",")
    if "data_utc" in cabecalho:
        print(f"[=] {nome}: já tem data_utc — saltado.")
        return
    i_dia, i_int = cabecalho.index("dia"), cabecalho.index("intervalo")
    e_producao = nome.startswith("producao_")

    dias, por_dia = [], defaultdict(list)
    for l in linhas[1:]:
        c = l.split(",")
        dia = c[i_dia]
        if dia not in por_dia:
            dias.append(dia)
        elif dias[-1] != dia:
            sys.exit(f"[ERR] {nome}: o dia {dia} aparece em dois blocos separados.")
        por_dia[dia].append((l, c[i_int][1:6]))

    saida, reordenados, contagens = [], [], defaultdict(list)
    for dia in dias:
        instantes = instantes_pelas_etiquetas(dia, por_dia[dia])
        if por_ordem(instantes):
            pares = [(l, u) for (l, _), u in zip(por_dia[dia], instantes)]
        elif e_producao:
            print(f"  [!] {nome}: {dia} fora de ordem — a pedir à REN.")
            pares = reordenar_com_ren(dia, por_dia[dia], cabecalho)
            reordenados.append(dia)
        else:
            sys.exit(f"[ERR] {nome}: {dia} fora de ordem ou com horas que não existem.")
        if saida and pares[0][1] <= saida[-1][1]:
            sys.exit(f"[ERR] {nome}: {dia} começa antes de o dia anterior acabar.")
        saida += pares
        contagens[len(pares)].append(dia)

    # Conferências: instantes únicos e a subir, saltos de 15 min
    saltos = [(a[1], b[1]) for a, b in zip(saida, saida[1:]) if b[1] - a[1] != QUARTO]
    resumo = ", ".join(f"{n} linhas: {len(d)} dias" for n, d in sorted(contagens.items()))
    print(f"[OK] {nome}: {len(saida)} linhas ({resumo})"
          + (f"; reordenados pela REN: {', '.join(reordenados)}" if reordenados else ""))
    if saltos:
        a, b = saltos[0]
        print(f"  [!] {nome}: {len(saltos)} saltos diferentes de 15 min "
              f"(1.º: {a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M} UTC)")

    if verificar:
        return
    novo = [linhas[0] + ",data_utc"] + [f"{l},{u.strftime(FORMATO_UTC)}" for l, u in saida]
    dados = (eol.join(novo) + eol).encode("utf-8")
    with open(caminho, "wb") as f:
        f.write((b"\xef\xbb\xbf" if bom else b"") + dados)


def main():
    ap = argparse.ArgumentParser(description="Acrescenta data_utc aos históricos do OMIE e da produção.")
    ap.add_argument("ficheiros", nargs="*", help="por omissão, todos os históricos")
    ap.add_argument("--verificar", action="store_true", help="não grava; só diz o que faria")
    args = ap.parse_args()

    ficheiros = args.ficheiros or sorted(
        glob.glob(os.path.join(ROOT_DIR, "data", "omie", "historico", "omie_historico_*.csv"))
        + glob.glob(os.path.join(ROOT_DIR, "data", "producao", "historico", "producao_historico_*.csv")))
    if not ficheiros:
        sys.exit("[ERR] nenhum ficheiro encontrado.")
    for f in ficheiros:
        processar(f, args.verificar)


if __name__ == "__main__":
    main()
