#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_albufeiras_snirh.py
Água guardada em cada albufeira hidroelétrica, dia a dia.

Fonte: SNIRH — Sistema Nacional de Informação de Recursos Hídricos, da Agência Portuguesa do
Ambiente (snirh.apambiente.pt), «Dados de base» da rede hidrométrica: o parâmetro «Volume
armazenado na última hora» (dam3), um valor por dia, das 23:00. Não precisa de chave; o SNIRH
pede que se refira a origem dos dados.

As albufeiras são as 56 com uso para energia: as do boletim semanal de albufeiras da APA que têm
«energia» no uso (as das centrais hidroelétricas e as de rega ou abastecimento que também têm
central) e as de centrais que não entram nesse boletim (as três do Tâmega, Alto Ceira e Pedrógão).
As das centrais hidroelétricas têm um valor por dia; as de rega, um por semana, à segunda-feira.

ONDE CORRE
----------
Num PC em Portugal, por tarefa agendada, e não no GitHub Actions: o SNIRH só aceita ligações de
Portugal (verificado a 31/10/2026 com o check-host.net: responde de PT; de Espanha, França,
Alemanha, Reino Unido e EUA dá Forbidden ou timeout). Os dados chegam ao GitHub por push.
Se os 3 primeiros pedidos ficarem sem resposta, o script desiste logo (PC fora de Portugal).

USO
---
  python atualizar_albufeiras_snirh.py              # últimos 30 dias de cada albufeira (revisões incluídas)
  python atualizar_albufeiras_snirh.py --backfill   # tudo desde 2001
  Uma albufeira acrescentada à lista (sem valores no ficheiro) é recolhida desde 2001 mesmo sem --backfill.

SAÍDA
-----
  data/producao/albufeiras_snirh_diario.csv
    data        AAAA-MM-DD
    <coluna>    uma por albufeira: volume armazenado nesse dia (hm3, 2 casas); vazio sem valor

  data/producao/albufeiras_snirh.csv — as albufeiras
    coluna, codigo_snirh, nome, bacia
    capacidade_hm3            capacidade total, do boletim semanal de albufeiras da APA (vazio nas que lá não estão)
    maximo_registado_hm3      o maior volume da série, e data_maximo
    referencia_hm3, referencia o volume que conta como 100% (a capacidade ou o máximo registado)
    inicio_serie, ultimo_dia  primeiro e último dia com valor
    frequencia                diária ou semanal (o intervalo mediano entre os últimos 9 valores)
    latitude, longitude       a barragem (graus decimais, WGS84)

  As centrais de cada albufeira (potência e produção) estão em data/producao/centrais_hidricas.csv,
  um ficheiro estático, revisto à mão quando sai um anuário novo da APREN.

Os volumes a zero ou negativos são falhas de leitura (o Baixo Sabor e o Ribeiradio aparecem a 0 em
17/06/2018) e ficam vazios. Só se reescreve um ficheiro se alguma coisa mudou.
"""

import csv
import io
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

PASTA = os.path.join(ROOT_DIR, "data", "producao")
CAMINHO_DIARIO = os.path.join(PASTA, "albufeiras_snirh_diario.csv")
CAMINHO_META = os.path.join(PASTA, "albufeiras_snirh.csv")
URL_CSV = ("https://snirh.apambiente.pt/snirh/_dadosbase/site/paraCSV/dados_csv.php"
           "?sites={site}&pars={par}&tmin={tmin}&tmax={tmax}&formato=csv")
PAR_VOLUME_DIARIO = "354895398"   # Volume armazenado na última hora (dam3)
INICIO_BACKFILL = date(2001, 1, 1)   # antes de 2001 o SNIRH só tem um valor por mês
DIAS_INCREMENTAL = 30
PAUSA = 0.5   # segundos entre pedidos

# coluna, código SNIRH, site (id interno do SNIRH), nome, bacia, capacidade total (dam3, do boletim semanal
# de albufeiras da APA de 19/10/2026; None = não está no boletim). Por bacia, de norte para sul, e por tamanho.
ALBUFEIRAS = [
    ("alto_lindoso", "02H/01A", 1627743428, "Alto Lindoso", "Lima", 379000),
    ("touvedo", "03G/01A", 1627759476, "Touvedo", "Lima", 15500),
    ("alto_rabagao", "03J/03A", 1627743430, "Alto Rabagão", "Cávado", 568700),
    ("paradela", "03J/01A", 1627758928, "Paradela", "Cávado", 164400),
    ("canicada", "04H/01A", 1627743674, "Caniçada", "Cávado", 159300),
    ("vilarinho_das_furnas", "03H/01A", 1627759560, "Vilarinho das Furnas", "Cávado", 117690),
    ("venda_nova", "03J/04A", 1627759522, "Venda Nova", "Cávado", 94500),
    ("salamonde", "03I/01A", 1627759378, "Salamonde", "Cávado", 65000),
    ("guilhofrei", "04I/01A", 1627758686, "Guilhofrei", "Ave", 21200),
    ("baixo_sabor", "06O/09A", 7554777518, "Baixo Sabor", "Douro", 1095000),
    ("carrapatelo", "07I/01A", 1627743682, "Carrapatelo", "Douro", 150200),
    ("alto_tamega", "04K/04A", 11508860178, "Alto Tâmega", "Douro", None),
    ("bemposta", "06S/01A", 1627743548, "Bemposta", "Douro", 128800),
    ("torrao", "07H/01A", 1627759468, "Torrão", "Douro", 123900),
    ("crestuma_lever", "07G/01A", 1627751912, "Crestuma-Lever", "Douro", 110000),
    ("foz_tua", "06M/06A", 9321904688, "Foz Tua", "Douro", 106100),
    ("vilar_tabuaco", "08L/01A", 1627759554, "Vilar-Tabuaço", "Douro", 99750),
    ("valeira", "07M/02A", 1627759510, "Valeira", "Douro", 98500),
    ("regua", "07K/01A", 1627759340, "Régua", "Douro", 95000),
    ("pocinho", "07O/02A", 1627758976, "Pocinho", "Douro", 82900),
    ("picote", "05S/01A", 1627758968, "Picote", "Douro", 62700),
    ("daivoes", "04J/08A", 9978398426, "Daivões", "Douro", None),
    ("miranda", "05T/01A", 1627758786, "Miranda", "Douro", 28000),
    ("gouvaes", "05K/02A", 10908513554, "Gouvães", "Douro", None),
    ("varosa", "07K/02A", 1627759512, "Varosa", "Douro", 12943),
    ("serra_serrada", "02Q/01A", 1627759434, "Serra Serrada", "Douro", 1680),
    ("ribeiradio", "09H/06A", 7554777512, "Ribeiradio", "Vouga", 136400),
    ("aguieira", "11H/01A", 1627743384, "Aguieira", "Mondego", 423000),
    ("fronhas", "12I/01A", 1627758668, "Fronhas", "Mondego", 62100),
    ("raiva", "12H/01A", 1627759328, "Raiva", "Mondego", 24400),
    ("lagoa_comprida", "11L/03A", 1627758736, "Lagoa Comprida", "Mondego", 13880),
    ("caldeirao", "10M/01A", 1627743662, "Caldeirão", "Mondego", 5500),
    ("vale_do_rossim", "11L/02A", 1627759502, "Vale do Rossim", "Mondego", 3500),
    ("alto_ceira", "12J/01A", 1627743426, "Alto Ceira", "Mondego", None),
    ("lagoacho", "11L/01A", 1627758738, "Lagoacho", "Mondego", 1525),
    ("castelo_de_bode", "16H/01A", 1627743896, "Castelo de Bode", "Tejo", 1095000),
    ("cabril", "14I/01A", 1627743586, "Cabril", "Tejo", 720000),
    ("maranhao", "19J/01A", 1627758764, "Maranhão", "Tejo", 205400),
    ("montargil", "19H/01A", 1627758816, "Montargil", "Tejo", 164300),
    ("pracana", "16K/01A", 1627759292, "Pracana", "Tejo", 111900),
    ("fratel", "16K/02A", 1627758658, "Fratel", "Tejo", 93000),
    ("idanha", "14N/01A", 1627758730, "Idanha", "Tejo", 78100),
    ("santa_luzia", "13J/01A", 1627759390, "Santa Luzia", "Tejo", 53700),
    ("bouca", "14H/01A", 1627743562, "Bouçã", "Tejo", 48400),
    ("povoa", "17L/01A", 1627759290, "Póvoa", "Tejo", 22000),
    ("belver", "17J/01A", 1627743544, "Belver", "Tejo", 20570),
    ("alvito", "24J/01A", 1627743440, "Alvito", "Sado", 132500),
    ("roxo", "26I/01A", 1627759368, "Roxo", "Sado", 96311),
    ("odivelas", "24I/01A", 1627758906, "Odivelas", "Sado", 96000),
    ("pego_do_altar", "23G/01A", 1627758948, "Pego do Altar", "Sado", 94000),
    ("vale_do_gaio", "24H/01A", 1627759492, "Vale do Gaio", "Sado", 63000),
    ("alqueva", "24M/07A", 1627743416, "Alqueva", "Guadiana", 4150000),
    ("caia", "20O/01A", 1627743648, "Caia", "Guadiana", 203000),
    ("pedrogao", "25L/01A", 1627758944, "Pedrógão", "Guadiana", None),
    ("arade", "30G/02A", 1627743460, "Arade", "Arade", 28380),
    ("bravura", "30E/01A", 1627743568, "Bravura", "Ribeiras do Algarve", 34825),
]
# Localização da barragem (latitude, longitude, WGS84): a lista de características de albufeiras do SNIRH, pelo
# código da estação; as que lá não estão com este código, o mapa de barragens da CNPGB; o Baixo Sabor, o paredão
# do escalão de montante no OpenStreetMap (igual ao Wikidata; a da CNPGB ficava 0,8 km a jusante). Conferidas a
# 01/11/2026 com os paredões desenhados no OpenStreetMap: todas a menos de 200 m (mediana de 7 m).
COORDENADAS = {
    "alto_lindoso": (41.8722, -8.2027),
    "touvedo": (41.8122, -8.3538),
    "alto_rabagao": (41.7379, -7.854),
    "paradela": (41.7669, -7.9501),
    "canicada": (41.6525, -8.2313),
    "vilarinho_das_furnas": (41.7634, -8.2098),
    "venda_nova": (41.6812, -7.9848),
    "salamonde": (41.6915, -8.0915),
    "guilhofrei": (41.5856, -8.1377),
    "baixo_sabor": (41.2283, -7.0128),   # OpenStreetMap
    "carrapatelo": (41.0856, -8.131),
    "alto_tamega": (41.5743, -7.7312),   # CNPGB
    "bemposta": (41.3012, -6.4699),
    "torrao": (41.0971, -8.2614),
    "crestuma_lever": (41.0729, -8.4868),
    "foz_tua": (41.2189, -7.4222),   # CNPGB
    "vilar_tabuaco": (40.9872, -7.5348),
    "valeira": (41.1603, -7.3751),
    "regua": (41.1462, -7.7399),
    "pocinho": (41.1352, -7.1143),
    "picote": (41.3802, -6.3518),
    "daivoes": (41.5256, -7.8634),   # CNPGB
    "miranda": (41.489, -6.2639),
    "gouvaes": (41.4942, -7.7271),   # CNPGB
    "varosa": (41.1204, -7.7756),
    "serra_serrada": (41.9613, -6.7722),
    "ribeiradio": (40.7437, -8.3197),   # CNPGB
    "aguieira": (40.3407, -8.1967),
    "fronhas": (40.2463, -8.1638),
    "raiva": (40.3097, -8.249),
    "lagoa_comprida": (40.364, -7.6492),
    "caldeirao": (40.5317, -7.3302),
    "vale_do_rossim": (40.3996, -7.5885),
    "alto_ceira": (40.1892, -7.8473),   # CNPGB
    "lagoacho": (40.3861, -7.6203),   # CNPGB
    "castelo_de_bode": (39.543, -8.3189),
    "cabril": (39.9178, -8.1328),
    "maranhao": (39.015, -7.9759),
    "montargil": (39.0532, -8.1753),
    "pracana": (39.565, -7.812),
    "fratel": (39.5434, -7.8027),
    "idanha": (39.9451, -7.2014),
    "santa_luzia": (40.0897, -7.8582),
    "bouca": (39.8537, -8.2198),
    "povoa": (39.4769, -7.5554),
    "belver": (39.4797, -7.9985),
    "alvito": (38.279, -7.9106),
    "roxo": (37.9296, -8.0819),
    "odivelas": (38.1848, -8.1153),
    "pego_do_altar": (38.4187, -8.392),
    "vale_do_gaio": (38.2481, -8.2955),
    "alqueva": (38.1973, -7.4953),
    "caia": (38.9978, -7.1493),
    "pedrogao": (38.1107, -7.6304),   # CNPGB
    "arade": (37.2395, -8.3763),
    "bravura": (37.2017, -8.6978),
}
COLUNAS_META = ["coluna", "codigo_snirh", "nome", "bacia", "capacidade_hm3", "maximo_registado_hm3",
                "data_maximo", "referencia_hm3", "referencia", "inicio_serie", "ultimo_dia", "frequencia",
                "latitude", "longitude"]


def pedir(url):
    """Texto da resposta (latin-1), com 3 tentativas."""
    for tentativa in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dados-energia (tiagofelicia.pt)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode("latin-1")
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ tentativa {tentativa + 1}: {e}")
            time.sleep(5 * (tentativa + 1))
    return None


def volumes(site, inicio, fim):
    """{AAAA-MM-DD: hm3} de uma albufeira; None se o pedido falhou. Linhas: 'DD/MM/AAAA HH:MM,valor,FLAG,'."""
    t = pedir(URL_CSV.format(site=site, par=PAR_VOLUME_DIARIO,
                             tmin=inicio.strftime("%d/%m/%Y"), tmax=fim.strftime("%d/%m/%Y")))
    if t is None or "Volume armazenado" not in t:   # sem o cabeçalho do CSV: página de erro, não dados
        return None
    out = {}
    for dd, mm, aaaa, v in re.findall(r"^(\d\d)/(\d\d)/(\d{4}) \d\d:\d\d,([-\d.]+),", t, re.M):
        dam3 = float(v)
        if dam3 > 0:   # zero ou negativo: falha de leitura
            out[f"{aaaa}-{mm}-{dd}"] = round(dam3 / 1000, 2)
    return out


def referencia(col, cap, valores):
    """Volume de referência para a limpeza: a capacidade do SNIRH ou, sem ela, o percentil 99,5 da série."""
    if cap:
        return cap / 1000
    v = sorted(valores)
    return v[min(len(v) - 1, int(len(v) * 0.995))] if v else None


def mediana(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def limpar(dias, desde, desde_col=None):
    """Tira os valores impossíveis dos dias >= desde (AAAA-MM-DD). O SNIRH tem erros de leitura: volumes
    com um algarismo a mais à esquerda (Torrão com 90 098 hm3 em maio de 2017), dias isolados perto de zero
    e, no Douro, picos de 16 a 18/06/2018 e semanas de valores trocados no verão de 2018. Regras:
      1. acima de 125% da capacidade (ou do dobro, se a albufeira andou no último ano acima dela, ou do
         dobro do percentil 99,5 da série, se não há capacidade) — sai;
      2. longe da mediana dos vizinhos que restam (7 dias de cada lado, pelo menos 6 com valor; se a série
         for esparsa, 31 dias de cada lado, pelo menos 4) — sai, se a distância passar de 6 desvios
         absolutos medianos dos vizinhos, de 10 vezes a variação típica de um dia para o outro nessa
         albufeira e de 5% da referência. A variação típica separa as albufeiras a fio de água, que sobem e
         descem 10% de um dia para o outro (Pedrógão, Fratel, Belver), das grandes, que mexem décimas
         (uma descida de 590 hm3 num dia no Alqueva é um erro). Repete-se até não sair mais nada (no máximo
         4 voltas), porque nos períodos com muitos erros seguidos a primeira mediana ainda está contaminada.
    A regra 2 só se aplica com pelo menos 2 vizinhos depois do dia: no fim da série vale só o teto, e os
    dias recentes voltam a ser pedidos e revistos durante 30 dias, já com os dias seguintes."""
    retirados = {}
    for col, _cod, _site, _nome, _bacia, cap in ALBUFEIRAS:
        vivos = {date.fromisoformat(d): float(v[col]) for d, v in dias.items() if col in v}
        if not vivos:
            continue
        ref = referencia(col, cap, vivos.values())
        s = sorted(vivos.items())
        variacao = mediana([abs(b - a) for (da, a), (db, b) in zip(s, s[1:]) if (db - da).days == 1] or [0])
        d = (desde_col or {}).get(col, desde)   # as albufeiras novas limpam-se na série toda
        d0 = date.fromisoformat(d) if d > "0001" else date.min
        # Com capacidade conhecida o teto desce para 125%, a não ser que a albufeira ande acima dela (Belver)
        teto = 2 * ref
        if cap:
            ultimo_ano = sorted(x for _, x in sorted(vivos.items())[-365:])
            if ultimo_ano[int(len(ultimo_ano) * 0.9)] <= 1.10 * ref:
                teto = 1.25 * ref
        fora = [d for d, x in vivos.items() if d >= d0 and x > teto]
        for d in fora:
            del vivos[d]
        for _volta in range(4):
            serie = sorted(vivos.items())
            novos = []
            for i, (d, x) in enumerate(serie):
                if d < d0:
                    continue
                for raio, minimo in ((7, 6), (31, 4)):
                    viz = [y for e, y in serie[max(0, i - raio):i + raio + 1] if e != d and abs((e - d).days) <= raio]
                    if len(viz) >= minimo:
                        break
                else:
                    continue
                if sum(1 for e, _ in serie[i + 1:i + raio + 1] if (e - d).days <= raio) < 2:
                    continue   # fim da série: sem dias depois, não há como distinguir um erro de uma subida
                med = mediana(viz)
                mad = mediana([abs(y - med) for y in viz])
                if abs(x - med) > max(6 * 1.4826 * mad, 10 * variacao, 0.05 * ref):
                    novos.append(d)
            if not novos:
                break
            for d in novos:
                del vivos[d]
            fora += novos
        for d in fora:
            del dias[d.isoformat()][col]
        if fora:
            retirados[col] = len(fora)
    return retirados


def ler_diario():
    if not os.path.exists(CAMINHO_DIARIO):
        return {}
    with open(CAMINHO_DIARIO, "r", encoding="utf-8-sig", newline="") as f:
        return {r["data"]: {k: v for k, v in r.items() if k != "data" and v != ""} for r in csv.DictReader(f)}


def texto_diario(dias):
    cols = [a[0] for a in ALBUFEIRAS]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["data"] + cols)
    for d in sorted(dias):
        w.writerow([d] + [dias[d].get(c, "") for c in cols])
    return buf.getvalue()


def texto_meta(dias):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUNAS_META, lineterminator="\n")
    w.writeheader()
    for col, cod, _site, nome, bacia, cap in ALBUFEIRAS:
        serie = [(d, float(v[col])) for d, v in sorted(dias.items()) if col in v]
        mx = max(serie, key=lambda x: x[1]) if serie else (None, None)
        # Percentagem: face à capacidade do boletim da APA; sem ela, ou se no último ano andou mais de 10% acima
        # dela, face ao máximo registado. Um máximo isolado acima da capacidade (cheias, erros que escaparam à
        # limpeza) não conta.
        ultimo_ano = sorted(x for d, x in serie[-365:])
        p90 = ultimo_ano[int(len(ultimo_ano) * 0.9)] if ultimo_ano else None
        usa_cap = bool(cap) and (p90 is None or p90 <= 1.10 * cap / 1000)
        ref = cap / 1000 if usa_cap else mx[1]
        # Diária ou semanal: o intervalo mediano entre os últimos 9 valores. As albufeiras de rega passaram a ter
        # um valor por semana, à segunda-feira (algumas tinham um por dia até 2026), por isso conta o mais recente
        datas = [date.fromisoformat(d) for d, _ in serie[-9:]]
        saltos = sorted((b - a).days for a, b in zip(datas, datas[1:]))
        freq = ("semanal" if saltos[len(saltos) // 2] >= 5 else "diária") if saltos else ""
        w.writerow({"coluna": col, "codigo_snirh": cod, "nome": nome, "bacia": bacia,
                    "capacidade_hm3": f"{cap / 1000:.2f}" if cap else "",
                    "maximo_registado_hm3": f"{mx[1]:.2f}" if serie else "", "data_maximo": mx[0] or "",
                    "referencia_hm3": f"{ref:.2f}" if ref else "",
                    "referencia": ("capacidade" if usa_cap else "maximo_registado") if ref else "",
                    "inicio_serie": serie[0][0] if serie else "", "ultimo_dia": serie[-1][0] if serie else "",
                    "frequencia": freq,
                    "latitude": COORDENADAS.get(col, ("", ""))[0], "longitude": COORDENADAS.get(col, ("", ""))[1]})
    return buf.getvalue()


def gravar(caminho, novo):
    antigo = None
    if os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
            antigo = f.read()
    if novo == antigo:
        print(f"= {os.path.relpath(caminho, ROOT_DIR)} sem alterações")
        return
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        f.write(novo)
    print(f"✓ {os.path.relpath(caminho, ROOT_DIR)} ({len(novo.splitlines()) - 1} linhas)")


def main():
    backfill = "--backfill" in sys.argv
    hoje = datetime.now(timezone.utc).date()
    inicio = INICIO_BACKFILL if backfill else hoje - timedelta(days=DIAS_INCREMENTAL)
    print(f"Albufeiras (SNIRH): {inicio} → {hoje}{' (backfill)' if backfill else ''}")

    dias = {} if backfill else ler_diario()
    # Albufeiras sem nenhum valor no ficheiro (acabadas de acrescentar à lista): a série toda, desde 2001
    sem_serie = set() if backfill else {a[0] for a in ALBUFEIRAS if not any(a[0] in v for v in dias.values())}
    falhas = recolhidos = 0
    for col, cod, site, nome, _bacia, _cap in ALBUFEIRAS:
        v = volumes(site, INICIO_BACKFILL if col in sem_serie else inicio, hoje)
        if v is None:
            falhas += 1
            print(f"  ✗ {nome} ({cod}): sem resposta")
            if falhas == 3 and not recolhidos:
                # Fora de Portugal o SNIRH recusa ou deixa pendurado: não vale a pena tentar as outras
                print("ERRO: o SNIRH não respondeu aos 3 primeiros pedidos (só aceita ligações de Portugal).")
                sys.exit(1)
        else:
            print(f"  {nome} ({cod}): {len(v)} dia(s), último {max(v) if v else '—'}"
                  + (" (albufeira nova: desde 2001)" if col in sem_serie else ""))
            # Os valores novos substituem os guardados (revisões); os que não vieram ficam como estavam
            for d, x in v.items():
                dias.setdefault(d, {})[col] = f"{x:.2f}"
            recolhidos += len(v)
        time.sleep(PAUSA)

    if not recolhidos:
        # Nenhum valor: o SNIRH em baixo, a recusar os pedidos ou com outro formato — não mexer nos ficheiros
        print(f"ERRO: nenhum valor recolhido ({falhas} pedido(s) sem resposta).")
        sys.exit(1)
    retirados = limpar(dias, inicio.isoformat(), {c: INICIO_BACKFILL.isoformat() for c in sem_serie})
    if retirados:
        print(f"  Valores impossíveis retirados: {sum(retirados.values())} "
              f"({', '.join(f'{c} {n}' for c, n in sorted(retirados.items(), key=lambda x: -x[1]))})")
    dias = {d: v for d, v in dias.items() if v}
    gravar(CAMINHO_DIARIO, texto_diario(dias))
    gravar(CAMINHO_META, texto_meta(dias))
    if falhas:
        print(f"⚠ {falhas} albufeira(s) sem resposta; ficam com os valores que já tinham.")


if __name__ == "__main__":
    main()
