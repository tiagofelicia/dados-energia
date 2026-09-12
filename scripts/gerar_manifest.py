#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_manifest.py
Escreve data/manifest.json — o catálogo legível por máquina de tudo o que este
repositório publica.

PARA QUE SERVE
--------------
Resolve quatro problemas de uma vez:

  1. Descoberta. Quem chega ao repositório não tem como enumerar os datasets
     sem abrir a pasta data/. Quatro deles — incluindo os 127 MB de
     producao-entsoe, o maior do repositório — não estão documentados em lado
     nenhum.
  2. Estado. O status.html lê este ficheiro para dizer se algum dataset parou
     de actualizar. Um fetch, em vez de doze.
  3. Divergência. O README e o index.html são catálogos paralelos mantidos à
     mão, e já divergiram. Passam a poder ser gerados daqui.
  4. Agregadores. Com um export DCAT por cima, entra em dados.gov.pt e no
     European Data Portal sem trabalho extra.

COMO ESTÁ ORGANIZADO
--------------------
Metade declarativa, metade inspeccionada.

O REGISTO abaixo tem o que não se infere do disco: título, descrição, cadência
prometida, tolerância de atraso, avisos. É conhecimento, e vive aqui.

O resto — período coberto, última data real, contagem de linhas e ficheiros,
tamanho, colunas — é lido dos ficheiros em cada corrida, para não haver dois
sítios a dizer coisas diferentes.

A ÚLTIMA DATA REAL
------------------
O campo mais importante, e o mais fácil de errar. O omie_dados_atuais.csv vai
até 01/01/2027, mas 111 desses dias são estimativas dos futuros OMIP. Um
catálogo ingénuo diria que o OMIE está actualizado até 2027 — e o status.html
mostraria "OK" para sempre, mesmo com o pipeline parado há um mês.

Por isso cada entrada declara COMO se determina a sua última data real, e o
OMIE usa a Data_Valores_OMIE, nunca o máximo do ficheiro.

USO
---
  python gerar_manifest.py
  python gerar_manifest.py --verificar   # não escreve; sai !=0 se algo falta
"""

import argparse
import csv
import glob
import json
import os
import sys
from datetime import date, datetime, timezone

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from gerar_records_omie import ultima_data_real_omie  # noqa: E402
DATA_DIR = os.path.join(ROOT_DIR, "data")
SAIDA = os.path.join(DATA_DIR, "manifest.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

BASE_URL = "https://dados.tiagofelicia.pt"

# ============================================================
# Registo — o que não se infere do disco
# ============================================================
# deteccao:
#   csv_col:<coluna>       última data da coluna (DD/MM/AAAA)
#   csv_col_iso:<coluna>   idem, em AAAA-MM-DD
#   omie_real              Data_Valores_OMIE (corta os futuros OMIP)
#   meta:<chave>           chave de um metadata.json na mesma pasta
#   meta_paises            metadata.json com um dicionário por país
#   glob_max               nome do ficheiro mais recente do padrão
#   estatico               não tem data (actualização manual/anual)

# NOTA: os ficheiros de data/simuladores/ (tarifários fixos, indexados e duais)
# NÃO constam deste catálogo de propósito. São fruto de pesquisa e agregação
# manual do autor, não de recolha automática de fonte pública, e não estão sob
# a CC BY 4.0 que cobre o resto do repositório — ver a nota de licença no
# README. Não os acrescentar aqui.

REGISTO = [
    # ---------- OMIE ----------
    dict(id="omie-atuais", grupo="omie", caminho="data/omie/omie_dados_atuais.csv",
         titulo="Preços OMIE do ano corrente",
         descricao="Preços quarto-horários do mercado diário MIBEL no ano corrente, "
                   "com classificação de período horário BTN. Inclui datas futuras "
                   "estimadas a partir dos futuros OMIP.",
         cadencia="~5x/dia útil", tolerancia_dias=1, deteccao="omie_real",
         avisos=["Contém blocos TABELA_ATUALIZACOES / TABELA_FUTUROS_* no fim do "
                 "ficheiro; pd.read_csv traz essas linhas como dados.",
                 "As datas posteriores a ultima_data_real são estimativas OMIP, "
                 "não preços de mercado."]),
    dict(id="omie-historico", grupo="omie", caminho="data/omie/historico/omie_historico_*.csv",
         titulo="Preços OMIE — séries anuais",
         descricao="Séries históricas anuais desde 2010, mesmo schema do ano corrente.",
         cadencia="fecho de ano", tolerancia_dias=400, deteccao="glob_max"),
    dict(id="omie-precos-horarios", grupo="omie", caminho="data/omie/precos-horarios.csv",
         titulo="Preços finais por tarifário indexado",
         descricao="Preço quarto-horário final por tarifário indexado e opção horária.",
         cadencia="~5x/dia útil", tolerancia_dias=1, deteccao="csv_col:dia",
         avisos=["Não abre com pd.read_csv sem argumentos: tem tabelas concatenadas "
                 "horizontalmente (TABELA_HORARIA na coluna 11, TABELA_CONSTANTES na 19), "
                 "o que dá linhas de 8, 11, 16, 19 e 20 campos."]),
    dict(id="omie-futuros", grupo="omie", caminho="data/omie/futuros_omip.csv",
         titulo="Futuros OMIP — sessão em curso",
         descricao="Versão leve com as tabelas de metadados e futuros OMIP (~3 KB).",
         cadencia="~5x/dia útil", tolerancia_dias=3, deteccao="omie_real"),
    # Coluna 'Data', não 'dia' como os restantes históricos — daí a deteção própria.
    dict(id="omip-historico", grupo="omie", caminho="data/omie/historico/omip_historico_*.csv",
         titulo="Futuros OMIP — histórico de sessões",
         descricao="Uma linha por sessão OMIP × zona × contrato.",
         cadencia="diária", tolerancia_dias=4, deteccao="csv_col:Data"),
    dict(id="omie-records", grupo="omie", caminho="data/omie/records_omie.json",
         titulo="Recordes OMIE",
         descricao="Recordes históricos (dia/hora mais caro e mais barato, etc.) "
                   "e agregados mensais e anuais.",
         cadencia="diária", tolerancia_dias=2, deteccao="meta:geradoEm"),
    dict(id="omie-hoje", grupo="omie", caminho="data/omie/hoje.json",
         titulo="Instantâneo OMIE do dia",
         descricao="Preços de hoje e de amanhã para dashboards e widgets (~8 KB).",
         cadencia="a cada actualização", tolerancia_dias=1, deteccao="meta:_hoje_data"),
    dict(id="mibel-acum", grupo="omie", caminho="data/omie/MIBEL_ano_atual_ACUM.csv",
         titulo="MIBEL acumulado (intermédio de pipeline)",
         descricao="Preços horários PT/ES dos últimos 12 meses. Intermédio do "
                   "pipeline; formato pode mudar sem aviso.",
         cadencia="~5x/dia útil", tolerancia_dias=2, deteccao="csv_col_iso:Data",
         intermedio=True),

    # ---------- Produção ----------
    dict(id="producao-atuais", grupo="producao", caminho="data/producao/producao_dados_atuais.csv",
         titulo="Produção elétrica do ano corrente",
         descricao="Produção por fonte em Portugal, potência média (MW) por "
                   "intervalo de 15 minutos.",
         cadencia="cada 4h", tolerancia_dias=1, deteccao="csv_col:dia"),
    dict(id="producao-historico", grupo="producao",
         caminho="data/producao/historico/producao_historico_*.csv",
         titulo="Produção elétrica — séries anuais",
         descricao="Séries históricas anuais desde 2010, mesmo schema.",
         cadencia="fecho de ano", tolerancia_dias=400, deteccao="glob_max"),
    dict(id="producao-bombagem", grupo="producao",
         caminho="data/producao/producao_bombagem_diaria.csv",
         titulo="Bombagem hidroelétrica diária",
         descricao="Produção por bombagem (turbinagem de reversíveis), em GWh/dia, "
                   "desde 2010. Permite separar a parcela não renovável da hídrica.",
         cadencia="cada 4h", tolerancia_dias=2, deteccao="csv_col:dia"),
    dict(id="producao-records", grupo="producao", caminho="data/producao/records_producao.json",
         titulo="Recordes de produção",
         descricao="Recordes do balanço energético e agregados mensais e anuais.",
         cadencia="cada 4h", tolerancia_dias=2, deteccao="meta:geradoEm"),
    dict(id="producao-hoje", grupo="producao", caminho="data/producao/hoje.json",
         titulo="Instantâneo de produção do dia",
         descricao="Mix quarto-horário, resumo diário e estado no último "
                   "intervalo, para dashboards (~15 KB).",
         cadencia="a cada actualização", tolerancia_dias=1, deteccao="meta:data"),
    dict(id="producao-entsoe", grupo="producao", caminho="data/producao/producao-entsoe/*/*.json",
         titulo="Produção europeia intra-diária (ENTSO-E)",
         descricao="Geração por tecnologia, consumo, previsões day-ahead e "
                   "intradiárias, preços e fluxos transfronteiriços, para 35 países. "
                   "Um ficheiro por país e semana ISO.",
         cadencia="cada 4h (PT/ES) · 2x/dia (todos)", tolerancia_dias=2,
         deteccao="meta_paises"),

    # ---------- Mapas ----------
    dict(id="mapas-precos", grupo="mapas", caminho="data/mapas/precos_qh/*.json",
         titulo="Preços day-ahead europeus",
         descricao="Preços por zona de mercado europeia (48 zonas), um ficheiro "
                   "por mês desde 2018-01. Por dia e zona: média, mínimo, máximo "
                   "e série de 96 valores.",
         cadencia="1x/dia", tolerancia_dias=1, deteccao="meta:ultima_data",
         avisos=["O campo resolution diz sempre PT15M, mesmo no histórico horário: "
                 "os valores horários são replicados pelos 4 quartos.",
                 "values pode conter null em dias parcialmente publicados."]),
    dict(id="mapas-producao", grupo="mapas", caminho="data/mapas/producao/*.json",
         titulo="Mix de produção europeu diário",
         descricao="Produção diária por país e tecnologia (GWh) e quota renovável, "
                   "para 28 países, desde 2026-01.",
         cadencia="2x/dia", tolerancia_dias=1, deteccao="meta:ultima_data"),

    # ---------- Gás ----------
    dict(id="gas-mibgas", grupo="gas", caminho="data/gas/mibgas_spot.csv",
         titulo="Índices MIBGAS",
         descricao="Índices diários do mercado ibérico de gás natural (PT, ES, "
                   "VTP, PVB, GNL, armazenamento), desde 17/12/2015. Só preços "
                   "reais: sem futuros, sem preenchimento de lacunas.",
         cadencia="2x/dia", tolerancia_dias=2, deteccao="meta:ultima_data"),
    dict(id="gas-futuros", grupo="gas", caminho="data/gas/mibgas_futuros.csv",
         titulo="Curva forward do gás ibérico",
         descricao="Produtos a prazo negociados no MIBGAS em preço absoluto, "
                   "do intradiário ao ano Y+2, desde 16/12/2015. A coluna "
                   "'rotulo' traduz o código relativo (GMES_M+2) no período de "
                   "entrega a que a cotação se refere (Maio 2025).",
         cadencia="2x/dia", tolerancia_dias=4, deteccao="csv_col_iso:data_iso",
         avisos=["Os produtos de médio e longo prazo existem só para ES — a "
                 "liquidez forward está no hub espanhol.",
                 "Só há linhas em dias de sessão; nem todos os produtos "
                 "negoceiam todos os dias.",
                 "horizonte 'intradiario' entrega no próprio dia da sessão e "
                 "'dia' entrega de D+1 a D+3: ambos têm dias_entrega=1."]),
    dict(id="gas-ttf-spread", grupo="gas", caminho="data/gas/mibgas_ttf_spread.csv",
         titulo="Spread PVB-TTF",
         descricao="Prémio do gás ibérico face ao benchmark europeu TTF, para 15 "
                   "produtos de D+1 a Y+2, desde 02/01/2024.",
         cadencia="2x/dia", tolerancia_dias=4, deteccao="csv_col_iso:data_iso",
         avisos=["Só há linhas em dias de sessão (~255/ano): fins de semana e "
                 "feriados não têm cotação.",
                 "ttf_derivado é uma estimativa (±1 EUR/MWh), não o índice oficial.",
                 "A chave é (data_iso, produto, entrega_inicio): os produtos de "
                 "estação W e S cotam duas estações em simultâneo e aparecem "
                 "duas vezes na mesma sessão."]),

    # ---------- Derivados ----------
    dict(id="agregados-omie", grupo="agregados", caminho="data/agregados/omie_*.csv",
         titulo="Agregados OMIE (diário, mensal, anual)",
         descricao="Médias, mínimos, máximos e preço médio por período do ciclo "
                   "horário BTN. Derivado de data/omie/, com os dias futuros excluídos.",
         cadencia="1x/dia", tolerancia_dias=2, deteccao="meta:omie.ultima_data"),
    dict(id="agregados-producao", grupo="agregados", caminho="data/agregados/producao_*.csv",
         titulo="Agregados de produção (diário, mensal, anual)",
         descricao="Energia por fonte, quota renovável, pico de consumo e saldo "
                   "importador. Derivado de data/producao/.",
         cadencia="1x/dia", tolerancia_dias=2, deteccao="meta:producao.ultima_data"),
    dict(id="emissoes", grupo="emissoes", caminho="data/emissoes/intensidade_*.csv",
         titulo="Intensidade carbónica da produção elétrica",
         descricao="gCO2eq/kWh da produção nacional, a 15 minutos desde 2010, "
                   "agregada por dia, mês, ano e hora do dia. Fatores IPCC AR5.",
         cadencia="1x/dia", tolerancia_dias=2, deteccao="meta:ultima_data",
         avisos=["São emissões de CICLO DE VIDA, não diretas: não comparáveis com "
                 "o Inventário Nacional da APA nem com o indicador da EEA.",
                 "Cobre a produção nacional, não o consumo: as importações de "
                 "Espanha (25% do consumo em 2024) não estão incluídas."]),

    # ---------- Referência ----------
    dict(id="referencia-tecnologias", grupo="referencia",
         caminho="data/referencia/tecnologias.json",
         titulo="Tabela de tecnologias de geração",
         descricao="Traduz entre os três vocabulários de tecnologias usados "
                   "neste repositório (REN, Energy-Charts, ENTSO-E), com "
                   "categoria, fator de emissão e notas de agregação.",
         cadencia="quando as fontes mudam", tolerancia_dias=400,
         deteccao="estatico"),

    # ---------- Regulado ----------
    dict(id="regulado-perfis", grupo="regulado", caminho="data/regulado/perfis_erse_9.json",
         titulo="Perfis de consumo ERSE",
         descricao="Perfis de consumo BTN publicados pela ERSE.",
         cadencia="anual", tolerancia_dias=400, deteccao="estatico"),
    dict(id="regulado-perdas", grupo="regulado",
         caminho="data/regulado/Perdas_calculadas_2026_TF.csv",
         titulo="Fatores de perdas BT e MT",
         descricao="Fatores (1+perdas) calculados a partir dos perfis de perdas da E-Redes.",
         cadencia="anual", tolerancia_dias=400, deteccao="estatico",
         avisos=["Formato fora da convenção do repositório: separador ';', vírgula "
                 "decimal, data '1/jan/2026', e 3 linhas decorativas antes do cabeçalho."]),
    dict(id="regulado-tos", grupo="regulado", caminho="data/regulado/tos_municipios.json",
         titulo="Taxa de Ocupação do Subsolo por município",
         descricao="TOS por município e respetivo ORD/CUR de gás natural.",
         cadencia="quando há alterações", tolerancia_dias=400, deteccao="estatico"),

]

GRUPOS = {
    "omie": "Preços de mercado (OMIE / OMIP)",
    "producao": "Produção elétrica",
    "mapas": "Europa",
    "gas": "Mercado de gás natural",
    "agregados": "Séries pré-calculadas",
    "emissoes": "Emissões",
    "referencia": "Tabelas de referência",
    "regulado": "Dados regulados (ERSE / E-Redes)",
}


# ============================================================
# Inspeção
# ============================================================

def ficheiros_de(caminho):
    padrao = os.path.join(ROOT_DIR, caminho.replace("/", os.sep))
    return sorted(glob.glob(padrao)) if "*" in caminho else (
        [padrao] if os.path.exists(padrao) else [])


def _de_csv(ficheiros, coluna, iso):
    melhor = None
    for f in ficheiros:
        try:
            d = pd.read_csv(f, encoding="utf-8-sig", usecols=[coluna], dtype=str)
            s = pd.to_datetime(d[coluna], errors="coerce",
                               format=None if iso else "%d/%m/%Y").dropna()
            if len(s):
                m = s.max().date()
                melhor = m if melhor is None or m > melhor else melhor
        except Exception:
            continue
    return melhor.isoformat() if melhor else None


def _de_meta(caminho, chave):
    pasta = os.path.dirname(os.path.join(ROOT_DIR, caminho.replace("/", os.sep)))
    # Ficheiros que são eles próprios o metadata (records, hoje)
    alvo = os.path.join(ROOT_DIR, caminho.replace("/", os.sep))
    candidatos = ([alvo] if alvo.endswith(".json") and os.path.exists(alvo) else []) + \
                 [os.path.join(pasta, "metadata.json")]
    for p in candidatos:
        if not os.path.exists(p):
            continue
        try:
            j = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        no = j
        for parte in chave.split("."):
            if isinstance(no, dict) and parte in no:
                no = no[parte]
            else:
                no = None
                break
        if isinstance(no, str) and no:
            return no[:10]
    return None


def _de_meta_paises(caminho):
    pasta = os.path.dirname(os.path.join(ROOT_DIR, caminho.replace("/", os.sep).split("*")[0]))
    p = os.path.join(pasta, "metadata.json")
    if not os.path.exists(p):
        return None
    try:
        j = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # O dataset está tão actualizado quanto o país mais atrasado.
    datas = [v["actualizado_em"][:10] for v in j.values()
             if isinstance(v, dict) and v.get("actualizado_em")]
    return min(datas) if datas else None


def ultima_data(entrada, ficheiros):
    d = entrada["deteccao"]
    if d == "omie_real":
        ts = ultima_data_real_omie()
        return ts.date().isoformat() if ts is not None else None
    if d == "meta_paises":
        return _de_meta_paises(entrada["caminho"])
    if d == "glob_max":
        if not ficheiros:
            return None
        return _de_csv([ficheiros[-1]], "dia", iso=False)
    if d.startswith("csv_col_iso:"):
        return _de_csv(ficheiros, d.split(":", 1)[1], iso=True)
    if d.startswith("csv_col:"):
        return _de_csv(ficheiros, d.split(":", 1)[1], iso=False)
    if d.startswith("meta:"):
        chave = d.split(":", 1)[1]
        if chave == "_hoje_data":   # omie/hoje.json guarda a data dentro de "hoje"
            return _de_meta(entrada["caminho"], "hoje.data")
        return _de_meta(entrada["caminho"], chave)
    return None


def inspecionar(entrada):
    ficheiros = ficheiros_de(entrada["caminho"])
    total = sum(os.path.getsize(f) for f in ficheiros)

    linhas = None
    if len(ficheiros) == 1 and ficheiros[0].endswith(".csv"):
        try:
            with open(ficheiros[0], "r", encoding="utf-8-sig") as f:
                linhas = sum(1 for _ in f) - 1
        except OSError:
            pass

    colunas = None
    csvs = [f for f in ficheiros if f.endswith(".csv")]
    if csvs:
        try:
            colunas = list(pd.read_csv(csvs[0], encoding="utf-8-sig", nrows=0).columns)
        except Exception:
            pass

    # Para um dataset de vários ficheiros, 'caminho' é um padrão e não um URL
    # descarregável. O 'url' aponta então à pasta, e o padrão fica em 'padrao'
    # para quem quiser enumerar — senão um cliente tentaria descarregar um URL
    # com '*' lá dentro.
    # A pasta que contém o padrão: último '/' ANTES do primeiro '*'. Cortar no
    # último '/' do caminho inteiro deixaria um '*' no URL (o producao-entsoe
    # tem glob em dois níveis, .../*/*.json); cortar no '*' deixaria um prefixo
    # de nome de ficheiro, que também não é um URL.
    tem_glob = "*" in entrada["caminho"]
    base_rel = (entrada["caminho"].split("*", 1)[0].rsplit("/", 1)[0] + "/"
                if tem_glob else entrada["caminho"])

    out = {
        "id": entrada["id"],
        "titulo": entrada["titulo"],
        "descricao": entrada["descricao"],
        "grupo": entrada["grupo"],
        "caminho": base_rel,
        "url": f"{BASE_URL}/{base_rel}",
        "formato": "json" if entrada["caminho"].endswith(".json") else "csv",
        "cadencia": entrada["cadencia"],
        "tolerancia_dias": entrada["tolerancia_dias"],
        "ultima_data": ultima_data(entrada, ficheiros),
        "ficheiros": len(ficheiros),
        "bytes": total,
    }
    if tem_glob:
        out["padrao"] = entrada["caminho"]
    if linhas is not None:
        out["linhas"] = linhas
    if colunas:
        out["colunas"] = colunas
    if entrada.get("avisos"):
        out["avisos"] = entrada["avisos"]
    if entrada.get("intermedio"):
        out["intermedio"] = True
    return out


def main():
    p = argparse.ArgumentParser(description="Gera data/manifest.json.")
    p.add_argument("--verificar", action="store_true",
                   help="não escreve; sai !=0 se faltarem ficheiros ou datas")
    args = p.parse_args()

    print(f"📋 Manifesto — {len(REGISTO)} datasets registados\n")
    datasets, problemas = [], []
    for entrada in REGISTO:
        d = inspecionar(entrada)
        datasets.append(d)
        if d["ficheiros"] == 0:
            problemas.append(f"{d['id']}: nenhum ficheiro em {d['caminho']}")
            estado = "AUSENTE"
        elif d["ultima_data"] is None and entrada["deteccao"] != "estatico":
            problemas.append(f"{d['id']}: não foi possível determinar a última data")
            estado = "SEM DATA"
        else:
            estado = d["ultima_data"] or "estático"
        mb = d["bytes"] / 1e6
        print(f"  {d['id']:24s} {d['ficheiros']:5d} fich. {mb:8.2f} MB  {estado}")

    manifesto = {
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repositorio": "https://github.com/tiagofelicia/dados-energia",
        "base_url": BASE_URL,
        "licenca": "CC BY 4.0",
        "licenca_url": "https://creativecommons.org/licenses/by/4.0/",
        "atribuicao": "Dados: Tiago Felícia — www.tiagofelicia.pt "
                      "(fontes originais: OMIE, REN, ENTSO-E, MIBGAS), "
                      "via dados.tiagofelicia.pt",
        "grupos": GRUPOS,
        "datasets": datasets,
        "totais": {
            "datasets": len(datasets),
            "ficheiros": sum(d["ficheiros"] for d in datasets),
            "bytes": sum(d["bytes"] for d in datasets),
        },
    }

    if problemas:
        print(f"\n⚠️  {len(problemas)} problema(s):")
        for x in problemas:
            print(f"     {x}")

    if args.verificar:
        print("\n(--verificar: nada foi escrito)")
        sys.exit(1 if problemas else 0)

    novo = json.dumps(manifesto, ensure_ascii=False, indent=1, sort_keys=True)
    antigo = None
    if os.path.exists(SAIDA):
        try:
            a = json.load(open(SAIDA, encoding="utf-8"))
            b = json.loads(novo)
            a.pop("gerado_em", None)
            b.pop("gerado_em", None)
            antigo = a == b
        except (OSError, ValueError):
            antigo = False
    if antigo is True:
        print(f"\n= data/manifest.json (sem alterações)")
    else:
        with open(SAIDA, "w", encoding="utf-8", newline="") as f:
            f.write(novo + "\n")
        t = manifesto["totais"]
        print(f"\n✓ data/manifest.json — {t['datasets']} datasets · "
              f"{t['ficheiros']} ficheiros · {t['bytes'] / 1e6:.0f} MB")

    if problemas:
        sys.exit(1)


if __name__ == "__main__":
    main()
