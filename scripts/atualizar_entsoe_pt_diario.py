#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_entsoe_pt_diario.py
Produção, consumo e trocas de Portugal segundo a ENTSO-E, dia a dia, desde 2015.

Serve para comparar a ENTSO-E com a REN (página /ren-vs-entsoe do site). Os ficheiros
semanais de data/producao/producao-entsoe/pt/ só começam em 2026 e trazem previsões,
preços e trocas por vizinho; estes são só os totais diários, desde que a plataforma
arrancou.

Fonte: ENTSO-E Transparency Platform. Obrigatório: variável de ambiente ENTSOE_TOKEN.
  A75 / A16   produção por tipo (art. 16.1.B e C do Regulamento (UE) n.º 543/2013)
  A65 / A16   consumo (Total Load, art. 6.1.A)
  A11         fluxos físicos Espanha → Portugal e Portugal → Espanha (art. 12.1.G)

USO
---
  python atualizar_entsoe_pt_diario.py                       # últimos 14 dias (revisões incluídas)
  python atualizar_entsoe_pt_diario.py --dias 400            # revisão longa (corre ao domingo)
  python atualizar_entsoe_pt_diario.py --backfill            # tudo desde 01/01/2015
  python atualizar_entsoe_pt_diario.py --desde 2020-01-01    # de uma data até ontem

Sem ficheiros na pasta, faz sempre o backfill. Só dias completos: o último é ontem
(hora de Lisboa).

SAÍDA
-----
  data/producao/entsoe-pt-diario/entsoe_pt_diario_AAAA.csv   um ficheiro por ano

    dia, data_iso                     DD/MM/AAAA e AAAA-MM-DD (dia de Lisboa)
    biomassa_gwh                      B01 Biomass
    gas_gwh                           B04 Fossil Gas
    carvao_gwh                        B05 Fossil Hard coal
    petroleo_gwh                      B06 Fossil Oil
    residuos_gwh                      B17 Waste
    outras_gwh                        B20 Other
    outras_renov_gwh                  B15 Other renewable
    hidrica_fio_gwh                   B11 Hydro Run-of-river and poundage
    hidrica_albufeira_gwh             B12 Hydro Water Reservoir
    bombagem_producao_gwh             B10 Hydro Pumped Storage, produção. A ENTSO-E põe aqui
                                      TODA a produção das centrais reversíveis, também a da
                                      água que chegou à albufeira pelo rio; a REN só conta
                                      como bombagem a água antes bombeada
    bombagem_consumo_gwh              B10, consumo das bombas
    eolica_terra_gwh                  B19 Wind Onshore
    eolica_mar_gwh                    B18 Wind Offshore
    solar_gwh                         B16 Solar
    ondas_gwh                         B13 Marine
    baterias_descarga_gwh             B25 Energy storage, descarga
    baterias_carga_gwh                B25 Energy storage, carga
    consumo_centrais_gwh              o que a ENTSO-E publica como consumo das outras centrais
                                      (gás, fio de água, albufeira…: serviços auxiliares)
    outros_tipos_gwh                  qualquer outro código (não devia haver; fica no log)
    consumo_gwh                       A65 Total Load
    importacao_gwh                    A11 Espanha → Portugal   } só nas horas com os dois
    exportacao_gwh                    A11 Portugal → Espanha   } sentidos (o saldo é a diferença)
    horas_producao                    horas do dia com algum valor de produção
    horas_consumo                     horas do dia com valor de consumo
    horas_trocas                      horas do dia com valor nos dois sentidos das trocas

  A ENTSO-E publica potência média (MW) por hora ou por quarto de hora; cada valor vale
  para o seu intervalo e a energia é MW × duração. Um tipo que não veio nesse dia fica
  vazio (não é zero). Um dia de 23 ou 25 horas (mudança de hora) tem 23 ou 25 horas.

Um dia refeito substitui o que lá estava, por grupo (produção, consumo, trocas), mas só
se o grupo vier com pelo menos tantas horas como antes: uma resposta incompleta da API
não apaga um dia que estava completo. Só se reescreve um ficheiro se alguma coisa mudou.
"""

import csv
import io
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import entsoe_client  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PASTA = os.path.join(ROOT_DIR, "data", "producao", "entsoe-pt-diario")
TZ = ZoneInfo("Europe/Lisbon")
INICIO_BACKFILL = date(2015, 1, 1)   # a ENTSO-E tem a série desde o arranque da plataforma
DIAS_INCREMENTAL = 14
QH = 900                             # segundos de um quarto de hora: a grelha comum
PAUSA = 0.4                          # segundos entre pedidos

# Nome canónico do entsoe_client (PSR_TYPES, + ' consumption' no sentido da carga) → coluna
TIPOS = {
    "Biomass": "biomassa_gwh",
    "Fossil gas": "gas_gwh",
    "Fossil hard coal": "carvao_gwh",
    "Fossil oil": "petroleo_gwh",
    "Waste": "residuos_gwh",
    "Others": "outras_gwh",
    "Other renewables": "outras_renov_gwh",
    "Hydro Run-of-River": "hidrica_fio_gwh",
    "Hydro water reservoir": "hidrica_albufeira_gwh",
    "Hydro pumped storage": "bombagem_producao_gwh",
    "Hydro pumped storage consumption": "bombagem_consumo_gwh",
    "Wind onshore": "eolica_terra_gwh",
    "Wind offshore": "eolica_mar_gwh",
    "Solar": "solar_gwh",
    "Marine": "ondas_gwh",
    "Energy storage": "baterias_descarga_gwh",
    "Energy storage consumption": "baterias_carga_gwh",
}
COL_PRODUCAO = list(dict.fromkeys(TIPOS.values())) + ["consumo_centrais_gwh", "outros_tipos_gwh"]
GRUPOS = {
    "producao": COL_PRODUCAO,
    "consumo": ["consumo_gwh"],
    "trocas": ["importacao_gwh", "exportacao_gwh"],
}
COLUNAS = (["dia", "data_iso"] + COL_PRODUCAO + GRUPOS["consumo"] + GRUPOS["trocas"]
           + ["horas_producao", "horas_consumo", "horas_trocas"])


# ---------- Leitura do XML ----------

def _resolucao_min(texto):
    texto = (texto or "").strip()
    if texto == "PT1H":
        return 60
    if texto.startswith("PT") and texto.endswith("M") and texto[2:-1].isdigit():
        return int(texto[2:-1])
    return None


def _instante(texto):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(texto, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return None


def series_em_quartos(xml_text, chave_de):
    """XML da ENTSO-E → {chave: {início do quarto de hora (unix UTC): MW}}.

    Cada ponto vale para o seu intervalo (60 ou 15 min, conforme o <resolution> do Period),
    que se reparte pelos quartos de hora que cobre; assim uma série horária e uma de 15 min
    somam-se sem perder 3 em cada 4 quartos. Com várias TimeSeries para a mesma chave e o
    mesmo quarto de hora (revisões), fica a última do documento: não se somam.

    Curva A03: a ENTSO-E não envia o ponto que repete o anterior; aqui cada posição em falta
    até ao fim do Period fica com o valor da anterior (o Period destes documentos acaba onde
    acabam os dados publicados).

    chave_de(TimeSeries) → str ou None (None = ignorar a série).
    """
    if not xml_text or "No matching data found" in xml_text or "<Acknowledgement_MarketDocument" in xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        print("  Aviso: resposta da ENTSO-E que não é XML válido")
        return {}
    strip = entsoe_client._strip_ns
    saida = {}
    for ts_elem in root.iter():
        if strip(ts_elem.tag) != "TimeSeries":
            continue
        chave = chave_de(ts_elem)
        if chave is None:
            continue
        curva = next(((c.text or "").strip() for c in ts_elem if strip(c.tag) == "curveType"), "")
        destino = saida.setdefault(chave, {})
        for period in ts_elem:
            if strip(period.tag) != "Period":
                continue
            inicio = fim = res = None
            pontos = {}
            for c in period:
                tg = strip(c.tag)
                if tg == "timeInterval":
                    for c2 in c:
                        if strip(c2.tag) == "start":
                            inicio = _instante(c2.text)
                        elif strip(c2.tag) == "end":
                            fim = _instante(c2.text)
                elif tg == "resolution":
                    res = _resolucao_min(c.text)
                elif tg == "Point":
                    pos = qty = None
                    for c3 in c:
                        if strip(c3.tag) == "position":
                            pos = int(c3.text)
                        elif strip(c3.tag) == "quantity":
                            qty = float(c3.text)
                    if pos is not None and qty is not None:
                        pontos[pos] = qty
            if inicio is None or not res or not pontos:
                continue
            if curva == "A03":
                ultima = max(pontos)
                if fim is not None:
                    ultima = max(ultima, int((fim - inicio).total_seconds() // (res * 60)))
                valor, cheio = None, {}
                for pos in range(min(pontos), ultima + 1):
                    valor = pontos.get(pos, valor)
                    cheio[pos] = valor
                pontos = cheio
            t0 = int(inicio.timestamp())
            if res >= 15:
                passos = res // 15
                for pos, mw in pontos.items():
                    base = t0 + (pos - 1) * res * 60
                    for k in range(passos):
                        destino[base + k * QH] = mw
            else:
                # Resolução abaixo de 15 min (não acontece em Portugal): média por quarto de hora
                somas = {}
                for pos, mw in pontos.items():
                    t = t0 + (pos - 1) * res * 60
                    q = t - t % QH
                    s = somas.setdefault(q, [0.0, 0])
                    s[0] += mw
                    s[1] += 1
                for q, (s, n) in somas.items():
                    destino[q] = s / n
    return saida


def _chave_tipo(ts_elem):
    return entsoe_client._extract_psr_with_direction(ts_elem)


def _chave_unica(_ts_elem):
    return "valor"


def _pedir(params, inicio_utc, fim_utc, token):
    params = dict(params, periodStart=entsoe_client._fmt_period(inicio_utc),
                  periodEnd=entsoe_client._fmt_period(fim_utc))
    xml = entsoe_client._get(params, token)
    time.sleep(PAUSA)
    return xml


def recolher(inicio_utc, fim_utc, token):
    """Os quatro pedidos de um intervalo → {"producao": {nome: {q: MW}}, "consumo": {q: MW},
    "importacao": {q: MW}, "exportacao": {q: MW}}."""
    pt = entsoe_client.EIC_MAP["pt"]
    es = entsoe_client.EIC_MAP["es"]
    prod = series_em_quartos(_pedir({"documentType": "A75", "processType": "A16", "in_Domain": pt},
                                    inicio_utc, fim_utc, token), _chave_tipo)
    cons = series_em_quartos(_pedir({"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": pt},
                                    inicio_utc, fim_utc, token), _chave_unica).get("valor", {})
    imp = series_em_quartos(_pedir({"documentType": "A11", "in_Domain": pt, "out_Domain": es},
                                   inicio_utc, fim_utc, token), _chave_unica).get("valor", {})
    exp = series_em_quartos(_pedir({"documentType": "A11", "in_Domain": es, "out_Domain": pt},
                                   inicio_utc, fim_utc, token), _chave_unica).get("valor", {})
    return {"producao": prod, "consumo": cons, "importacao": imp, "exportacao": exp}


# ---------- Agregação por dia ----------

def _dia_local(q, cache={}):
    # O dia de Lisboa de um quarto de hora; a hora UTC inteira chega (o fuso é de horas inteiras)
    h = q - q % 3600
    d = cache.get(h)
    if d is None:
        d = cache[h] = datetime.fromtimestamp(h, TZ).date()
    return d


def dias_de_quartos(dados, desconhecidos=None):
    """{"producao", "consumo", "importacao", "exportacao"} em quartos de hora → {data: linha}.
    Só aparecem os dias com algum valor. desconhecidos (set) recebe os nomes sem coluna."""
    gwh = {}      # data → coluna → MWh
    quartos = {}  # data → grupo → set(q)
    imp, exp = dados["importacao"], dados["exportacao"]
    if bool(imp) != bool(exp):
        # Um sentido sem nenhum valor no pedido inteiro: sem trocas nesse sentido (zero), e não
        # uma lacuna; de outro modo o dia perdia o saldo
        print(f"  Aviso: A11 sem {'exportação' if imp else 'importação'} no pedido inteiro; conta como 0")
        dados = dict(dados, importacao=imp or {q: 0.0 for q in exp}, exportacao=exp or {q: 0.0 for q in imp})
    for nome, serie in dados["producao"].items():
        col = TIPOS.get(nome)
        if col is None and nome.endswith(" consumption"):
            # Gás, fio de água, albufeira… também têm um sentido de carga (MW pequenos)
            col = "consumo_centrais_gwh"
        elif col is None:
            col = "outros_tipos_gwh"
            if desconhecidos is not None:
                desconhecidos.add(nome)
        for q, mw in serie.items():
            d = _dia_local(q)
            g = gwh.setdefault(d, {})
            g[col] = g.get(col, 0.0) + mw * 0.25
            quartos.setdefault(d, {}).setdefault("producao", set()).add(q)
    for q, mw in dados["consumo"].items():
        d = _dia_local(q)
        g = gwh.setdefault(d, {})
        g["consumo_gwh"] = g.get("consumo_gwh", 0.0) + mw * 0.25
        quartos.setdefault(d, {}).setdefault("consumo", set()).add(q)
    # Trocas só nos quartos de hora com os dois sentidos, para o saldo não misturar horas
    imp, exp = dados["importacao"], dados["exportacao"]
    for q in imp.keys() & exp.keys():
        d = _dia_local(q)
        g = gwh.setdefault(d, {})
        g["importacao_gwh"] = g.get("importacao_gwh", 0.0) + imp[q] * 0.25
        g["exportacao_gwh"] = g.get("exportacao_gwh", 0.0) + exp[q] * 0.25
        quartos.setdefault(d, {}).setdefault("trocas", set()).add(q)
    linhas = {}
    for d, g in gwh.items():
        qs = quartos.get(d, {})
        linha = {"dia": d.strftime("%d/%m/%Y"), "data_iso": d.isoformat(),
                 "horas_producao": _horas(len(qs.get("producao", ()))),
                 "horas_consumo": _horas(len(qs.get("consumo", ()))),
                 "horas_trocas": _horas(len(qs.get("trocas", ())))}
        for col in COL_PRODUCAO + GRUPOS["consumo"] + GRUPOS["trocas"]:
            linha[col] = f"{g[col] / 1000:.3f}" if col in g else ""
        linhas[d.isoformat()] = linha
    return linhas


def _horas(n_quartos):
    h = n_quartos / 4
    return str(int(h)) if h == int(h) else f"{h:g}"


# ---------- Ficheiros ----------

def caminho_ano(ano):
    return os.path.join(PASTA, f"entsoe_pt_diario_{ano}.csv")


def ler_ano(ano):
    c = caminho_ano(ano)
    if not os.path.exists(c):
        return {}
    with open(c, "r", encoding="utf-8-sig", newline="") as f:
        return {r["data_iso"]: {k: r.get(k, "") or "" for k in COLUNAS} for r in csv.DictReader(f)}


def texto_csv(linhas):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUNAS, lineterminator="\n")
    w.writeheader()
    for k in sorted(linhas):
        w.writerow(linhas[k])
    return buf.getvalue()


def juntar(existente, nova):
    """Junta um dia refeito ao que estava, grupo a grupo. Devolve (linha, [grupos mantidos])."""
    if existente is None:
        return nova, []
    linha = dict(existente)
    mantidos = []
    for grupo, cols in GRUPOS.items():
        h_novo = float(nova[f"horas_{grupo}"] or 0)
        h_antigo = float(existente.get(f"horas_{grupo}") or 0)
        if h_novo > 0 and h_novo >= h_antigo:
            for c in cols:
                linha[c] = nova[c]
            linha[f"horas_{grupo}"] = nova[f"horas_{grupo}"]
        elif h_antigo > h_novo:
            mantidos.append(f"{grupo} {h_antigo:g} h > {h_novo:g} h")
    return linha, mantidos


def meses(inicio, fim):
    """[inicio, fim) partido em meses civis: um pedido de cada tipo por mês."""
    a = inicio
    while a < fim:
        prox = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
        b = min(prox, fim)
        yield a, b
        a = b


def utc_meia_noite(d):
    return datetime(d.year, d.month, d.day, tzinfo=TZ).astimezone(timezone.utc)


def main():
    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        print("ERRO: variável de ambiente ENTSOE_TOKEN não definida.")
        sys.exit(1)

    args = sys.argv[1:]
    hoje = datetime.now(TZ).date()
    sem_ficheiros = not (os.path.isdir(PASTA) and any(n.endswith(".csv") for n in os.listdir(PASTA)))
    if "--backfill" in args or sem_ficheiros:
        inicio = INICIO_BACKFILL
    elif "--desde" in args:
        inicio = date.fromisoformat(args[args.index("--desde") + 1])
    else:
        dias = int(args[args.index("--dias") + 1]) if "--dias" in args else DIAS_INCREMENTAL
        inicio = hoje - timedelta(days=dias)
    fim = hoje   # exclusivo: o último dia é ontem
    backfill = inicio == INICIO_BACKFILL
    print(f"ENTSO-E Portugal, dia a dia (A75 + A65 + A11): {inicio} → {fim - timedelta(days=1)}"
          f"{' (backfill)' if backfill else ''}")

    novas, desconhecidos = {}, set()
    for a, b in meses(inicio, fim):
        dados = recolher(utc_meia_noite(a), utc_meia_noite(b), token)
        linhas = dias_de_quartos(dados, desconhecidos)
        # Os quartos de hora fora do intervalo (a API pode devolver mais) não entram
        linhas = {k: v for k, v in linhas.items() if a.isoformat() <= k < b.isoformat()}
        n = (b - a).days
        hp = sum(float(l["horas_producao"]) for l in linhas.values())
        hc = sum(float(l["horas_consumo"]) for l in linhas.values())
        ht = sum(float(l["horas_trocas"]) for l in linhas.values())
        print(f"  {a} → {b}: {len(linhas)}/{n} dias · produção {hp:g} h · consumo {hc:g} h · trocas {ht:g} h")
        novas.update(linhas)
    if desconhecidos:
        print(f"  Aviso: tipos sem coluna própria, somados em outros_tipos_gwh: {', '.join(sorted(desconhecidos))}")
    if not novas:
        # No modo incremental pode acontecer (a API sem resposta); num backfill é um problema
        print("Nenhum dia devolvido pela ENTSO-E.")
        sys.exit(1 if backfill else 0)

    os.makedirs(PASTA, exist_ok=True)
    for ano in sorted({int(k[:4]) for k in novas}):
        existentes = ler_ano(ano)
        antigo = texto_csv(existentes) if existentes else None
        avisos = []
        for k, linha in novas.items():
            if int(k[:4]) != ano:
                continue
            existentes[k], mantidos = juntar(existentes.get(k), linha)
            if mantidos:
                avisos.append(f"{k} ({'; '.join(mantidos)})")
        if avisos:
            print(f"  Aviso: {len(avisos)} dia(s) de {ano} com menos horas do que antes; fica o que estava: "
                  f"{', '.join(avisos[:5])}{' …' if len(avisos) > 5 else ''}")
        dias_ano = sorted(existentes)
        falta = [(date.fromisoformat(x) + timedelta(days=1)).isoformat()
                 for x, y in zip(dias_ano, dias_ano[1:])
                 if date.fromisoformat(y) - date.fromisoformat(x) != timedelta(days=1)]
        if falta:
            print(f"  Aviso: dias em falta em {ano} (o 1.º de cada lacuna): {', '.join(falta[:10])}")
        novo = texto_csv(existentes)
        rel = os.path.relpath(caminho_ano(ano), ROOT_DIR)
        if novo == antigo:
            print(f"= {rel} sem alterações ({len(existentes)} dias)")
            continue
        with open(caminho_ano(ano), "w", encoding="utf-8-sig", newline="") as f:
            f.write(novo)
        print(f"✓ {rel}: {len(existentes)} dias, o último de {dias_ano[-1]}")


if __name__ == "__main__":
    main()
