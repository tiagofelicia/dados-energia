#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_eua_co2.py
Publica os resultados dos leilões primários de licenças de emissão da UE
(EU ETS) como dataset aberto em data/emissoes/eua_co2.csv.

  Fonte: EEX — plataforma comum de leilões da União Europeia
         https://public.eex-group.com/eex/eua-auction-report/
         emission-spot-primary-market-auction-report-<ANO>-data.xlsx
  Série: 07/01/2020 → hoje (é o primeiro ano publicado neste caminho).

PORQUÊ ESTE DATASET
-------------------
Faltava a terceira peça para se poder calcular o custo de transformar gás
natural em eletricidade. Com o OMIE e o MIBGAS que este repositório já publica,
o custo variável de um ciclo combinado é:

    custo (EUR/MWh elétrico) = gás / rendimento
                             + preço_CO2 * 0,2016 / rendimento

onde 0,2016 tCO2/MWh térmico é o valor por omissão do regulamento de
monitorização da UE para gás natural, e o rendimento de um CCGT moderno anda
nos 55 %. A diferença para o preço do OMIE é o *clean spark spread* — quanto
uma central a gás ganha (ou perde) por MWh produzido.

ATENÇÃO ao fator de emissão: é de COMBUSTÃO, o único que o ETS cobra. Não é o
mesmo que os fatores de ciclo de vida do IPCC AR5 usados em
gerar_intensidade_carbonica.py, que incluem extração e transporte.

PORQUÊ A EEX E NÃO OUTRA FONTE
------------------------------
Há fontes com série mais longa — a SENDECO2 publica desde 2008 — mas a nota
legal delas proíbe expressamente reproduzir e redistribuir os conteúdos. Serve
para consultar, não para republicar sob a CC BY 4.0 deste repositório.

A EEX é a plataforma comum de leilões da UE e o ficheiro está marcado "Public"
no próprio cabeçalho. É primário (preço de fecho do leilão), não secundário
(spot de mercado), e os dois diferem tipicamente algumas dezenas de cêntimos.

DOIS CONTRATOS NO MESMO FICHEIRO
--------------------------------
  T3PA  EUA  — licenças gerais. É esta a série que interessa para eletricidade.
  EAA3  EUAA — licenças de aviação, outro instrumento.
A coluna 'tipo' traduz o código para EUA/EUAA; filtre por ela.

A CHAVE NÃO É A DATA
--------------------
Há dias com dois leilões (por exemplo um da UE e um da Polónia, com preços
diferentes). A chave é (data_iso, leilao, contrato). Em 2020-2026 são 15 dias
em 1466, mas bastam para partir um agrupamento feito só por data.

USO
---
  python atualizar_eua_co2.py              # ano corrente + anterior
  python atualizar_eua_co2.py --backfill   # desde 2020 (reconstrói tudo)
  python atualizar_eua_co2.py --anos 2023 2024
  python atualizar_eua_co2.py --cache DIR  # lê xlsx locais, sem rede

SAÍDA
-----
  data/emissoes/eua_co2.csv   uma linha por leilão

Não escreve data/emissoes/metadata.json: esse ficheiro é de
gerar_intensidade_carbonica.py e as duas escritas atropelar-se-iam. A última
data deste dataset é detetada pelo manifesto a partir da coluna data_iso.

Convenção de formato da casa: UTF-8 com BOM, separador ',', ponto decimal,
'dia' em DD/MM/AAAA. A coluna 'data_iso' (AAAA-MM-DD) é redundante de
propósito — é a que permite ordenar e fazer joins sem parse de datas.
"""

import argparse
import os
import re
import sys
import time
import warnings
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests

# O xlsx da EEX não traz estilo por omissão e o openpyxl avisa uma vez por
# ficheiro. Em CI são sete linhas de ruído num log que se quer legível.
warnings.filterwarnings("ignore", message="Workbook contains no default style")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA = os.path.join(ROOT_DIR, "data", "emissoes")
FICHEIRO_CSV = os.path.join(PASTA, "eua_co2.csv")

URL_TEMPLATE = ("https://public.eex-group.com/eex/eua-auction-report/"
                "emission-spot-primary-market-auction-report-{ano}-data.xlsx")

# 2019 e anteriores respondem 404 neste caminho.
ANO_INICIAL = 2020

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
}

TIMEOUT = 90
TENTATIVAS = 3

COLUNAS = ["dia", "data_iso", "leilao", "zona", "contrato", "tipo", "estado",
           "preco_eur_t", "minimo_eur_t", "maximo_eur_t", "mediana_eur_t",
           "volume_tco2", "racio_cobertura", "licitantes",
           "receita_eur", "receita_pt_eur"]

TIPOS = {"T3PA": "EUA", "EAA3": "EUAA"}


# ============================================================
# Recolha
# ============================================================

def descarregar(ano, cache_dir=None):
    """Devolve os bytes do xlsx anual. Com --cache, lê do disco e não usa rede."""
    if cache_dir:
        caminho = os.path.join(cache_dir, f"eua_{ano}.xlsx")
        if not os.path.exists(caminho):
            raise FileNotFoundError(f"não está em cache: {caminho}")
        print(f"  📁 {ano}: da cache local")
        return open(caminho, "rb").read()

    url = URL_TEMPLATE.format(ano=ano)
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
            resp.raise_for_status()
            if len(resp.content) < 10_000:
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
    """'Auction Price €/tCO2' -> 'auction price €/tco2'; tira quebras de linha."""
    return re.sub(r"\s+", " ", str(col).replace("\n", " ")).strip().lower()


def _achar(df, *padroes, excluir=()):
    """Primeira coluna cujo nome normalizado contém TODOS os padrões."""
    for col in df.columns:
        n = _norm(col)
        if all(p in n for p in padroes) and not any(x in n for x in excluir):
            return col
    return None


def ler_ano(conteudo, ano):
    """Normaliza o xlsx de um ano. Devolve um DataFrame com _dt e as colunas cruas.

    A linha de cabeçalho é PROCURADA e não assumida: o ficheiro tem um bloco de
    disclaimer por cima cujo tamanho a EEX já mudou. Assumir a linha 6 partiria
    o script no dia em que acrescentem uma linha ao aviso.
    """
    bruto = pd.read_excel(BytesIO(conteudo), sheet_name=0, header=None)

    linha_cab = None
    for i in range(min(30, len(bruto))):
        valores = [_norm(v) for v in bruto.iloc[i].tolist()]
        if any("auction price" in v for v in valores) and any(v == "date" for v in valores):
            linha_cab = i
            break
    if linha_cab is None:
        raise ValueError(f"{ano}: não encontrei a linha de cabeçalho no xlsx")

    df = pd.read_excel(BytesIO(conteudo), sheet_name=0, header=linha_cab)

    c_data = _achar(df, "date")
    c_nome = _achar(df, "auction name")
    c_contr = _achar(df, "contract")
    c_estado = _achar(df, "status")
    c_preco = _achar(df, "auction price")
    em_falta = [n for n, c in (("Date", c_data), ("Auction Name", c_nome),
                               ("Contract", c_contr), ("Auction Price", c_preco))
                if c is None]
    if em_falta:
        raise ValueError(f"{ano}: colunas em falta no xlsx: {em_falta}")

    c_min = _achar(df, "minimum bid")
    c_max = _achar(df, "maximum bid")
    c_med = _achar(df, "median")
    c_vol = _achar(df, "auction volume")
    c_cob = _achar(df, "cover ratio")
    c_lic = _achar(df, "total number of bidders")
    c_rec = _achar(df, "total revenue")
    c_zona = _achar(df, "zone")
    # "Portugal (PT)" está no bloco de receitas por Estado-membro, repartido
    # a partir dos leilões da UE. Os leilões nacionais (DE, PL) não o preenchem.
    c_pt = _achar(df, "portugal")

    def col(c):
        return df[c] if c else pd.NA

    out = pd.DataFrame({
        "_dt": pd.to_datetime(df[c_data], errors="coerce"),
        "leilao": df[c_nome].astype(str).str.strip(),
        "zona": (df[c_zona].astype(str).str.strip() if c_zona else ""),
        "contrato": df[c_contr].astype(str).str.strip(),
        "estado": (df[c_estado].astype(str).str.strip() if c_estado else ""),
        "preco_eur_t": pd.to_numeric(df[c_preco], errors="coerce"),
        "minimo_eur_t": pd.to_numeric(col(c_min), errors="coerce"),
        "maximo_eur_t": pd.to_numeric(col(c_max), errors="coerce"),
        "mediana_eur_t": pd.to_numeric(col(c_med), errors="coerce"),
        "volume_tco2": pd.to_numeric(col(c_vol), errors="coerce"),
        "racio_cobertura": pd.to_numeric(col(c_cob), errors="coerce"),
        "licitantes": pd.to_numeric(col(c_lic), errors="coerce"),
        "receita_eur": pd.to_numeric(col(c_rec), errors="coerce"),
        "receita_pt_eur": pd.to_numeric(col(c_pt), errors="coerce"),
    })
    # Sem data não é um leilão. Um leilão cancelado FICA, com preço vazio: é um
    # evento real do mercado e apagá-lo esconderia informação.
    return out.dropna(subset=["_dt"])


# ============================================================
# Formatação
# ============================================================

def ordenar(df):
    """Ordem canónica: por data e, dentro do dia, por leilão e contrato.

    Tem de ser a MESMA no backfill e no incremental. Quando diferem, cada
    corrida reescreve o ficheiro inteiro sem nada ter mudado nos dados.
    """
    return (df.sort_values(["data_iso", "leilao", "contrato"], kind="stable")
              .reset_index(drop=True))


def formatar(df):
    df = df[df["_dt"].dt.year >= ANO_INICIAL].copy()
    df = df.drop_duplicates(subset=["_dt", "leilao", "contrato"], keep="last")

    out = pd.DataFrame({
        "dia": df["_dt"].dt.strftime("%d/%m/%Y"),
        "data_iso": df["_dt"].dt.strftime("%Y-%m-%d"),
        "leilao": df["leilao"],
        "zona": df["zona"],
        "contrato": df["contrato"],
        "tipo": df["contrato"].map(TIPOS).fillna(""),
        "estado": df["estado"],
        # Tipos fixados à mão de propósito. Sem isto o dtype sai do conteúdo: no
        # incremental só se carregam dois anos e, se nesses anos todos os volumes
        # forem inteiros, a coluna vem int64 e escreve "1548000"; no backfill vem
        # float64 e escreve "1548000.0". Mesmos dados, texto diferente — e o
        # ficheiro inteiro era reescrito a cada corrida.
        "preco_eur_t": pd.to_numeric(df["preco_eur_t"], errors="coerce").round(2).astype("float64"),
        "minimo_eur_t": pd.to_numeric(df["minimo_eur_t"], errors="coerce").round(2).astype("float64"),
        "maximo_eur_t": pd.to_numeric(df["maximo_eur_t"], errors="coerce").round(2).astype("float64"),
        "mediana_eur_t": pd.to_numeric(df["mediana_eur_t"], errors="coerce").round(2).astype("float64"),
        "volume_tco2": pd.to_numeric(df["volume_tco2"], errors="coerce").round(0).astype("Int64"),
        "racio_cobertura": pd.to_numeric(df["racio_cobertura"], errors="coerce").round(2).astype("float64"),
        "licitantes": pd.to_numeric(df["licitantes"], errors="coerce").round(0).astype("Int64"),
        "receita_eur": pd.to_numeric(df["receita_eur"], errors="coerce").round(0).astype("Int64"),
        "receita_pt_eur": pd.to_numeric(df["receita_pt_eur"], errors="coerce").round(0).astype("Int64"),
    })
    return ordenar(out)


def gravar(df):
    os.makedirs(PASTA, exist_ok=True)
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


# ============================================================
# Principal
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="Publica os leilões de CO2 do EU ETS (fonte: EEX).")
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

    print(f"🏭 Leilões de CO2 (EU ETS) — {len(anos)} ano(s): {anos[0]}–{anos[-1]}")
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
        j = j.drop_duplicates(subset=["data_iso", "leilao", "contrato"],
                              keep="last")
        final = ordenar(j)
        print(f"   {antes} linhas + {len(novo)} recolhidas "
              f"→ {len(final)} ({len(final) - antes:+d})")
    else:
        final = novo

    mudou = gravar(final)
    print(f"\n✅ {FICHEIRO_CSV}"
          f"{'' if mudou else '  (sem alterações — não reescrito)'}")
    print(f"   {len(final):,} leilões · "
          f"{final['data_iso'].min()} → {final['data_iso'].max()}")
    print(f"   por tipo: " + ", ".join(
        f"{t}={n}" for t, n in final['tipo'].value_counts().items()))

    eua = final[final["tipo"] == "EUA"]
    precos = pd.to_numeric(eua["preco_eur_t"], errors="coerce").dropna()
    if len(precos):
        ult = eua[eua["preco_eur_t"].notna()].iloc[-1]
        print(f"   EUA: último {ult['preco_eur_t']} EUR/tCO2 em {ult['dia']} "
              f"· mín {precos.min():.2f} · máx {precos.max():.2f}")

    if falhados:
        print(f"\n⚠️  {len(falhados)} ano(s) falharam: "
              + ", ".join(str(a) for a, _ in falhados))


if __name__ == "__main__":
    main()
