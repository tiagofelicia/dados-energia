#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_mibgas_futuros.py
Publica os produtos a prazo do MIBGAS em data/gas/mibgas_futuros.csv — a curva
forward do gás ibérico, em preço absoluto.

  Fonte: aba "Trading Data PVB&VTP" do MIBGAS_Data_<ANO>.xlsx
  Série: 2015 → hoje (a aba mudou de nome três vezes; ver LER_ABA)

PORQUE FALTAVA
--------------
O atualizar_mibgas_historico.py publica os ÍNDICES (aba "MIBGAS Indexes"), que
são só spot day-ahead. Os produtos a prazo — mensais, trimestrais, estações e
anos — estão noutra aba do MESMO ficheiro, que já era descarregado e nunca era
lido. Acrescentá-los não custa um único pedido de rede a mais.

Fecha também a simetria que já existe na eletricidade:

    omie_dados_atuais.csv  (spot)   ·  futuros_omip.csv       (futuros)
    mibgas_spot.csv        (spot)   ·  mibgas_futuros.csv     (futuros)

A COLUNA 'rotulo'
-----------------
O MIBGAS identifica os produtos por posição relativa — GMES_M+2 é "o segundo
mês a contar de agora", e o que isso significa muda todos os meses. Para ler
uma série histórica é preciso saber a que período de entrega cada cotação se
referia.

Esta coluna resolve isso, derivada de First/Last Day Delivery:

    GMES_M+2   2025-05-01 → 2025-05-31   ->  "Maio 2025"
    GQES_Q+1   2025-07-01 → 2025-09-30   ->  "3.º Trimestre 2025"
    GSES_W     2025-10-01 → 2026-03-31   ->  "Inverno 2025/26"
    GYES_Y+1   2026-01-01 → 2026-12-31   ->  "2026"

É derivada das datas e não de uma tabela de correspondências: uma tabela teria
de ser reescrita a cada ano, e é exactamente o trabalho que esta coluna existe
para evitar a quem consome.

PREÇOS: DOIS, NÃO UM
--------------------
  preco_ultimo      Last Price — sinal de fecho da sessão. Segue a metodologia
                    do Last Price do MIBGAS, que filtra dados não significativos
                    e estima o valor quando não houve liquidez.
  preco_referencia  Reference Price — média ponderada de todas as transações da
                    sessão. Mais robusto, mas menos representativo do fecho.

Medido em 2025: diferem em média 0,19 EUR/MWh (máximo 7,36), e há 6 745 linhas
com Reference contra 6 742 com Last. Publicam-se os dois, como já se faz no
mibgas_spot.csv com vtp_last/vtp_avg — a escolha é de quem usa.

USO
---
  python atualizar_mibgas_futuros.py              # ano corrente + anterior
  python atualizar_mibgas_futuros.py --backfill   # desde 2015
  python atualizar_mibgas_futuros.py --anos 2025
  python atualizar_mibgas_futuros.py --cache DIR  # xlsx locais, sem rede
"""

import argparse
import os
import sys
from datetime import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from atualizar_mibgas_historico import (  # noqa: E402
    PASTA_GAS,
    descarregar,
    _achar,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

FICHEIRO_CSV = os.path.join(PASTA_GAS, "mibgas_futuros.csv")
ANO_INICIAL = 2015

# A aba mudou de nome três vezes desde 2015. Procura-se por ordem.
NOMES_ABA = ["Trading Data PVB&VTP", "Trading Data PVB", "Trading Data"]

COLUNAS = ["dia", "data_iso", "produto", "area", "horizonte", "rotulo",
           "entrega_inicio", "entrega_fim", "dias_entrega",
           "preco_ultimo", "preco_referencia", "volume_mwh"]

MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho",
         "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"]

ORDEM_HORIZONTE = ["intradiario", "dia", "fim-de-semana", "resto-do-mes",
                   "mes", "trimestre", "estacao", "ano", "outro"]


def ler_aba(conteudo, ano):
    """Lê a aba de trading do xlsx anual, seja qual for o nome que tenha."""
    buf = pd.io.common.BytesIO(conteudo)
    abas = pd.ExcelFile(buf).sheet_names
    nome = next((n for n in NOMES_ABA if n in abas), None)
    if nome is None:
        raise ValueError(f"{ano}: nenhuma aba de trading em {abas}")
    return pd.read_excel(buf, sheet_name=nome), nome


# O código do produto declara o tipo — é a fonte a dizê-lo, não uma dedução
# nossa a partir das datas. Deduzir pela duração falhava nos fins de semana
# prolongados por feriado, que começam à quinta em vez de sexta (Corpus Christi
# de 2022, Natal de 2025) e caíam todos em "outro".
#
# (prefixo, horizonte, duração esperada em dias) — a duração serve só para
# validar: se a fonte mudar de formato, o desvio aparece no log em vez de
# passar despercebido. Ordem importa: GMA antes de GM.
FAMILIAS = [
    ("GBoM", "resto-do-mes",  None),      # Balance of Month: duração irregular
    ("GWD",  "intradiario",   (1, 1)),    # Within-Day: entrega no próprio dia
    ("GWE",  "fim-de-semana", (2, 4)),
    ("GDA",  "dia",           (1, 1)),    # Day-Ahead: D+1 a D+3
    ("GMA",  "mes",           (28, 31)),
    ("GM",   "mes",           (28, 31)),
    ("GQ",   "trimestre",     (90, 92)),
    ("GS",   "estacao",       (182, 183)),
    ("GY",   "ano",           (365, 366)),
]


def classificar(produto, ini, fim):
    """Devolve (horizonte, rotulo): o horizonte vem do código do produto, o
    rótulo do período de entrega."""
    p = str(produto).strip()
    if pd.isna(ini) or pd.isna(fim):
        return "outro", None
    dias = (fim - ini).days + 1

    horizonte = limites = None
    for prefixo, nome, esperado in FAMILIAS:
        if p.startswith(prefixo):
            horizonte, limites = nome, esperado
            break
    if horizonte is None:
        print(f"  ! produto sem família conhecida: {p}")
        return "outro", f"{ini.strftime('%d/%m/%Y')}–{fim.strftime('%d/%m/%Y')}"
    if limites and not limites[0] <= dias <= limites[1]:
        print(f"  ! {p} {ini.date()}→{fim.date()}: {dias} dias, esperados "
              f"{limites[0]}-{limites[1]} para {horizonte}")

    if horizonte in ("dia", "intradiario"):
        return horizonte, ini.strftime("%d/%m/%Y")
    if horizonte == "fim-de-semana":
        return horizonte, f"Fim de semana {ini.strftime('%d/%m/%Y')}"
    if horizonte == "resto-do-mes":
        return horizonte, f"Resto de {MESES[ini.month - 1]} {ini.year}"
    if horizonte == "mes":
        return horizonte, f"{MESES[ini.month - 1]} {ini.year}"
    if horizonte == "trimestre":
        return horizonte, f"{(ini.month - 1) // 3 + 1}.º Trimestre {ini.year}"
    if horizonte == "estacao":
        # Verão: abril a setembro. Inverno: outubro a março do ano seguinte —
        # daí o rótulo com os dois anos.
        if ini.month == 4:
            return horizonte, f"Verão {ini.year}"
        if ini.month == 10:
            return horizonte, f"Inverno {ini.year}/{str(ini.year + 1)[-2:]}"
        return horizonte, f"Estação {ini.strftime('%m/%Y')}"
    return horizonte, str(ini.year)


def ler_ano(conteudo, ano):
    df, nome_aba = ler_aba(conteudo, ano)
    c_dia = _achar(df, "trading day")
    c_prod = _achar(df, "product")
    c_ini = _achar(df, "first day delivery")
    c_fim = _achar(df, "last day delivery")
    c_ref = _achar(df, "reference price")
    c_last = _achar(df, "last price")
    c_area = _achar(df, "area")
    c_vol = _achar(df, "volume traded", excluir=("auction",))

    falta = [n for n, c in (("Trading day", c_dia), ("Product", c_prod),
                            ("First Day Delivery", c_ini)) if c is None]
    if falta:
        raise ValueError(f"{ano}: colunas em falta em '{nome_aba}': {falta}")

    out = pd.DataFrame({
        "_dt": pd.to_datetime(df[c_dia], errors="coerce"),
        "produto": df[c_prod].astype(str).str.strip(),
        "_ini": pd.to_datetime(df[c_ini], errors="coerce"),
        "_fim": pd.to_datetime(df[c_fim], errors="coerce") if c_fim else pd.NaT,
        "area": (df[c_area].astype(str).str.strip().str.upper()
                 if c_area else ""),
        "preco_ultimo": pd.to_numeric(df[c_last], errors="coerce") if c_last else pd.NA,
        "preco_referencia": pd.to_numeric(df[c_ref], errors="coerce") if c_ref else pd.NA,
        "volume_mwh": pd.to_numeric(df[c_vol], errors="coerce") if c_vol else pd.NA,
    })
    # Uma linha sem nenhum dos dois preços não é uma cotação.
    return out.dropna(subset=["_dt"]).loc[
        out["preco_ultimo"].notna() | out["preco_referencia"].notna()]


def ordenar(df):
    """Ordem canónica do ficheiro: por sessão e, dentro dela, do horizonte mais
    curto para o mais longo.

    Tem de ser a MESMA no backfill e no incremental. Quando diferiam, cada
    corrida reescrevia o ficheiro inteiro sem nada ter mudado nos dados e
    produzia um commit por dia com 35 mil linhas de ruído.

    Opera sobre as colunas já formatadas: data_iso e entrega_inicio estão em
    AAAA-MM-DD, que ordena correctamente como texto.

    A chave (data_iso, produto, entrega_inicio) é a única que identifica uma
    linha: GSES_W aparece duas vezes na mesma sessão, uma por cada inverno
    cotado.
    """
    ordem = {h: i for i, h in enumerate(ORDEM_HORIZONTE)}
    return (df.assign(_o=df["horizonte"].map(ordem).fillna(99))
              .sort_values(["data_iso", "_o", "produto", "entrega_inicio"],
                           kind="stable")
              .drop(columns="_o")
              .reset_index(drop=True))


def formatar(df):
    df = df[df["_dt"].dt.year >= ANO_INICIAL].copy()
    df = df.drop_duplicates(subset=["_dt", "produto", "_ini"], keep="last")

    cls = [classificar(p, i, f) for p, i, f in
           zip(df["produto"], df["_ini"], df["_fim"])]
    df["horizonte"] = [c[0] for c in cls]
    df["rotulo"] = [c[1] for c in cls]

    out = pd.DataFrame({
        "dia": df["_dt"].dt.strftime("%d/%m/%Y"),
        "data_iso": df["_dt"].dt.strftime("%Y-%m-%d"),
        "produto": df["produto"],
        "area": df["area"],
        "horizonte": df["horizonte"],
        "rotulo": df["rotulo"],
        "entrega_inicio": df["_ini"].dt.strftime("%Y-%m-%d"),
        "entrega_fim": df["_fim"].dt.strftime("%Y-%m-%d"),
        # Tipos fixados à mão de propósito. Sem isto, o dtype sai do conteúdo:
        # no incremental só se carregam dois anos e, se nesses anos todos os
        # volumes forem inteiros, a coluna vem int64 e escreve "61685"; no
        # backfill vem float64 e escreve "61685.0". Mesmos dados, texto
        # diferente — e o ficheiro inteiro era reescrito a cada corrida.
        "dias_entrega": ((df["_fim"] - df["_ini"]).dt.days + 1).astype("Int64"),
        "preco_ultimo": pd.to_numeric(df["preco_ultimo"], errors="coerce")
                          .round(3).astype("float64"),
        "preco_referencia": pd.to_numeric(df["preco_referencia"], errors="coerce")
                              .round(3).astype("float64"),
        "volume_mwh": pd.to_numeric(df["volume_mwh"], errors="coerce")
                        .round(1).astype("float64"),
    })
    return ordenar(out)


def gravar(df):
    os.makedirs(PASTA_GAS, exist_ok=True)
    novo = df.to_csv(index=False, encoding="utf-8", lineterminator="\n")
    antigo = None
    if os.path.exists(FICHEIRO_CSV):
        with open(FICHEIRO_CSV, "r", encoding="utf-8-sig", newline="") as f:
            antigo = f.read()
    if novo != antigo:
        with open(FICHEIRO_CSV, "w", encoding="utf-8-sig", newline="") as f:
            f.write(novo)
        return True
    return False


def main():
    p = argparse.ArgumentParser(
        description="Publica a curva forward do gás ibérico (MIBGAS).")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--anos", nargs="+", type=int)
    p.add_argument("--cache", metavar="DIR")
    args = p.parse_args()

    ano_atual = datetime.now().year
    existe = os.path.exists(FICHEIRO_CSV)
    if args.anos:
        anos = sorted(args.anos)
    elif args.backfill or not existe:
        if not existe and not args.backfill:
            print("ℹ️  CSV ainda não existe — a fazer backfill completo.")
        anos = list(range(ANO_INICIAL, ano_atual + 1))
    else:
        anos = [ano_atual - 1, ano_atual]

    print(f"🔥 Futuros MIBGAS — {len(anos)} ano(s): {anos[0]}–{anos[-1]}")
    partes, falhados = [], []
    for ano in anos:
        try:
            partes.append(ler_ano(descarregar(ano, args.cache), ano))
        except Exception as e:
            falhados.append((ano, e))
            print(f"  ❌ {ano}: {e}")
    if not partes:
        print("❌ Nenhum ano foi lido com sucesso.")
        sys.exit(1)

    novo = formatar(pd.concat(partes, ignore_index=True))

    if existe and not args.backfill:
        antigo = pd.read_csv(FICHEIRO_CSV, encoding="utf-8-sig", dtype=str)
        antes = len(antigo)
        j = pd.concat([antigo[COLUNAS], novo.astype(str)], ignore_index=True)
        j = j.drop_duplicates(subset=["data_iso", "produto", "entrega_inicio"],
                              keep="last")
        final = ordenar(j)
        print(f"   {antes} linhas + {len(novo)} recolhidas "
              f"→ {len(final)} ({len(final) - antes:+d})")
    else:
        final = novo

    mudou = gravar(final)
    print(f"\n✅ {FICHEIRO_CSV}"
          f"{'' if mudou else '  (sem alterações — não reescrito)'}")
    print(f"   {len(final):,} linhas · {final['data_iso'].nunique():,} sessões · "
          f"{final['data_iso'].min()} → {final['data_iso'].max()}")
    print(f"   {final['produto'].nunique()} produtos · horizontes: "
          + ", ".join(f"{h}={n}" for h, n in
                      final['horizonte'].value_counts().items()))

    if falhados:
        print(f"\n❌ {len(falhados)} ano(s) falharam: "
              f"{', '.join(str(a) for a, _ in falhados)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
