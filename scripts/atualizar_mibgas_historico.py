#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_mibgas_historico.py
Publica os índices diários do MIBGAS (mercado ibérico de gás natural) como
dataset aberto em data/gas/mibgas_spot.csv.

  Fonte: https://www.mibgas.es/pt/file-access/MIBGAS_Data_<ANO>.xlsx
  Série: 16/12/2015 (arranque do MIBGAS) até ao dia seguinte (day-ahead).

CONTEXTO
--------
O atualizar_tarifarios_gas.py já lia esta fonte, mas apenas o ano corrente e o
anterior, apenas a coluna PVB (o hub espanhol), e fundia o resultado com futuros
OMIP e forward-fill numa única coluna 'Preço' dentro do xlsx do simulador. Isso
serve o simulador — que precisa de uma série contínua até 2028 — mas não serve
como dado aberto: quem lesse essa série não distinguiria preço real de futuro
nem de preenchimento.

Este script faz o oposto e complementa-o: só preços REAIS de mercado, sem
futuros, sem forward-fill, sem lacunas preenchidas. Um dia sem publicação fica
vazio, e é essa a informação correta.

A separação espelha a que já existe na eletricidade:
    omie_dados_atuais.csv  (real)  ·  futuros_omip.csv  (futuros)
    mibgas_spot.csv        (real)  ·  [futuros de gás, a fazer]

DOIS FORMATOS DE FONTE
----------------------
O ficheiro anual do MIBGAS mudou de estrutura em 2023 e este script lê ambos:

  2015-2022  aba "Indices"        formato LONGO — uma linha por dia × área,
                                  com coluna 'Area' (ES, e PT desde 16/03/2021)
  2023-2026  aba "MIBGAS Indexes" formato LARGO — uma linha por dia, um índice
                                  por coluna

As colunas são localizadas por PADRÃO e não por nome exato: o MIBGAS renomeou-as
várias vezes ('MIBGAS Index' → 'MIBGAS-ES Index' → 'MIBGAS-ES Index' + PVB/VTP).
Assumir um nome fixo partiria o script no próximo ano em que mudem.

ÍNDICES PUBLICADOS
------------------
  mibgas_pt   Índice de referência MIBGAS-PT           desde 16/03/2021
  mibgas_es   Índice de referência MIBGAS-ES           desde 16/12/2015
  vtp_last    VTP (hub português), última transação    desde 01/01/2023
  vtp_avg     VTP, preço médio ponderado               desde 01/01/2023
  pvb_last    PVB (hub espanhol), última transação     desde 01/01/2023
  pvb_avg     PVB, preço médio ponderado               desde 01/01/2023
  lng_es      GNL Espanha                              desde 2018 (esparso)
  avb_es      Armazenamento Espanha (AVB)              desde 2021

  Todos em EUR/MWh (PCS). Um campo vazio significa "não publicado nesse dia",
  nunca zero.

  Nota para quem indexa tarifários: PVB é o hub ESPANHOL e VTP o PORTUGUÊS.
  Em 2026 o mibgas_pt afastou-se do pvb_last em mais de 1 EUR/MWh em 117 de
  247 dias (máximo 9,16). Verifique qual é o índice que a fórmula do
  comercializador refere antes de escolher a coluna.

USO
---
  python atualizar_mibgas_historico.py              # ano corrente + anterior
  python atualizar_mibgas_historico.py --backfill   # desde 2015 (reconstrói tudo)
  python atualizar_mibgas_historico.py --anos 2023 2024
  python atualizar_mibgas_historico.py --cache DIR  # lê xlsx locais, sem rede

Modo default: descarrega os dois anos mais recentes e faz upsert por dia no CSV
existente — o MIBGAS republica índices com correções retroativas, por isso os
dias recentes são sempre re-pedidos. Se o CSV não existir, cai automaticamente
em --backfill.

SAÍDA
-----
  data/gas/mibgas_spot.csv       série diária, uma linha por dia
  data/gas/metadata.json         {"ultima_data", "ultima_atualizacao", "dias"}

Convenção de formato da casa: UTF-8 com BOM, separador ',', ponto decimal,
'dia' em DD/MM/AAAA. A coluna 'data_iso' (AAAA-MM-DD) é redundante de
propósito — é a que permite ordenar e fazer joins sem parse de datas.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO

import pandas as pd
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_GAS = os.path.join(ROOT_DIR, "data", "gas")
FICHEIRO_CSV = os.path.join(PASTA_GAS, "mibgas_spot.csv")
FICHEIRO_META = os.path.join(PASTA_GAS, "metadata.json")

URL_TEMPLATE = ("https://www.mibgas.es/pt/file-access/"
                "MIBGAS_Data_{ano}.xlsx?path=AGNO_{ano}/XLS")

# O MIBGAS arrancou a 16/12/2015.
ANO_INICIAL = 2015

# Sem o User-Agent de browser a fonte responde 403.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
}

TIMEOUT = 90
TENTATIVAS = 3

COLUNAS = ["dia", "data_iso", "mibgas_pt", "mibgas_es",
           "vtp_last", "vtp_avg", "pvb_last", "pvb_avg",
           "lng_es", "avb_es"]

PRECOS = [c for c in COLUNAS if c not in ("dia", "data_iso")]


# ============================================================
# Recolha
# ============================================================

def descarregar(ano, cache_dir=None):
    """Devolve os bytes do xlsx anual. Com --cache, lê do disco e não usa rede."""
    if cache_dir:
        caminho = os.path.join(cache_dir, f"MIBGAS_Data_{ano}.xlsx")
        if not os.path.exists(caminho):
            raise FileNotFoundError(f"não está em cache: {caminho}")
        print(f"  📁 {ano}: da cache local")
        return open(caminho, "rb").read()

    url = URL_TEMPLATE.format(ano=ano)
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            # verify=True: a cadeia TLS do mibgas.es valida sem problema.
            # (O atualizar_tarifarios_gas.py usa verify=False por inércia
            #  histórica; foi testado e já não é preciso.)
            resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            resp.raise_for_status()
            if len(resp.content) < 50_000:
                raise ValueError(f"resposta demasiado pequena "
                                 f"({len(resp.content)} bytes) — provável erro")
            print(f"  ⬇️  {ano}: {len(resp.content) // 1024} KB")
            return resp.content
        except Exception as e:
            ultimo_erro = e
            if tentativa < TENTATIVAS:
                espera = 2 ** tentativa
                print(f"  ⚠️  {ano}: tentativa {tentativa} falhou ({e}); "
                      f"nova tentativa em {espera}s")
                time.sleep(espera)
    raise RuntimeError(f"não foi possível obter o ano {ano}: {ultimo_erro}")


def _norm(col):
    """'MIBGAS-ES  LNG Index\\n[EUR/MWh]' -> 'mibgas-es lng index'."""
    s = re.sub(r"\s+", " ", str(col).replace("\n", " "))
    s = s.replace("[EUR/MWh]", "").replace("[MWh]", "")
    return s.strip().lower()


def _achar(df, *padroes, excluir=()):
    """Primeira coluna cujo nome normalizado contém TODOS os padrões."""
    for col in df.columns:
        n = _norm(col)
        if all(p in n for p in padroes) and not any(x in n for x in excluir):
            return col
    return None


def ler_ano(conteudo, ano):
    """Normaliza o xlsx de um ano para o schema COLUNAS. Devolve um DataFrame."""
    buf = BytesIO(conteudo)
    abas = pd.ExcelFile(buf).sheet_names

    if "MIBGAS Indexes" in abas:
        return _ler_largo(buf, ano)
    if "Indices" in abas:
        return _ler_longo(buf, ano)
    raise ValueError(f"{ano}: nenhuma aba de índices reconhecida em {abas}")


def _ler_largo(buf, ano):
    """2023+ — uma linha por dia, um índice por coluna."""
    df = pd.read_excel(buf, sheet_name="MIBGAS Indexes")
    col_dia = _achar(df, "delivery day")
    if col_dia is None:
        raise ValueError(f"{ano}: coluna 'Delivery day' não encontrada")

    mapa = {
        "mibgas_pt": _achar(df, "mibgas-pt", "index"),
        "mibgas_es": _achar(df, "mibgas-es", "index", excluir=("lng", "avb", "ugs")),
        "vtp_last":  _achar(df, "vtp", "last price"),
        "vtp_avg":   _achar(df, "vtp", "average price"),
        "pvb_last":  _achar(df, "pvb", "last price"),
        "pvb_avg":   _achar(df, "pvb", "average price"),
        "lng_es":    _achar(df, "lng", "index"),
        "avb_es":    _achar(df, "avb", "index"),
    }

    out = pd.DataFrame({"_dt": pd.to_datetime(df[col_dia], errors="coerce")})
    for destino, origem in mapa.items():
        out[destino] = pd.to_numeric(df[origem], errors="coerce") if origem else pd.NA

    em_falta = [k for k, v in mapa.items() if v is None]
    if em_falta:
        print(f"     ℹ️  {ano}: sem coluna para {', '.join(em_falta)}")
    return out.dropna(subset=["_dt"])


def _ler_longo(buf, ano):
    """2015-2022 — uma linha por dia × área; a área está na coluna 'Area'."""
    df = pd.read_excel(buf, sheet_name="Indices")
    col_dia = _achar(df, "delivery day")
    col_area = _achar(df, "area")
    if col_dia is None:
        raise ValueError(f"{ano}: coluna 'Delivery day' não encontrada")

    # O índice principal mudou de nome ao longo dos anos: 'MIBGAS Index'
    # (2015-17, 2021-22) e 'MIBGAS-ES Index' (2018-20). Ambos são o índice
    # da área indicada na linha — em 2018-20 só existia área ES.
    col_idx = (_achar(df, "mibgas index", excluir=("lng", "avb", "ugs"))
               or _achar(df, "mibgas-es index", excluir=("lng", "avb", "ugs")))
    if col_idx is None:
        raise ValueError(f"{ano}: coluna de índice não encontrada")

    col_lng = _achar(df, "lng index")
    col_avb = _achar(df, "avb index")

    df = df.copy()
    df["_dt"] = pd.to_datetime(df[col_dia], errors="coerce")
    df = df.dropna(subset=["_dt"])
    df["_idx"] = pd.to_numeric(df[col_idx], errors="coerce")
    df["_area"] = (df[col_area].astype(str).str.strip().str.upper()
                   if col_area else "ES")

    # Pivotar as áreas para colunas, para casar com o formato de 2023+.
    piv = (df.pivot_table(index="_dt", columns="_area", values="_idx", aggfunc="last")
             .rename(columns={"PT": "mibgas_pt", "ES": "mibgas_es"}))

    out = piv.reindex(columns=["mibgas_pt", "mibgas_es"]).reset_index()

    # LNG e AVB são sempre espanhóis; qualquer área serve para os ler.
    for destino, origem in (("lng_es", col_lng), ("avb_es", col_avb)):
        if origem is not None:
            extra = (df.assign(_v=pd.to_numeric(df[origem], errors="coerce"))
                       .dropna(subset=["_v"])
                       .groupby("_dt")["_v"].last())
            out[destino] = out["_dt"].map(extra)
        else:
            out[destino] = pd.NA

    # PVB e VTP só passam a ser publicados em 2023.
    for c in ("vtp_last", "vtp_avg", "pvb_last", "pvb_avg"):
        out[c] = pd.NA

    return out


# ============================================================
# Persistência
# ============================================================

def formatar(df):
    """Ordena, formata datas e arredonda. Devolve o DataFrame final."""
    df = df.dropna(subset=["_dt"]).sort_values("_dt").drop_duplicates("_dt", keep="last")
    df = df[df["_dt"].dt.year >= ANO_INICIAL]

    out = pd.DataFrame({
        "dia": df["_dt"].dt.strftime("%d/%m/%Y"),
        "data_iso": df["_dt"].dt.strftime("%Y-%m-%d"),
    })
    for c in PRECOS:
        serie = pd.to_numeric(df[c], errors="coerce") if c in df.columns else pd.NA
        out[c] = pd.Series(serie, index=df.index).round(2)

    # Uma linha sem um único preço não é um dia sem publicação — é ruído da
    # folha (linhas de rodapé, dias futuros ainda sem leilão). Fora.
    return out[out[PRECOS].notna().any(axis=1)].reset_index(drop=True)


def ler_csv_existente():
    if not os.path.exists(FICHEIRO_CSV):
        return None
    df = pd.read_csv(FICHEIRO_CSV, encoding="utf-8-sig", dtype={"dia": str, "data_iso": str})
    for c in PRECOS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[COLUNAS]


def gravar(df):
    """Escreve CSV e metadata. Não toca nos ficheiros se nada mudou.

    O carimbo 'ultima_atualizacao' mudaria a cada execução e, sozinho, faria
    o bot commitar 5x/dia um ficheiro sem dados novos. Só se escreve quando o
    CSV muda de facto — o git fica com um commit por alteração real.
    """
    os.makedirs(PASTA_GAS, exist_ok=True)

    csv_novo = df.to_csv(index=False, encoding="utf-8", float_format="%.2f",
                         lineterminator="\n")
    csv_antigo = None
    if os.path.exists(FICHEIRO_CSV):
        with open(FICHEIRO_CSV, "r", encoding="utf-8-sig", newline="") as f:
            csv_antigo = f.read()

    mudou = csv_novo != csv_antigo
    if mudou:
        with open(FICHEIRO_CSV, "w", encoding="utf-8-sig", newline="") as f:
            f.write(csv_novo)

    meta = {
        "ultima_data": df["data_iso"].max(),
        "primeira_data": df["data_iso"].min(),
        "dias": int(len(df)),
        "ultima_atualizacao": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fonte": "MIBGAS (https://www.mibgas.es)",
        "unidade": "EUR/MWh",
    }
    if mudou or not os.path.exists(FICHEIRO_META):
        with open(FICHEIRO_META, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")

    meta["_mudou"] = mudou
    return meta


# ============================================================
# Entrada
# ============================================================

def main():
    p = argparse.ArgumentParser(description="Publica os índices MIBGAS como CSV aberto.")
    p.add_argument("--backfill", action="store_true",
                   help=f"reconstrói desde {ANO_INICIAL}")
    p.add_argument("--anos", nargs="+", type=int, help="anos específicos")
    p.add_argument("--cache", metavar="DIR",
                   help="ler os xlsx de um diretório local em vez da rede")
    args = p.parse_args()

    ano_atual = datetime.now().year
    existente = ler_csv_existente()

    if args.anos:
        anos = sorted(args.anos)
    elif args.backfill or existente is None:
        if existente is None and not args.backfill:
            print("ℹ️  CSV ainda não existe — a fazer backfill completo.")
        anos = list(range(ANO_INICIAL, ano_atual + 1))
    else:
        # O MIBGAS corrige índices retroativamente; re-pedir sempre 2 anos.
        anos = [ano_atual - 1, ano_atual]

    print(f"🔥 MIBGAS — a processar {len(anos)} ano(s): {anos[0]}–{anos[-1]}")

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
    print(f"\n📊 {len(novo)} dias recolhidos "
          f"({novo['data_iso'].min()} → {novo['data_iso'].max()})")

    if existente is not None and not args.backfill:
        antes = len(existente)
        juncao = pd.concat([existente, novo], ignore_index=True)
        juncao = juncao.drop_duplicates(subset=["data_iso"], keep="last")
        final = juncao.sort_values("data_iso").reset_index(drop=True)
        print(f"   {antes} dias no CSV + {len(novo)} recolhidos "
              f"→ {len(final)} ({len(final) - antes:+d})")
    else:
        final = novo

    meta = gravar(final)

    print(f"\n✅ {FICHEIRO_CSV}"
          f"{'' if meta['_mudou'] else '  (sem alterações — não reescrito)'}")
    print(f"   {meta['dias']} dias · {meta['primeira_data']} → {meta['ultima_data']}")
    for c in PRECOS:
        n = final[c].notna().sum()
        print(f"     {c:11s} {n:5d} dias  ({100 * n / len(final):5.1f}%)")

    if falhados:
        # Um ano em falta deixa um buraco na série publicada. Falhar alto é o
        # que impede o workflow de ficar verde sobre um dataset incompleto.
        print(f"\n❌ {len(falhados)} ano(s) falharam: "
              f"{', '.join(str(a) for a, _ in falhados)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
