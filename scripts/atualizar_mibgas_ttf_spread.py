#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_mibgas_ttf_spread.py
Publica o spread PVB-TTF do MIBGAS em data/gas/mibgas_ttf_spread.csv — o prémio
(ou desconto) do gás ibérico face ao benchmark europeu, da sessão seguinte até
dois anos à frente.

  Fonte: aba "Trading Data PVB-TTF" do MIBGAS_Data_<ANO>.xlsx
  Série: 02/01/2024 → hoje (a aba existe desde 2023, mas 2023 só tem 3 dias
         de dados de teste, descartados pelo filtro de ano)

O QUE É ESTE PRODUTO
--------------------
Os produtos PVB-TTF são contratos de gás na zona espanhola cotados como
DIFERENCIAL. A definição oficial do MIBGAS (aba "Definitions"):

    PVB_TTF_D+1 — Daily Product Indexed to the TTF Price Assessment Day-Ahead
    (published in ICIS European Spot Gas Markets) in the Spanish zone

Ou seja: quem compra paga (TTF assessment da ICIS) + spread. Um spread positivo
significa gás ibérico mais caro que o benchmark europeu; negativo, mais barato.

PORQUÊ PUBLICAR O SPREAD E NÃO O TTF
------------------------------------
O TTF em si é um índice proprietário (ICE Endex) e o assessment day-ahead que
este produto referencia é da ICIS — ambos comerciais, nenhum republicável sob
a CC BY 4.0 deste repositório. O spread, esse, é publicado pelo MIBGAS nos
mesmos termos dos restantes dados que já usamos.

E para um leitor português o spread responde melhor à pergunta real: quanto é
que o gás ibérico está acima ou abaixo da Europa.

A COLUNA ttf_derivado
---------------------
Calculada apenas para o produto D+1, como:

    ttf_derivado = pvb_last(dia de entrega) - spread(dia de negociação)

onde pvb_last vem do mibgas_spot.csv. Só D+1 porque só aí os dois valores são
fixados no MESMO momento: o spread D+1 é cotado no dia T para entrega T+1, e o
índice PVB day-ahead de T+1 é fixado nessa mesma sessão T. Para D+2, D+3 e os
produtos mensais/trimestrais/anuais não existe índice PVB correspondente fixado
no mesmo instante, e a subtração compararia momentos diferentes — por isso esses
produtos saem só com o spread.

EXATIDÃO (verificado, não presumido)
------------------------------------
  • ruído metodológico (derivar com pvb_last vs pvb_avg):
        média 0,37 · p95 1,07 · máx 2,95 EUR/MWh
  • coerência entre produtos da mesma entrega, em 397 dias:
        |D+1 - D+2| média 0,28 · p95 0,78
        |D+1 - D+3| média 0,37 · p95 1,02
  • contra as médias trimestrais da DG ENER (Quarterly Report on European
    Gas Markets):
        2024-Q2  oficial ~32   derivado 31,7
        2025-Q1  oficial ~46   derivado 46,9
        2025-Q2  oficial  35   derivado 35,6

Conclusão: serve para análise de tendências e comparação de mercados, com
incerteza da ordem de 1 EUR/MWh. NÃO serve para liquidação contratual nem como
substituto do assessment oficial — quem precisa do TTF oficial vai à ICE/ICIS.

USO
---
  python atualizar_mibgas_ttf_spread.py              # ano corrente + anterior
  python atualizar_mibgas_ttf_spread.py --backfill   # desde 2024
  python atualizar_mibgas_ttf_spread.py --anos 2025
  python atualizar_mibgas_ttf_spread.py --cache DIR  # xlsx locais, sem rede

SAÍDA
-----
  data/gas/mibgas_ttf_spread.csv   uma linha por dia de negociação × produto

Só há linhas em dias de sessão do MIBGAS (~255/ano): fins de semana e feriados
não têm cotação. Isto é a realidade do mercado, não uma lacuna de recolha.
"""

import argparse
import os
import sys
from datetime import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# Partilha o download, os headers e o retry com o script dos índices, em vez de
# os duplicar — é o mesmo ficheiro anual que se descarrega.
from atualizar_mibgas_historico import (  # noqa: E402
    PASTA_GAS,
    FICHEIRO_CSV as FICHEIRO_SPOT,
    descarregar,
    _achar,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

FICHEIRO_CSV = os.path.join(PASTA_GAS, "mibgas_ttf_spread.csv")
ABA = "Trading Data PVB-TTF"

# 2023 tem a aba mas só 3 dias soltos (12/09, e dois em dezembro) — arranque
# de testes, sem série. A série contínua começa em 02/01/2024.
ANO_INICIAL = 2024

COLUNAS = ["dia", "data_iso", "produto", "entrega_inicio", "entrega_fim",
           "spread", "ttf_derivado"]

# Ordem de leitura, da sessão seguinte ao horizonte mais longo. Serve para
# ordenar o CSV de forma estável dentro de cada dia.
ORDEM_PRODUTOS = ["D+1", "D+2", "D+3", "W", "BoM", "M+1", "M+2", "M+3",
                  "Q+1", "Q+2", "Q+3", "Q+4", "S", "Y+1", "Y+2"]


def ler_ano(conteudo, ano):
    """Normaliza a aba PVB-TTF de um ano. Devolve DataFrame com _dt e produto."""
    df = pd.read_excel(pd.io.common.BytesIO(conteudo), sheet_name=ABA)

    col_dia = _achar(df, "trading day")
    col_ini = _achar(df, "first day delivery")
    col_fim = _achar(df, "last day delivery")
    col_ref = _achar(df, "reference price")
    col_prod = _achar(df, "product")
    em_falta = [n for n, c in (("Trading day", col_dia), ("Product", col_prod),
                               ("Reference Price", col_ref)) if c is None]
    if em_falta:
        raise ValueError(f"{ano}: colunas em falta na aba {ABA}: {em_falta}")

    out = pd.DataFrame({
        "_dt": pd.to_datetime(df[col_dia], errors="coerce"),
        "produto": (df[col_prod].astype(str)
                    .str.replace("PVB-TTF_", "", regex=False).str.strip()),
        "_ini": pd.to_datetime(df[col_ini], errors="coerce") if col_ini else pd.NaT,
        "_fim": pd.to_datetime(df[col_fim], errors="coerce") if col_fim else pd.NaT,
        "spread": pd.to_numeric(df[col_ref], errors="coerce"),
    })
    # Sem preço de referência não há cotação nesse produto/dia — não é linha.
    return out.dropna(subset=["_dt", "spread"])


def juntar_ttf(df):
    """Acrescenta ttf_derivado às linhas D+1, a partir do mibgas_spot.csv."""
    df["ttf_derivado"] = pd.NA

    if not os.path.exists(FICHEIRO_SPOT):
        print(f"  ⚠️  {os.path.basename(FICHEIRO_SPOT)} não existe — "
              f"ttf_derivado fica vazio. Corre primeiro o "
              f"atualizar_mibgas_historico.py.")
        return df

    spot = pd.read_csv(FICHEIRO_SPOT, encoding="utf-8-sig")
    if "pvb_last" not in spot.columns:
        print("  ⚠️  mibgas_spot.csv sem coluna pvb_last — ttf_derivado vazio.")
        return df

    pvb = (spot.assign(_e=pd.to_datetime(spot["data_iso"]))
               .dropna(subset=["pvb_last"])
               .set_index("_e")["pvb_last"])

    # Só D+1: é o único produto cujo spread e cujo índice PVB são fixados na
    # mesma sessão (ver docstring).
    d1 = df["produto"].eq("D+1") & df["_ini"].notna()
    df.loc[d1, "ttf_derivado"] = (
        df.loc[d1, "_ini"].map(pvb) - df.loc[d1, "spread"]
    )
    n = int(df.loc[d1, "ttf_derivado"].notna().sum())
    print(f"  🔗 ttf_derivado calculado para {n} dias D+1")
    return df


def ordenar(df):
    """Ordena por dia e, dentro do dia, pelo horizonte do produto.

    Tem de ser a MESMA ordenação no backfill e no merge incremental: ordenar um
    por horizonte e o outro alfabeticamente faria as linhas trocar de sítio a
    cada corrida, gerando um diff completo do ficheiro sem dados novos.
    """
    ordem = {p: i for i, p in enumerate(ORDEM_PRODUTOS)}
    return (df.assign(_ord=df["produto"].map(ordem).fillna(99))
              .sort_values(["data_iso", "_ord", "produto", "entrega_inicio"],
                           kind="stable")
              .drop(columns="_ord")
              .reset_index(drop=True))


def formatar(df):
    df = df[df["_dt"].dt.year >= ANO_INICIAL].copy()
    # A chave tem de incluir a entrega. Sem ela, "W" e "S" — que cotam duas
    # estações em simultâneo, o inverno que vem e o seguinte — colapsavam numa
    # linha só e perdiam-se 684 cotações (6 % do ficheiro), sempre a mais
    # próxima, que é justamente a que o MIBGAS mostra na página.
    df = df.drop_duplicates(subset=["_dt", "produto", "_ini"], keep="last")

    out = pd.DataFrame({
        "dia": df["_dt"].dt.strftime("%d/%m/%Y"),
        "data_iso": df["_dt"].dt.strftime("%Y-%m-%d"),
        "produto": df["produto"],
        "entrega_inicio": df["_ini"].dt.strftime("%Y-%m-%d"),
        "entrega_fim": df["_fim"].dt.strftime("%Y-%m-%d"),
        "spread": pd.to_numeric(df["spread"], errors="coerce").round(3),
        "ttf_derivado": pd.to_numeric(df["ttf_derivado"], errors="coerce").round(2),
    })
    return ordenar(out)


def gravar(df):
    """Escreve só se mudou — evita um commit por corrida sem dados novos."""
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
        description="Publica o spread PVB-TTF do MIBGAS como CSV aberto.")
    p.add_argument("--backfill", action="store_true",
                   help=f"reconstrói desde {ANO_INICIAL}")
    p.add_argument("--anos", nargs="+", type=int)
    p.add_argument("--cache", metavar="DIR",
                   help="ler os xlsx de um diretório local em vez da rede")
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

    print(f"🔥 Spread PVB-TTF — a processar {len(anos)} ano(s): "
          f"{anos[0]}–{anos[-1]}")

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

    novo = formatar(juntar_ttf(pd.concat(partes, ignore_index=True)))

    if existe and not args.backfill:
        antigo = pd.read_csv(FICHEIRO_CSV, encoding="utf-8-sig",
                             dtype={"dia": str, "data_iso": str,
                                    "entrega_inicio": str, "entrega_fim": str})
        antes = len(antigo)
        j = pd.concat([antigo[COLUNAS], novo], ignore_index=True)
        j = j.drop_duplicates(subset=["data_iso", "produto", "entrega_inicio"],
                              keep="last")
        final = ordenar(j)
        print(f"   {antes} linhas + {len(novo)} recolhidas "
              f"→ {len(final)} ({len(final) - antes:+d})")
    else:
        final = novo

    mudou = gravar(final)

    dias = final["data_iso"].nunique()
    print(f"\n✅ {FICHEIRO_CSV}"
          f"{'' if mudou else '  (sem alterações — não reescrito)'}")
    print(f"   {len(final)} linhas · {dias} dias de sessão · "
          f"{final['data_iso'].min()} → {final['data_iso'].max()}")
    print(f"   produtos: {final['produto'].nunique()}")
    d1 = final[final["produto"] == "D+1"]
    if len(d1):
        print(f"   spread D+1: média {d1['spread'].mean():+.2f} · "
              f"min {d1['spread'].min():+.2f} · máx {d1['spread'].max():+.2f} EUR/MWh")

    if falhados:
        print(f"\n❌ {len(falhados)} ano(s) falharam: "
              f"{', '.join(str(a) for a, _ in falhados)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
