"""
Reservas de gás natural (armazenamento subterrâneo) de Portugal, de Espanha e da União Europeia, dia a
dia, segundo a GIE AGSI (Gas Infrastructure Europe — Aggregated Gas Storage Inventory).

  python scripts/atualizar_reservas_gas_agsi.py          # os últimos 45 dias
  python scripts/atualizar_reservas_gas_agsi.py --tudo   # tudo desde 2011

  -> data/gas/reservas_gas.csv
     data, zona (PT | ES | EU), armazenado_twh, capacidade_twh, cheio_pct, injecao_gwh, extracao_gwh, estado
  -> data/mapas/reservas-gas/AAAA.json   (todos os países europeus da AGSI e a UE, um ficheiro por ano)
     {"ano", "primeiro_dia", "campos": [cheio_pct, armazenado_twh, capacidade_twh, estado],
      "paises": {"AT": [[95.5, 3.41, 3.57, "C"], null, …]}}   (posição i = primeiro_dia + i dias)
  -> data/mapas/reservas-gas/metadata.json
     a ultima_data (o dia do mapa: o último da UE na AGSI) e, por país, o último dia até esse, o mesmo
     dia de há um ano e o mínimo, a média e o máximo desse dia nos cinco anos anteriores (o que o mapa
     precisa para abrir sem ler os ficheiros por ano)

Requer o secret AGSI_KEY: a chave pessoal e gratuita do registo em https://agsi.gie.eu/account. Sem a
chave o script avisa e sai sem erro (o resto do workflow continua). A chave vai no cabeçalho x-key e
nunca é escrita no log.

Notas sobre a fonte:
  • Um dia de gás por linha; a AGSI publica o dia anterior ao fim da tarde (CET) e revê os dias
    recentes (estado E = estimado, C = confirmado): cada corrida volta a pedir os últimos 45 dias.
  • A UE é o agregado da própria AGSI (type=eu), não uma soma feita aqui.
  • Unidades: armazenado e capacidade em TWh, injeção e extração em GWh/dia, cheio em %.
  • A atribuição é obrigatória: «Fonte: GIE AGSI» junto dos dados.
  • Portugal (o Carriço) só tem valores desde 12/11/2011; os dias antes disso vêm a zeros e ficam de fora.
  • Portugal, nos dias que a AGSI ainda não confirmou: o valor é o da REN (Data Hub → Gás → Balanço diário →
    Balanço do armazenamento subterrâneo, modelo 2638), com estado R. Em 2026 a AGSI ficou semanas com
    Portugal «estimado» e o valor congelado no último dia confirmado (3,4097 TWh de 07/10 a 21/11), enquanto
    a REN tinha os valores reais; nos dias confirmados as duas coincidem a ±2 GWh. Quando a AGSI confirma o
    dia (nos últimos 45 dias), o valor da AGSI volta a mandar.
  • Os países: a UE a 27 e os vizinhos que a AGSI cobre (PAISES_EUROPA). Os que não têm armazenamento,
    ou não estão na AGSI, vêm vazios e ficam na lista sem_dados do metadata.json (a cinzento no mapa).
    Uma falha num desses países só dá um aviso; uma falha em PT, ES ou na UE faz a corrida acabar com erro.
  • Sem os ficheiros da Europa (a 1.ª corrida), os países europeus pedem-se desde 2011, mesmo sem --tudo
    (~600 pedidos, com 1 s entre eles: uns 10 minutos). PT, ES e a UE vêm do reservas_gas.csv.
  • Um país sem nenhum dia nos ficheiros da Europa volta a pedir-se desde 2011 em cada corrida: assim um
    país que falhou na 1.ª (na de 23/11/2026, a Croácia, a Dinamarca e a Eslováquia) entra sozinho na
    seguinte. Os que não têm armazenamento custam um pedido curto cada.
  • O dia do mapa (ultima_data do metadata.json) é o último da UE na AGSI. Os dias R de Portugal podem ir
    um dia à frente (de manhã a REN já publicou ontem e a AGSI ainda não): ficam nos ficheiros, mas não
    passam a ser o dia do mapa, onde só Portugal teria valor.
O script só reescreve os ficheiros que mudam.
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import date, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FICHEIRO = os.path.join(ROOT_DIR, "data", "gas", "reservas_gas.csv")
URL = os.environ.get("AGSI_URL", "https://agsi.gie.eu/api")   # AGSI_URL só para testes
ZONAS = {"PT": {"country": "PT"}, "ES": {"country": "ES"}, "EU": {"type": "eu"}}   # as do reservas_gas.csv
PASTA_EUROPA = os.path.join(ROOT_DIR, "data", "mapas", "reservas-gas")
# A UE a 27 e os vizinhos que a AGSI cobre. Os que não têm armazenamento vêm vazios (ver o cabeçalho).
PAISES_EUROPA = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
                 "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB", "UA", "RS"]
FONTE_EUROPA = ("GIE AGSI (Gas Infrastructure Europe); Portugal: REN nos dias que a AGSI ainda não "
                "confirmou (estado R)")
ANOS_MEDIA = 5                      # anos anteriores no mínimo / média / máximo do metadata.json
INICIO_TUDO = date(2011, 1, 1)
DIAS_NORMAL = 45
TAMANHO = 300                       # linhas por página
COLUNAS = ["data", "zona", "armazenado_twh", "capacidade_twh", "cheio_pct",
           "injecao_gwh", "extracao_gwh", "estado"]
CAMPOS = {"armazenado_twh": "gasInStorage", "capacidade_twh": "workingGasVolume", "cheio_pct": "full",
          "injecao_gwh": "injection", "extracao_gwh": "withdrawal"}
TENTATIVAS = 3
REN_URL = "https://datahub.ren.pt/service/exports/csv"
REN_MODELO = 2638                   # Gás → Balanço diário → Balanço do armazenamento subterrâneo
REN_MAX_DIAS = 120                  # no máximo tantos dias da REN por corrida (um pedido por dia)


class ErroAGSI(Exception):
    pass


def pedir(chave, params):
    hdr = {"x-key": chave, "User-Agent": "dados-energia (+https://dados.tiagofelicia.pt)"}
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            r = requests.get(URL, params=params, headers=hdr, timeout=90)
            if r.status_code == 429 and tentativa < TENTATIVAS:
                print(f"  429 (limite de pedidos): a aguardar {30 * tentativa} s")
                time.sleep(30 * tentativa)
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("error") or j.get("message") and not j.get("data"):
                raise ErroAGSI(f"{j.get('error') or ''} {j.get('message') or ''}".strip())
            return j
        except requests.RequestException as e:
            if tentativa == TENTATIVAS:
                raise
            print(f"  {type(e).__name__}: nova tentativa daqui a {5 * tentativa} s")
            time.sleep(5 * tentativa)


def num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if s in ("", "-", "n/a", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def recolher(chave, zona, desde, ate):
    """{data_iso: {coluna: valor, 'estado': str}} de uma zona, página a página."""
    out, pagina, ultima = {}, 1, 1
    while pagina <= ultima:
        params = dict(ZONAS.get(zona) or {"country": zona},
                      **{"from": desde.isoformat(), "to": ate.isoformat(), "size": TAMANHO, "page": pagina})
        j = pedir(chave, params)
        ultima = int(j.get("last_page") or 1)
        for d in j.get("data") or []:
            dia = (d.get("gasDayStart") or "")[:10]
            if len(dia) != 10:
                continue
            linha = {col: num(d.get(campo)) for col, campo in CAMPOS.items()}
            if sem_dados(linha):
                continue
            linha["estado"] = (d.get("status") or "").strip()
            out[dia] = linha
        pagina += 1
        time.sleep(1)
    return out


def sem_dados(linha):
    """Dia sem dados: sem valores, ou capacidade a zero (Portugal vem a zeros até 11/11/2011)."""
    if linha.get("armazenado_twh") is None and linha.get("cheio_pct") is None:
        return True
    return not linha.get("capacidade_twh")


def ren_pt(dia):
    """(existências, injeção, extração) do Carriço num dia, em GWh, segundo a REN; None se não houver."""
    params = {"startDateString": dia, "endDateString": dia, "culture": "pt-PT", "country": "PT",
              "modelId": REN_MODELO}
    r = requests.get(REN_URL, params=params, timeout=60,
                     headers={"User-Agent": "dados-energia (+https://dados.tiagofelicia.pt)"})
    if r.status_code in (400, 404):                       # o dia ainda não foi publicado (hoje, ontem)
        return None
    r.raise_for_status()
    val = {}
    for c in csv.reader(io.StringIO(r.content.decode("utf-8-sig"))):
        if len(c) > 1 and c[0].strip():
            val.setdefault(c[0].strip(), c[1])
    ex = num(val.get("EXISTÊNCIAS"))
    if not ex:
        return None
    return ex, num(val.get("Injeção")) or 0.0, num(val.get("Extração")) or 0.0


def completar_pt_com_ren(linhas, ate):
    """Põe os valores da REN nos dias de Portugal que a AGSI não confirmou (depois do último confirmado)."""
    confirmados = sorted(k[0] for k, v in linhas.items() if k[1] == "PT" and v[7] == "C")
    if not confirmados:
        return
    ult_c = confirmados[-1]
    cap = num(linhas[(ult_c, "PT")][3])                   # TWh: a capacidade declarada à AGSI
    dia = max(date.fromisoformat(ult_c) + timedelta(days=1), ate - timedelta(days=REN_MAX_DIAS))
    n = 0
    while dia <= ate and cap:
        k = (dia.isoformat(), "PT")
        if k not in linhas or linhas[k][7] != "C":
            try:
                v = ren_pt(dia.isoformat())
            except requests.RequestException as e:
                print(f"::warning::REN (Portugal, {dia.isoformat()}): {type(e).__name__}: {e}")
                break
            if v:
                ex, inj, ext = v
                twh = ex / 1000
                linhas[k] = [dia.isoformat(), "PT", fmt(twh, 4), fmt(cap, 4), fmt(100 * twh / cap, 2),
                             fmt(inj, 2), fmt(ext, 2), "R"]
                n += 1
            time.sleep(0.5)
        dia += timedelta(days=1)
    if n:
        print(f"  PT: {n} dia(s) com o valor da REN (a AGSI só tem confirmados até {ult_c})")


def fmt(v, casas):
    if v is None:
        return ""
    return f"{v:.{casas}f}".rstrip("0").rstrip(".") if casas else str(int(round(v)))


CASAS = {"armazenado_twh": 4, "capacidade_twh": 4, "cheio_pct": 2, "injecao_gwh": 2, "extracao_gwh": 2}


def linha_csv(dia, zona, r):
    return [dia, zona] + [fmt(r.get(c), CASAS[c]) for c in COLUNAS[2:-1]] + [r.get("estado", "")]


def ler_ficheiro():
    if not os.path.exists(FICHEIRO):
        return {}
    with open(FICHEIRO, encoding="utf-8-sig", newline="") as f:
        return {(r["data"], r["zona"]): [r[c] for c in COLUNAS] for r in csv.DictReader(f)}


def ler_europa():
    """As linhas já guardadas nos ficheiros da Europa, no formato das do CSV (sem injeção nem extração)."""
    out = {}
    if not os.path.isdir(PASTA_EUROPA):
        return out
    for nome in sorted(os.listdir(PASTA_EUROPA)):
        if not (len(nome) == 9 and nome[:4].isdigit() and nome.endswith(".json")):
            continue
        with open(os.path.join(PASTA_EUROPA, nome), encoding="utf-8") as f:
            j = json.load(f)
        d0 = date.fromisoformat(j["primeiro_dia"])
        for zona, valores in j["paises"].items():
            for i, v in enumerate(valores):
                if v:
                    dia = (d0 + timedelta(days=i)).isoformat()
                    out[(dia, zona)] = [dia, zona, fmt(v[1], 4), fmt(v[2], 4), fmt(v[0], 2), "", "", v[3]]
    return out


def gravar(caminho, texto):
    """Escreve só se mudou; devolve True se escreveu."""
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8", newline="") as f:
            if f.read() == texto:
                return False
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        f.write(texto)
    return True


def _valores(v):
    """[cheio_pct, armazenado_twh, capacidade_twh, estado] de uma linha, ou None se não tiver dados."""
    pct, twh, cap = num(v[4]), num(v[2]), num(v[3])
    if sem_dados({"armazenado_twh": twh, "cheio_pct": pct, "capacidade_twh": cap}):
        return None
    return [round(pct, 2), round(twh, 4), round(cap, 4), v[7]]


def escrever_europa(linhas):
    """data/mapas/reservas-gas/AAAA.json (só os anos que mudam) e metadata.json; devolve quantos mudaram."""
    zonas = ["EU"] + PAISES_EUROPA
    serie = {}                                              # zona -> {dia: valores}
    for (dia, zona), v in linhas.items():
        if zona in zonas:
            val = _valores(v)
            if val:
                serie.setdefault(zona, {})[dia] = val
    mudados = 0
    anos = sorted({int(d[:4]) for s in serie.values() for d in s})
    for ano in anos:
        d0 = date(ano, 1, 1)
        partes = []
        for zona in zonas:
            dias = {d: v for d, v in serie.get(zona, {}).items() if d.startswith(str(ano))}
            if not dias:
                continue
            arr = [None] * ((date.fromisoformat(max(dias)) - d0).days + 1)
            for d, v in dias.items():
                arr[(date.fromisoformat(d) - d0).days] = v
            partes.append(f'"{zona}":' + json.dumps(arr, separators=(",", ":"), ensure_ascii=False))
        # Uma linha por país: um dia novo só muda as linhas dos países que o têm (diffs pequenos no git)
        texto = ('{"ano":%d,"primeiro_dia":"%s","campos":["cheio_pct","armazenado_twh","capacidade_twh","estado"],'
                 '"fonte":%s,"paises":{\n' % (ano, d0.isoformat(), json.dumps(FONTE_EUROPA, ensure_ascii=False))
                 + ",\n".join(partes) + "\n}}\n")
        if gravar(os.path.join(PASTA_EUROPA, f"{ano}.json"), texto):
            mudados += 1

    # metadata.json: o que o mapa precisa para abrir. O dia do mapa é o último da UE na AGSI (sem a UE,
    # o último que não seja da REN): os dias R de Portugal podem ir um dia à frente e não o arrastam.
    if serie.get("EU"):
        dia_mapa = max(serie["EU"])
    else:
        agsi = [d for s in serie.values() for d, v in s.items() if v[3] != "R"]
        dia_mapa = max(agsi) if agsi else None
    paises = {}
    for zona in zonas:
        s = serie.get(zona)
        if not s:
            continue
        ate_mapa = [d for d in s if dia_mapa is None or d <= dia_mapa]
        ult = max(ate_mapa) if ate_mapa else max(s)
        pct, twh, cap, est = s[ult]
        a, m, d = (int(x) for x in ult.split("-"))
        def no_dia(ano):
            # o mesmo dia noutro ano (29/02 -> 28/02), ou até 3 dias antes se faltar
            base = date(ano, m, 28 if (m == 2 and d == 29) else d)
            for k in range(4):
                x = (base - timedelta(days=k)).isoformat()
                if x in s:
                    return x, s[x][0]
            return None, None
        dia_ant, pct_ant = no_dia(a - 1)
        anteriores = [p for p in (no_dia(a - k)[1] for k in range(1, ANOS_MEDIA + 1)) if p is not None]
        paises[zona] = {
            "data": ult, "cheio_pct": pct, "armazenado_twh": twh, "capacidade_twh": cap, "estado": est,
            "ha_um_ano_data": dia_ant, "ha_um_ano_pct": pct_ant,
            "media_5a_pct": round(sum(anteriores) / len(anteriores), 2) if len(anteriores) >= 3 else None,
            "min_5a_pct": min(anteriores) if len(anteriores) >= 3 else None,
            "max_5a_pct": max(anteriores) if len(anteriores) >= 3 else None,
            "primeiro_dia": min(s),
        }
    meta = {"ultima_data": dia_mapa,
            "fonte": FONTE_EUROPA, "anos": anos,
            "sem_dados": [z for z in PAISES_EUROPA if z not in serie], "paises": paises}
    if gravar(os.path.join(PASTA_EUROPA, "metadata.json"),
              json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True) + "\n"):
        mudados += 1
    return mudados


def escrever(linhas):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUNAS)
    ordem = {z: i for i, z in enumerate(ZONAS)}
    for k in sorted((k for k in linhas if k[1] in ZONAS), key=lambda k: (k[0], ordem.get(k[1], 9))):
        # As linhas antigas também passam pelo filtro: o ficheiro de 23/11/2026 tinha os zeros de 2011
        if sem_dados({"armazenado_twh": num(linhas[k][2]), "cheio_pct": num(linhas[k][4]),
                      "capacidade_twh": num(linhas[k][3])}):
            continue
        w.writerow(linhas[k])
    novo = "﻿" + buf.getvalue()
    if os.path.exists(FICHEIRO):
        with open(FICHEIRO, encoding="utf-8", newline="") as f:
            if f.read() == novo:
                return False
    os.makedirs(os.path.dirname(FICHEIRO), exist_ok=True)
    with open(FICHEIRO, "w", encoding="utf-8", newline="") as f:
        f.write(novo)
    return True


def main():
    ap = argparse.ArgumentParser(description="Reservas de gás natural PT, ES e UE (GIE AGSI).")
    ap.add_argument("--tudo", action="store_true", help=f"pedir tudo desde {INICIO_TUDO.isoformat()}")
    args = ap.parse_args()

    chave = (os.environ.get("AGSI_KEY") or "").strip()
    if not chave:
        print("::notice::Sem o secret AGSI_KEY: as reservas de gás (GIE AGSI) não foram atualizadas. "
              "A chave é gratuita, com registo em https://agsi.gie.eu/account.")
        return 0

    atual = ler_ficheiro()
    europa = ler_europa()
    ate = date.today()
    recente = ate - timedelta(days=DIAS_NORMAL)
    tudo_csv = args.tudo or not atual            # PT, ES e a UE
    tudo_eu = args.tudo or not europa            # os outros países (a 1.ª corrida com o mapa da Europa)
    print(f"🛢️ Reservas de gás (GIE AGSI) até {ate.isoformat()}: PT/ES/UE "
          f"{'desde ' + INICIO_TUDO.isoformat() if tudo_csv else 'os últimos ' + str(DIAS_NORMAL) + ' dias'}; "
          f"outros países {'desde ' + INICIO_TUDO.isoformat() if tudo_eu else 'os últimos ' + str(DIAS_NORMAL) + ' dias'}")

    # O CSV manda nas suas zonas (tem a injeção e a extração); os ficheiros da Europa nas outras
    novas = dict(europa)
    novas.update(atual)
    # Um país sem nenhum dia guardado pede-se desde 2011: se falhou numa corrida, entra na seguinte
    com_dados = {z for (_, z) in europa}
    mudadas, falhas, vazios = 0, [], []
    for zona in ["EU"] + [z for z in PAISES_EUROPA]:
        nucleo = zona in ZONAS
        desde = INICIO_TUDO if (tudo_csv if nucleo else (tudo_eu or zona not in com_dados)) else recente
        try:
            dias = recolher(chave, zona, desde, ate)
        except (ErroAGSI, requests.RequestException, ValueError) as e:
            # A mensagem da AGSI nunca traz a chave; o URL do requests também não (vai no cabeçalho)
            if nucleo:
                falhas.append(zona)
                print(f"::warning::{zona}: {type(e).__name__}: {e}")
            else:
                vazios.append(zona)
                print(f"  {zona}: sem dados ({type(e).__name__}: {e})")
            continue
        if not dias:
            vazios.append(zona)
            continue
        for dia, r in dias.items():
            linha = linha_csv(dia, zona, r)
            if novas.get((dia, zona)) != linha:
                mudadas += 1
            novas[(dia, zona)] = linha
        print(f"  {zona}: {len(dias)} dias, o último {max(dias)}")
    if vazios:
        print(f"  sem dados na AGSI (sem armazenamento?): {', '.join(vazios)}")

    if len(falhas) == len(ZONAS):
        print("::error::A AGSI não respondeu para nenhuma das zonas principais — nada foi escrito.")
        return 1
    completar_pt_com_ren(novas, ate)
    if escrever(novas):
        print(f"✓ {os.path.relpath(FICHEIRO, ROOT_DIR)} ({mudadas} linhas novas ou revistas no total)")
    else:
        print(f"= {os.path.relpath(FICHEIRO, ROOT_DIR)} sem alterações")
    n = escrever_europa(novas)
    print(f"{'✓' if n else '='} {os.path.relpath(PASTA_EUROPA, ROOT_DIR)}: {n} ficheiro(s) atualizado(s)")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
