#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_precos_consumidor.py
Preços da eletricidade e do gás natural pagos pelas famílias e pelas empresas,
país a país, na Europa.

Fonte: Eurostat (API de disseminação, sem chave), as oito séries recolhidas ao
abrigo do Regulamento (UE) 2016/1952:

  nrg_pc_204    eletricidade, famílias, por semestre
  nrg_pc_205    eletricidade, empresas (não domésticos), por semestre
  nrg_pc_202    gás natural, famílias, por semestre
  nrg_pc_203    gás natural, empresas, por semestre
  nrg_pc_204_c  eletricidade, famílias, o preço dividido em componentes, por ano
  nrg_pc_205_c  eletricidade, empresas, componentes, por ano
  nrg_pc_202_c  gás natural, famílias, componentes, por ano
  nrg_pc_203_c  gás natural, empresas, componentes, por ano

O Eurostat publica o 1.º semestre em outubro e o 2.º em abril ou maio do ano
seguinte; os componentes de um ano saem em outubro do ano seguinte. Entre uma
publicação e outra os valores só mudam quando o Eurostat os revê.

USO
---
  python atualizar_precos_consumidor.py

SAÍDA
-----
  data/precos-consumidor/eletricidade_precos_familias.csv   (e _empresas, gas_precos_*)
    periodo            semestre: AAAA-S1 (janeiro a junho) ou AAAA-S2 (julho a dezembro)
    pais               código do Eurostat (PT, ES, EL = Grécia, UK, XK = Kosovo,
                       EU27_2020 = média da UE a 27, EA = média da zona euro)
    banda              faixa de consumo anual: DA–DE (eletricidade, famílias),
                       IA–IG (eletricidade, empresas), D1–D3 (gás, famílias),
                       I1–I6 (gás, empresas); TOT = todas as faixas juntas
    sem_impostos       €/kWh, sem impostos nem taxas
    sem_iva            €/kWh, sem o IVA e os outros impostos recuperáveis
    com_impostos       €/kWh, com todos os impostos e taxas
    pps_sem_impostos, pps_sem_iva, pps_com_impostos
                       o mesmo em paridade de poder de compra (PPS por kWh)
    estado             as marcas do Eurostat nos valores da linha, juntas:
                       e estimado, p provisório, d definição diferente,
                       b quebra de série, n pouco significativo, u pouco fiável

  data/precos-consumidor/eletricidade_componentes_familias.csv   (e _empresas, gas_componentes_*)
    ano, pais, banda   como acima (ano em vez de semestre); nas empresas há também
                       IA-IF (eletricidade) e I1-I5 (gás), as faixas todas menos a maior
    energia_fornecimento, redes, impostos_taxas (inclui o IVA), iva,
    impostos_renovaveis, impostos_capacidade, impostos_ambientais, impostos_nuclear,
    impostos_outros   em €/kWh; energia_fornecimento + redes + impostos_taxas dá o
                       preço com todos os impostos, e as cinco parcelas impostos_* mais
                       o iva dão o impostos_taxas (a menos de arredondamentos)
    reducoes_total, reducoes_renovaveis, reducoes_capacidade, reducoes_ambientais,
    reducoes_nuclear, reducoes_outras
                       as reduções e isenções declaradas à parte (TAX_*_ALLOW e
                       ALLOW_OTH no Eurostat), já descontadas nas parcelas de cima
    estado             como acima

  data/precos-consumidor/metadata.json
    a data da última atualização do Eurostat, o primeiro e o último período e o
    número de linhas de cada ficheiro, e, por grupo, o último período e o dia em
    que acaba (o manifesto lê-o para a última data)

Os preços começam no 1.º semestre de 2007, quando entraram as faixas de consumo
atuais; os componentes em 2017. Valores em euros e em PPS; a moeda nacional não
entra. Cada ficheiro só se reescreve se mudou, e nunca com menos períodos ou
muito menos linhas do que tinha, sem Portugal ou sem a média da UE no último
período, ou com valores fora do possível. Um ficheiro com problemas fica como
estava e o script sai com erro; os outros escrevem-se na mesma.
"""

import csv
import io
import json
import os
import re
import sys
import time

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA = os.path.join(ROOT_DIR, "data", "precos-consumidor")
META = os.path.join(PASTA, "metadata.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
NAVEGADOR = "https://ec.europa.eu/eurostat/databrowser/view/{codigo}/default/table?lang=en"
TENTATIVAS = 3
INICIO_PRECOS = "2007-S1"   # as faixas de consumo atuais começam aqui

# As faixas: código do Eurostat → o nome curto que fica no ficheiro, pela ordem
# em que aparecem. Os componentes da eletricidade das famílias chamam
# KWH_LE15000 à faixa DE (≥ 15 000 kWh), apesar do «LE»: o nome do Eurostat diz
# «15 000 kWh or over - band DE».
BANDAS_ELE_FAM = {"TOT_KWH": "TOT", "KWH_LT1000": "DA", "KWH1000-2499": "DB",
                  "KWH2500-4999": "DC", "KWH5000-14999": "DD", "KWH_GE15000": "DE"}
BANDAS_ELE_EMP = {"TOT_KWH": "TOT", "MWH_LT20": "IA", "MWH20-499": "IB", "MWH500-1999": "IC",
                  "MWH2000-19999": "ID", "MWH20000-69999": "IE", "MWH70000-149999": "IF",
                  "MWH_GE150000": "IG"}
BANDAS_GAS_FAM = {"TOT_GJ": "TOT", "GJ_LT20": "D1", "GJ20-199": "D2", "GJ_GE200": "D3"}
BANDAS_GAS_EMP = {"TOT_GJ": "TOT", "GJ_LT1000": "I1", "GJ1000-9999": "I2", "GJ10000-99999": "I3",
                  "GJ100000-999999": "I4", "GJ1000000-3999999": "I5", "GJ_GE4000000": "I6"}

# tipo: «precos» (semestral, três níveis de impostos, € e PPS) ou «componentes»
# (anual, em €). com_unidade: a série tem a dimensão unit (só se pede kWh; o gás
# também vem em GJ). As séries de componentes da eletricidade não a têm, e
# pedir unit=KWH dá erro 400.
SERIES = [
    dict(ficheiro="eletricidade_precos_familias.csv", codigo="nrg_pc_204", tipo="precos",
         grupo="eletricidade_precos", com_unidade=True, bandas=BANDAS_ELE_FAM),
    dict(ficheiro="eletricidade_precos_empresas.csv", codigo="nrg_pc_205", tipo="precos",
         grupo="eletricidade_precos", com_unidade=True, bandas=BANDAS_ELE_EMP),
    dict(ficheiro="gas_precos_familias.csv", codigo="nrg_pc_202", tipo="precos",
         grupo="gas_precos", com_unidade=True, bandas=BANDAS_GAS_FAM),
    dict(ficheiro="gas_precos_empresas.csv", codigo="nrg_pc_203", tipo="precos",
         grupo="gas_precos", com_unidade=True, bandas=BANDAS_GAS_EMP),
    dict(ficheiro="eletricidade_componentes_familias.csv", codigo="nrg_pc_204_c", tipo="componentes",
         grupo="eletricidade_componentes", com_unidade=False,
         bandas={**{k: v for k, v in BANDAS_ELE_FAM.items() if k != "KWH_GE15000"},
                 "KWH_LE15000": "DE"}),
    dict(ficheiro="eletricidade_componentes_empresas.csv", codigo="nrg_pc_205_c", tipo="componentes",
         grupo="eletricidade_componentes", com_unidade=False,
         bandas={"TOT_KWH": "TOT", "MWH_LE149999": "IA-IF",
                 **{k: v for k, v in BANDAS_ELE_EMP.items() if k != "TOT_KWH"}}),
    dict(ficheiro="gas_componentes_familias.csv", codigo="nrg_pc_202_c", tipo="componentes",
         grupo="gas_componentes", com_unidade=True, bandas=BANDAS_GAS_FAM),
    dict(ficheiro="gas_componentes_empresas.csv", codigo="nrg_pc_203_c", tipo="componentes",
         grupo="gas_componentes", com_unidade=True,
         bandas={"TOT_GJ": "TOT", "GJ_LE3999999": "I1-I5",
                 **{k: v for k, v in BANDAS_GAS_EMP.items() if k != "TOT_GJ"}}),
]

IMPOSTOS = {"X_TAX": "sem_impostos", "X_VAT": "sem_iva", "I_TAX": "com_impostos"}
COL_PRECOS = (["periodo", "pais", "banda"] + list(IMPOSTOS.values())
              + [f"pps_{c}" for c in IMPOSTOS.values()] + ["estado"])

COMPONENTES = {
    "NRG_SUP": "energia_fornecimento", "NETC": "redes", "TAX_FEE_LEV_CHRG": "impostos_taxas",
    "VAT": "iva", "TAX_RNW": "impostos_renovaveis", "TAX_CAP": "impostos_capacidade",
    "TAX_ENV": "impostos_ambientais", "TAX_NUC": "impostos_nuclear", "OTH": "impostos_outros",
    "TAX_FEE_LEV_CHRG_ALLOW": "reducoes_total", "TAX_RNW_ALLOW": "reducoes_renovaveis",
    "TAX_CAP_ALLOW": "reducoes_capacidade", "TAX_ENV_ALLOW": "reducoes_ambientais",
    "TAX_NUC_ALLOW": "reducoes_nuclear", "ALLOW_OTH": "reducoes_outras",
}
COL_COMPONENTES = ["ano", "pais", "banda"] + list(COMPONENTES.values()) + ["estado"]

# Limites do possível, em €/kWh (ou PPS/kWh). Um preço pode ser negativo: nos
# Países Baixos, as famílias que gastam menos de 1 000 kWh por ano pagaram
# −0,92 €/kWh em 2022 (a devolução fixa do imposto sobre a energia é maior do que
# a conta). Uma parcela de impostos também (as renováveis em Portugal em 2023
# devolveram dinheiro; os impostos das mesmas famílias neerlandesas, −1,46 €/kWh).
# O mais alto até 2026: 1,00 €/kWh, o gás das famílias pequenas na Suécia.
LIMITES = {"precos": (-2.0, 5.0), "componentes": (-3.0, 5.0)}
# Os países que têm de estar no último período de cada ficheiro
OBRIGATORIOS = ("PT", "EU27_2020")
# Um ficheiro novo com menos de 90 % das linhas do anterior não se escreve: o
# Eurostat não apaga séries de um semestre para o outro.
MINIMO_LINHAS = 0.9


def pedir(serie):
    """O JSON-stat da série, só com as faixas, as moedas e (se houver) o kWh que interessam."""
    params = [("format", "JSON"), ("lang", "EN")]
    params += [("currency", m) for m in (("EUR", "PPS") if serie["tipo"] == "precos" else ("EUR",))]
    if serie["com_unidade"]:
        params.append(("unit", "KWH"))
    params += [("nrg_cons", b) for b in serie["bandas"]]
    if serie["tipo"] == "precos":
        params.append(("sinceTimePeriod", INICIO_PRECOS))
    erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            r = requests.get(f"{API}/{serie['codigo']}", params=params, timeout=120,
                             headers={"Accept": "application/json"})
            if r.status_code == 200:
                j = r.json()
                if "value" in j and "dimension" in j:
                    return j
                erro = f"resposta sem dados: {str(j)[:200]}"
            else:
                erro = f"HTTP {r.status_code}: {r.text[:200]}"
        except (requests.RequestException, ValueError) as e:
            erro = str(e)[:200]
        if tentativa < TENTATIVAS:
            time.sleep(10 * tentativa)
    raise RuntimeError(f"Eurostat, {serie['codigo']}: {erro}")


def observacoes(j):
    """Cada valor do JSON-stat como (dicionário dimensão → código, valor, marca)."""
    ids, tamanhos = j["id"], j["size"]
    codigos = []
    for dim in ids:
        idx = j["dimension"][dim]["category"]["index"]
        por_pos = [None] * len(idx)
        for cod, pos in idx.items():
            por_pos[pos] = cod
        codigos.append(por_pos)
    marcas = j.get("status", {})
    for chave, valor in j["value"].items():
        if valor is None:
            continue
        resto, pos = int(chave), {}
        for i in range(len(ids) - 1, -1, -1):
            resto, p = divmod(resto, tamanhos[i])
            pos[ids[i]] = codigos[i][p]
        # As marcas vêm como «e», «p», «d|C»…: só interessam as letras minúsculas
        # (o «C», confidencial, só aparece sem valor)
        yield pos, valor, "".join(sorted(set(re.findall(r"[a-z]", marcas.get(chave, "")))))


def num(v):
    """O valor como o Eurostat o dá (4 casas), sem zeros à direita; vazio se não há."""
    if v is None:
        return ""
    s = ("%.6f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def linhas_precos(j, serie):
    linhas = {}
    for pos, valor, marca in observacoes(j):
        banda = serie["bandas"].get(pos["nrg_cons"])
        col = IMPOSTOS.get(pos["tax"])
        if banda is None or col is None or pos["currency"] not in ("EUR", "PPS"):
            continue
        k = (pos["time"], pos["geo"], banda)
        r = linhas.setdefault(k, {"periodo": k[0], "pais": k[1], "banda": banda, "_marcas": set()})
        r[col if pos["currency"] == "EUR" else f"pps_{col}"] = valor
        r["_marcas"].update(marca)
    return linhas


def linhas_componentes(j, serie):
    linhas = {}
    for pos, valor, marca in observacoes(j):
        banda = serie["bandas"].get(pos["nrg_cons"])
        col = COMPONENTES.get(pos["nrg_prc"])
        if banda is None or col is None or pos["currency"] != "EUR":
            continue
        k = (pos["time"], pos["geo"], banda)
        r = linhas.setdefault(k, {"ano": k[0], "pais": k[1], "banda": banda, "_marcas": set()})
        r[col] = valor
        r["_marcas"].update(marca)
    return linhas


def texto_csv(linhas, colunas, ordem_bandas):
    ordem = {b: i for i, b in enumerate(ordem_bandas)}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(colunas)
    for k in sorted(linhas, key=lambda k: (k[0], k[1], ordem.get(k[2], 99))):
        r = linhas[k]
        w.writerow([r[c] if c in colunas[:3] else
                    "".join(sorted(r["_marcas"])) if c == "estado" else num(r.get(c))
                    for c in colunas])
    return buf.getvalue()


def ler_csv(caminho):
    if not os.path.exists(caminho):
        return None
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        return f.read().replace("\r\n", "\n")


def resumo_csv(texto):
    """(linhas, períodos) de um CSV já escrito."""
    if not texto:
        return 0, []
    rs = list(csv.reader(io.StringIO(texto)))[1:]
    return len(rs), sorted({r[0] for r in rs})


def problemas(linhas, serie, antigo):
    erros = []
    if not linhas:
        return ["nenhum valor"]
    tempos = sorted({k[0] for k in linhas})
    ultimo = tempos[-1]
    for p in OBRIGATORIOS:
        if not any(k[0] == ultimo and k[1] == p for k in linhas):
            erros.append(f"{p} não está no último período ({ultimo})")
    lo, hi = LIMITES[serie["tipo"]]
    fora = [(k, c, v) for k, r in linhas.items() for c, v in r.items()
            if isinstance(v, float) and not lo <= v <= hi]
    if fora:
        erros.append(f"{len(fora)} valor(es) fora de [{lo}, {hi}]: "
                     + ", ".join(f"{k[0]} {k[1]} {k[2]} {c}={v}" for k, c, v in fora[:4]))
    n_ant, tempos_ant = resumo_csv(antigo)
    if tempos_ant and ultimo < tempos_ant[-1]:
        erros.append(f"o último período recuou de {tempos_ant[-1]} para {ultimo}")
    if tempos_ant and set(tempos_ant) - set(tempos):
        erros.append(f"perdia períodos: {', '.join(sorted(set(tempos_ant) - set(tempos))[:6])}")
    if n_ant and len(linhas) < MINIMO_LINHAS * n_ant:
        erros.append(f"{len(linhas)} linhas contra {n_ant} no ficheiro (menos de {MINIMO_LINHAS:.0%})")
    return erros


def fim_do_periodo(p):
    """O último dia de um semestre (AAAA-S1 → 30/06, AAAA-S2 → 31/12) ou de um ano."""
    if "-S" in p:
        return f"{p[:4]}-06-30" if p.endswith("S1") else f"{p[:4]}-12-31"
    return f"{p}-12-31"


def atualizado_eurostat(j):
    for a in j.get("extension", {}).get("annotation", []):
        if a.get("type") == "UPDATE_DATA" and a.get("date"):
            return a["date"]
    return j.get("updated")


def main():
    print("Preços ao consumidor (Eurostat)")
    os.makedirs(PASTA, exist_ok=True)
    try:
        meta_antiga = json.load(open(META, encoding="utf-8"))
    except (OSError, ValueError):
        meta_antiga = {}
    ficheiros_meta = dict(meta_antiga.get("ficheiros", {}))
    falhas = []

    for serie in SERIES:
        nome, caminho = serie["ficheiro"], os.path.join(PASTA, serie["ficheiro"])
        try:
            j = pedir(serie)
        except RuntimeError as e:
            print(f"  ✗ {nome}: {e}")
            falhas.append(nome)
            continue
        if serie["tipo"] == "precos":
            linhas, colunas = linhas_precos(j, serie), COL_PRECOS
        else:
            linhas, colunas = linhas_componentes(j, serie), COL_COMPONENTES
        antigo = ler_csv(caminho)
        erros = problemas(linhas, serie, antigo)
        if erros:
            print(f"  ✗ {nome}: o ficheiro não foi escrito")
            for e in erros:
                print(f"      {e}")
            falhas.append(nome)
            continue
        novo = texto_csv(linhas, colunas, list(serie["bandas"].values()))
        tempos = sorted({k[0] for k in linhas})
        ficheiros_meta[nome] = {
            "codigo_eurostat": serie["codigo"],
            "titulo_eurostat": j.get("label", ""),
            "url_eurostat": NAVEGADOR.format(codigo=serie["codigo"]),
            "atualizado_eurostat": atualizado_eurostat(j),
            "primeiro_periodo": tempos[0],
            "ultimo_periodo": tempos[-1],
            "linhas": len(linhas),
            "paises": len({k[1] for k in linhas}),
        }
        if novo == antigo:
            print(f"  = {nome}: sem alterações ({len(linhas)} linhas, {tempos[0]} a {tempos[-1]})")
            continue
        with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
            f.write(novo)
        print(f"  ✓ {nome}: {len(linhas)} linhas, {tempos[0]} a {tempos[-1]}")

    # O grupo está tão atualizado quanto o seu ficheiro mais atrasado
    grupos = {}
    for serie in SERIES:
        m = ficheiros_meta.get(serie["ficheiro"])
        if not m:
            continue
        g = grupos.setdefault(serie["grupo"], {"ultimo_periodo": m["ultimo_periodo"]})
        g["ultimo_periodo"] = min(g["ultimo_periodo"], m["ultimo_periodo"])
    for g in grupos.values():
        g["data_fim"] = fim_do_periodo(g["ultimo_periodo"])
    meta = {
        "fonte": "Eurostat (Regulamento (UE) 2016/1952)",
        "nota": "Preços em €/kWh e em PPS/kWh. Ver o README.",
        "grupos": dict(sorted(grupos.items())),
        "ficheiros": {s["ficheiro"]: ficheiros_meta[s["ficheiro"]] for s in SERIES
                      if s["ficheiro"] in ficheiros_meta},
    }
    if meta != meta_antiga:
        with open(META, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"  ✓ metadata.json")
    if falhas:
        print(f"ERRO em {len(falhas)} ficheiro(s): {', '.join(falhas)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
