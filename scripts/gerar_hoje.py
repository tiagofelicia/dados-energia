#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_hoje.py
Produz instantâneos leves do dia corrente, para dashboards e widgets.

  data/omie/hoje.json       preços de hoje e de amanhã       ~7 KB
  data/producao/hoje.json   mix de produção de hoje          ~12 KB
  data/gas/hoje.json        índice MIBGAS do dia             ~2 KB

PORQUÊ
------
Um widget que só queira mostrar o preço da luz hoje tem hoje de descarregar
omie_dados_atuais.csv (1,9 MB) ou producao_dados_atuais.csv (2,6 MB) e filtrar
o dia. Para uma página web isso é inviável — e é reprocessar 4,5 MB para usar
0,3% deles. Estes ficheiros são a mesma informação a 0,4% do peso.

Não contêm nada de novo: derivam dos CSV já publicados.

O QUE É "HOJE"
--------------
O dia local de Lisboa no momento da geração — não o dia UTC do runner. À
meia-noite de verão isso faria diferença de um dia.

Se a fonte ainda não tiver dados desse dia (a REN publica com algum atraso), o
ficheiro traz o último dia disponível e di-lo explicitamente:

    "data": "2026-09-10",        o dia a que os dados se referem
    "e_hoje": false,             não é o dia corrente
    "dias_atraso": 1             quantos dias atrás

Um widget deve ler "data" e não assumir que é hoje. É preferível dizer a verdade
sobre o atraso a fingir frescura que não existe.

REAL vs ESTIMADO (OMIE)
-----------------------
O bloco "amanha" só é real depois de o leilão day-ahead do MIBEL fechar (por
volta das 13:15). Antes disso, o omie_dados_atuais.csv já traz valores para
amanhã, mas são estimativas derivadas dos futuros OMIP.

O campo "real" distingue os dois casos, comparando com a Data_Valores_OMIE:

    "real": true     preço de mercado fechado
    "real": false    estimativa a partir dos futuros OMIP

Nunca omitimos o bloco: um widget que mostre "ainda não há preço para amanhã"
é menos útil do que um que mostre a estimativa devidamente rotulada.

O ÍNDICE DE GÁS CHEGA SEMPRE COM UM DIA DE ATRASO
-------------------------------------------------
O MIBGAS publica o índice de um dia de gás depois de esse dia fechar, por isso a
última linha do mibgas_spot.csv costuma trazer o dia corrente ainda sem
mibgas_pt/mibgas_es. O alvo do gas/hoje.json é o último dia COM índice português
— tipicamente ontem — e o "dias_atraso" di-lo, como nos outros dois.

USO
---
  python gerar_hoje.py              # os três
  python gerar_hoje.py --so-omie
  python gerar_hoje.py --so-producao
  python gerar_hoje.py --so-gas
  python gerar_hoje.py --data 2026-09-01   # um dia específico (testes)
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from gerar_records_omie import (  # noqa: E402
    ler_csv_omie,
    ultima_data_real_omie,
)
from gerar_records_producao import (  # noqa: E402
    ler_csv_producao,
    ler_bombagem,
    agregar_diario,
    intervalos_reportados,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

TZ = ZoneInfo("Europe/Lisbon")
PASTA_OMIE = os.path.join(ROOT_DIR, "data", "omie")
PASTA_PROD = os.path.join(ROOT_DIR, "data", "producao")
PASTA_GAS = os.path.join(ROOT_DIR, "data", "gas")

# Dias da janela usada nas médias, extremos e série do gás (o MIBGAS publica
# todos os dias do calendário, incluindo fins de semana).
JANELA_GAS = 30

CICLOS = {
    "BD": {"V": "bd_v", "F": "bd_f"},
    "BS": {"V": "bs_v", "F": "bs_f"},
    "TD": {"V": "td_v", "C": "td_c", "P": "td_p"},
    "TS": {"V": "ts_v", "C": "ts_c", "P": "ts_p"},
}

FONTES_PROD = {
    "hidrica": "Hídrica",
    "eolica": "Eólica",
    "solar": "Solar",
    "biomassa": "Biomassa",
    "ondas": "Ondas",
    "gas_ciclo_combinado": "Gás Natural - Ciclo Combinado",
    "gas_cogeracao": "Gás natural - Cogeração",
    "carvao": "Carvão",
    "outra_termica": "Outra Térmica",
    "importacao": "Importação",
    "exportacao": "Exportação",
    "bombagem": "Bombagem",
    "baterias_injecao": "Injeção de Baterias",
    "baterias_consumo": "Consumo Baterias",
}
RENOVAVEIS = ["hidrica", "eolica", "solar", "biomassa", "ondas"]


def hoje_lisboa():
    return datetime.now(TZ).date()


def agora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v):
    """float nativo arredondado, ou None — para o JSON não levar NaN nem numpy."""
    if v is None or pd.isna(v):
        return None
    return round(float(v), 2)


def inicio_do_intervalo(texto):
    """'[14:15-14:30[' -> '14:15'. Devolve None se o formato não bater."""
    t = str(texto).strip().lstrip("[")
    return t.split("-")[0] if "-" in t else None


# ============================================================
# OMIE
# ============================================================

def bloco_omie(g, dia, real):
    """Constrói o bloco de um dia a partir das suas linhas quarto-horárias."""
    g = g.sort_values("_ord")
    horas = [inicio_do_intervalo(x) for x in g["intervalo"]]

    bloco = {
        "data": dia.isoformat(),
        "real": real,
        "intervalos": int(len(g)),
        "horas": horas,
    }
    for zona, col in (("pt", "preco_pt"), ("es", "preco_es")):
        s = pd.to_numeric(g[col], errors="coerce")
        if s.notna().sum() == 0:
            bloco[zona] = None
            continue
        i_min, i_max = int(s.values.argmin()), int(s.values.argmax())
        bloco[zona] = {
            "medio": _num(s.mean()),
            "min": _num(s.min()),
            "min_hora": horas[i_min],
            "max": _num(s.max()),
            "max_hora": horas[i_max],
            "valores": [_num(v) for v in s],
        }

    ciclos = {}
    for col_ciclo, mapa in CICLOS.items():
        if col_ciclo not in g.columns:
            continue
        medias = g.groupby(col_ciclo)["preco_pt"].mean()
        for codigo, destino in mapa.items():
            if codigo in medias.index:
                ciclos[destino] = _num(medias[codigo])
    bloco["ciclos_medio_pt"] = ciclos
    return bloco


def gerar_omie(alvo):
    caminho = os.path.join(PASTA_OMIE, "omie_dados_atuais.csv")
    if not os.path.exists(caminho):
        print("  ⚠️  omie_dados_atuais.csv não existe — a saltar OMIE.")
        return None

    df = ler_csv_omie(caminho)
    df["_d"] = pd.to_datetime(df["dia"], format="%d/%m/%Y", errors="coerce").dt.date
    df = df.dropna(subset=["_d"])
    # A ordem do CSV é a ordem cronológica dentro do dia (inclui a hora repetida
    # de outubro); preserva-se pelo índice em vez de ordenar por texto.
    df["_ord"] = range(len(df))

    _ts = ultima_data_real_omie()
    ultimo_real = _ts.date() if _ts is not None else None
    if ultimo_real is None:
        raise RuntimeError(
            "Data_Valores_OMIE não encontrada — sem ela não é possível dizer se "
            "os preços de amanhã são reais ou estimados a partir dos futuros "
            "OMIP. A abortar em vez de publicar estimativas sem rótulo."
        )

    disponiveis = sorted(d for d in df["_d"].unique() if d <= ultimo_real)
    if not disponiveis:
        raise RuntimeError("nenhum dia com preços reais em omie_dados_atuais.csv")

    # O fallback nunca escolhe um dia futuro: a Data_Valores_OMIE já cobre
    # amanhã depois do leilão das 13:15, e apanhar esse dia como "hoje" daria
    # um dias_atraso negativo. Com --data respeita-se o que foi pedido.
    dia_hoje = alvo if alvo in disponiveis else max(
        (d for d in disponiveis if d <= alvo), default=disponiveis[0])
    dia_amanha = dia_hoje + timedelta(days=1)

    out = {
        "gerado_em": agora_iso(),
        "timezone": "Europe/Lisbon",
        "interval_minutes": 15,
        "unidade": "EUR/MWh",
        "fonte": "OMIE (mercado diário MIBEL)",
        "e_hoje": dia_hoje == hoje_lisboa(),
        "dias_atraso": (hoje_lisboa() - dia_hoje).days,
        "hoje": bloco_omie(df[df["_d"] == dia_hoje], dia_hoje, True),
        "amanha": None,
    }
    g_am = df[df["_d"] == dia_amanha]
    if len(g_am):
        out["amanha"] = bloco_omie(g_am, dia_amanha, dia_amanha <= ultimo_real)

    return out


# ============================================================
# Produção
# ============================================================

def gerar_producao(alvo):
    caminho = os.path.join(PASTA_PROD, "producao_dados_atuais.csv")
    if not os.path.exists(caminho):
        print("  ⚠️  producao_dados_atuais.csv não existe — a saltar produção.")
        return None

    bruto = ler_csv_producao(caminho)
    bruto["_d"] = pd.to_datetime(bruto["dia"], format="%d/%m/%Y", errors="coerce").dt.date
    bruto = bruto.dropna(subset=["_d"])

    disponiveis = sorted(bruto["_d"].unique())
    # Mesmo critério do OMIE: o fallback não salta para um dia futuro.
    dia = alvo if alvo in disponiveis else max(
        (d for d in disponiveis if d <= alvo), default=disponiveis[0])
    g = bruto[bruto["_d"] == dia].copy()

    # O dia em curso traz sempre 96 intervalos, mas a partir da hora da recolha
    # as fontes despacháveis vêm a zero. Aqui NÃO se exclui o dia — o ponto do
    # ficheiro é mostrá-lo — mas trunca-se no último intervalo reportado, para
    # que o resumo e o bloco "agora" não descrevam um sistema que não existe
    # (ex.: 100 % renovável com 815 MW contra 6 072 MW de consumo).
    n_real = intervalos_reportados(g)
    n_total = len(g)
    if n_real < n_total:
        print(f"  ✂️  {n_total - n_real} intervalos ainda não reportados pela REN "
              f"— resumo e 'agora' calculados sobre os {n_real} reais")
        g = g.iloc[:n_real].copy()

    # Resumo diário pela MESMA lógica dos recordes e dos agregados, para os três
    # nunca divergirem (QH=0,25; hídrica renovável desconta bombagem turbinada).
    daily = agregar_diario(g, ler_bombagem())
    linha = daily.iloc[0] if len(daily) else None

    horas = [inicio_do_intervalo(x) for x in g["intervalo"]]
    series, ultimo = {}, {}
    for chave, col in FONTES_PROD.items():
        if col not in g.columns:
            continue
        s = pd.to_numeric(g[col], errors="coerce").fillna(0)
        series[chave] = [_num(v) for v in s]
        ultimo[chave] = _num(s.iloc[-1])

    consumo = pd.to_numeric(g["Consumo"], errors="coerce").fillna(0)
    series["consumo"] = [_num(v) for v in consumo]

    ren_agora = sum(ultimo.get(k) or 0 for k in RENOVAVEIS)
    # Denominador do instante: só produção nacional (exclui importação e o
    # consumo de bombagem, que não é geração).
    nac_agora = ren_agora + sum(
        ultimo.get(k) or 0
        for k in ("gas_ciclo_combinado", "gas_cogeracao", "carvao",
                  "outra_termica", "baterias_injecao")
    )

    out = {
        "gerado_em": agora_iso(),
        "timezone": "Europe/Lisbon",
        "interval_minutes": 15,
        "unidade_series": "MW",
        "unidade_resumo": "GWh",
        "fonte": "REN Data Hub",
        "e_hoje": dia == hoje_lisboa(),
        "dias_atraso": (hoje_lisboa() - dia).days,
        "data": dia.isoformat(),
        "intervalos": int(len(g)),
        "intervalos_no_ficheiro": int(n_total),
        "completo": n_real >= 92 and n_real >= n_total,
        "horas": horas,
        "agora": {
            "hora": horas[-1] if horas else None,
            "consumo_mw": _num(consumo.iloc[-1]) if len(consumo) else None,
            "perc_renovavel": _num(100 * ren_agora / nac_agora) if nac_agora else None,
            "fontes_mw": ultimo,
        },
        "resumo": None,
        "series_mw": series,
    }

    if linha is not None:
        out["resumo"] = {
            "consumo_gwh": _num(linha.get("consumoGwh")),
            "pico_consumo_mw": _num(linha.get("Consumo_p")),
            "pico_consumo_hora": linha.get("picoConsumoHora"),
            "producao_nac_gwh": _num(linha.get("producaoNacGwh")),
            "renovavel_gwh": _num(linha.get("renovavelGwh")),
            "perc_renovavel": _num(linha.get("percRenov")),
            "saldo_importador_gwh": _num(linha.get("saldoImpGwh")),
        }
    return out


# ============================================================
# Gás (MIBGAS)
# ============================================================

def bloco_gas(valores, dias, i):
    """Estatísticas de uma série diária de preços na posição i.

    valores/dias são listas alinhadas (já ordenadas por data). Devolve None se
    não houver preço nesse dia — o índice português só existe desde 03/2021 e
    há dias sem publicação.
    """
    v = valores[i]
    if v is None:
        return None

    out = {"preco": v}

    # Dia anterior COM preço (não necessariamente i-1: há lacunas na série)
    ant = next((valores[k] for k in range(i - 1, -1, -1) if valores[k] is not None), None)
    if ant is not None:
        out["var_dia"] = _num(v - ant)
        out["var_dia_pct"] = _num(100 * (v - ant) / ant) if ant else None

    ini = max(0, i - (JANELA_GAS - 1))
    janela = [(dias[k], valores[k]) for k in range(ini, i + 1) if valores[k] is not None]
    if janela:
        precos = [p for _, p in janela]
        out["media_7d"] = _num(sum(precos[-7:]) / len(precos[-7:]))
        out["media_30d"] = _num(sum(precos) / len(precos))
        d_min, p_min = min(janela, key=lambda x: x[1])
        d_max, p_max = max(janela, key=lambda x: x[1])
        out["min_30d"] = p_min
        out["min_30d_dia"] = d_min
        out["max_30d"] = p_max
        out["max_30d_dia"] = d_max
    return out


def gerar_gas(alvo):
    caminho = os.path.join(PASTA_GAS, "mibgas_spot.csv")
    if not os.path.exists(caminho):
        print("  ⚠️  mibgas_spot.csv não existe — a saltar gás.")
        return None

    df = pd.read_csv(caminho, encoding="utf-8-sig")
    for col in ("data_iso", "mibgas_pt", "mibgas_es"):
        if col not in df.columns:
            raise RuntimeError(f"mibgas_spot.csv sem a coluna '{col}'")

    df["_d"] = pd.to_datetime(df["data_iso"], format="%Y-%m-%d", errors="coerce").dt.date
    df = df.dropna(subset=["_d"]).sort_values("_d").reset_index(drop=True)
    df = df[df["_d"] <= alvo]
    if df.empty:
        raise RuntimeError("mibgas_spot.csv sem dias até à data pedida")

    dias = [d.isoformat() for d in df["_d"]]
    pt = [_num(v) for v in pd.to_numeric(df["mibgas_pt"], errors="coerce")]
    es = [_num(v) for v in pd.to_numeric(df["mibgas_es"], errors="coerce")]

    # O alvo é o último dia com índice PORTUGUÊS: é esse o preço que a maioria
    # dos leitores procura, e sem ele o instantâneo não teria valor principal.
    i = next((k for k in range(len(pt) - 1, -1, -1) if pt[k] is not None), None)
    if i is None:
        raise RuntimeError("mibgas_spot.csv sem nenhum índice PT")

    dia = df["_d"].iloc[i]
    b_pt = bloco_gas(pt, dias, i)
    b_es = bloco_gas(es, dias, i)

    ini = max(0, i - (JANELA_GAS - 1))
    serie = {
        "dias": dias[ini:i + 1],
        "pt": pt[ini:i + 1],
        "es": es[ini:i + 1],
    }

    return {
        "gerado_em": agora_iso(),
        "timezone": "Europe/Lisbon",
        "unidade": "EUR/MWh",
        "fonte": "MIBGAS (mercado ibérico de gás natural)",
        "e_hoje": dia == hoje_lisboa(),
        "dias_atraso": (hoje_lisboa() - dia).days,
        "data": dia.isoformat(),
        "pt": b_pt,
        "es": b_es,
        "spread_pt_es": (_num(b_pt["preco"] - b_es["preco"])
                         if b_pt and b_es else None),
        "serie_30d": serie,
    }


# ============================================================
# Escrita
# ============================================================

def gravar(dados, caminho):
    """Escreve só se o conteúdo mudou, ignorando o carimbo gerado_em.

    Sem isto, o carimbo sozinho faria um commit a cada corrida.
    """
    novo = json.dumps(dados, ensure_ascii=False, indent=1, sort_keys=True)
    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                antigo = json.load(f)
            a, b = dict(antigo), dict(dados)
            a.pop("gerado_em", None)
            b.pop("gerado_em", None)
            if a == b:
                print(f"   = {os.path.basename(caminho):22s} (sem alterações)")
                return False
        except (OSError, ValueError):
            pass

    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        f.write(novo + "\n")
    kb = len(novo.encode("utf-8")) / 1024
    print(f"   ✓ {os.path.basename(caminho):22s} {kb:6.1f} KB")
    return True


def main():
    p = argparse.ArgumentParser(description="Instantâneos leves do dia corrente.")
    p.add_argument("--so-omie", action="store_true")
    p.add_argument("--so-producao", action="store_true")
    p.add_argument("--so-gas", action="store_true")
    p.add_argument("--data", help="dia específico, AAAA-MM-DD (testes)")
    args = p.parse_args()

    # Sem nenhum "--so-*" correm todos; com um ou mais, só esses.
    seletivo = args.so_omie or args.so_producao or args.so_gas

    def quer(nome):
        return getattr(args, "so_" + nome) if seletivo else True

    alvo = date.fromisoformat(args.data) if args.data else hoje_lisboa()
    print(f"📅 Hoje em Lisboa: {hoje_lisboa()}"
          f"{'' if not args.data else f' · alvo pedido: {alvo}'}")

    def nota_atraso(d):
        n = d["dias_atraso"]
        if d["e_hoje"]:
            return ""
        return f"  (atraso de {n} dia{'' if n == 1 else 's'})"

    if quer("omie"):
        print("\n🔌 OMIE")
        d = gerar_omie(alvo)
        if d:
            print(f"   {d['hoje']['data']} · média PT {d['hoje']['pt']['medio']}"
                  f" EUR/MWh{nota_atraso(d)}")
            am = d.get("amanha")
            if am:
                origem = "real" if am["real"] else "ESTIMADO de futuros OMIP"
                print(f"   {am['data']} · média PT {am['pt']['medio']} EUR/MWh · {origem}")
            gravar(d, os.path.join(PASTA_OMIE, "hoje.json"))

    if quer("producao"):
        print("\n⚡ Produção")
        d = gerar_producao(alvo)
        if d:
            r = d.get("resumo") or {}
            print(f"   {d['data']} · {d['intervalos']} intervalos · "
                  f"consumo {r.get('consumo_gwh')} GWh · "
                  f"renovável {r.get('perc_renovavel')}%{nota_atraso(d)}")
            gravar(d, os.path.join(PASTA_PROD, "hoje.json"))

    if quer("gas"):
        print("\n🔥 Gás (MIBGAS)")
        d = gerar_gas(alvo)
        if d:
            pt, es = d.get("pt") or {}, d.get("es") or {}
            print(f"   {d['data']} · PT {pt.get('preco')} EUR/MWh"
                  f" · ES {es.get('preco')} EUR/MWh"
                  f" · média 30d {pt.get('media_30d')}{nota_atraso(d)}")
            gravar(d, os.path.join(PASTA_GAS, "hoje.json"))

    print()


if __name__ == "__main__":
    main()
