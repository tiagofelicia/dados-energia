"""
Reservas de gás natural (armazenamento subterrâneo) de Portugal, de Espanha e da União Europeia, dia a
dia, segundo a GIE AGSI (Gas Infrastructure Europe — Aggregated Gas Storage Inventory).

  python scripts/atualizar_reservas_gas_agsi.py          # os últimos 45 dias
  python scripts/atualizar_reservas_gas_agsi.py --tudo   # tudo desde 2011

  -> data/gas/reservas_gas.csv
     data, zona (PT | ES | EU), armazenado_twh, capacidade_twh, cheio_pct, injecao_gwh, extracao_gwh, estado

Requer o secret AGSI_KEY: a chave pessoal e gratuita do registo em https://agsi.gie.eu/account. Sem a
chave o script avisa e sai sem erro (o resto do workflow continua). A chave vai no cabeçalho x-key e
nunca é escrita no log.

Notas sobre a fonte:
  • Um dia de gás por linha; a AGSI publica o dia anterior ao fim da tarde (CET) e revê os dias
    recentes (estado E = estimado, C = confirmado): cada corrida volta a pedir os últimos 45 dias.
  • A UE é o agregado da própria AGSI (type=eu), não uma soma feita aqui.
  • Unidades: armazenado e capacidade em TWh, injeção e extração em GWh/dia, cheio em %.
  • A atribuição é obrigatória: «Fonte: GIE AGSI» junto dos dados.
O script só reescreve o ficheiro quando alguma coisa muda.
"""
import argparse
import csv
import io
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
ZONAS = {"PT": {"country": "PT"}, "ES": {"country": "ES"}, "EU": {"type": "eu"}}
INICIO_TUDO = date(2011, 1, 1)
DIAS_NORMAL = 45
TAMANHO = 300                       # linhas por página
COLUNAS = ["data", "zona", "armazenado_twh", "capacidade_twh", "cheio_pct",
           "injecao_gwh", "extracao_gwh", "estado"]
CAMPOS = {"armazenado_twh": "gasInStorage", "capacidade_twh": "workingGasVolume", "cheio_pct": "full",
          "injecao_gwh": "injection", "extracao_gwh": "withdrawal"}
TENTATIVAS = 3


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
        params = dict(ZONAS[zona], **{"from": desde.isoformat(), "to": ate.isoformat(),
                                      "size": TAMANHO, "page": pagina})
        j = pedir(chave, params)
        ultima = int(j.get("last_page") or 1)
        for d in j.get("data") or []:
            dia = (d.get("gasDayStart") or "")[:10]
            if len(dia) != 10:
                continue
            linha = {col: num(d.get(campo)) for col, campo in CAMPOS.items()}
            if linha["armazenado_twh"] is None and linha["cheio_pct"] is None:
                continue
            linha["estado"] = (d.get("status") or "").strip()
            out[dia] = linha
        pagina += 1
        time.sleep(1)
    return out


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


def escrever(linhas):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUNAS)
    ordem = {z: i for i, z in enumerate(ZONAS)}
    for k in sorted(linhas, key=lambda k: (k[0], ordem.get(k[1], 9))):
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
    ate = date.today()
    desde = INICIO_TUDO if (args.tudo or not atual) else ate - timedelta(days=DIAS_NORMAL)
    print(f"🛢️ Reservas de gás (GIE AGSI): {desde.isoformat()} a {ate.isoformat()}"
          f"{' (sem ficheiro: tudo)' if not atual else ''}")

    novas, mudadas, falhas = dict(atual), 0, []
    for zona in ZONAS:
        try:
            dias = recolher(chave, zona, desde, ate)
        except (ErroAGSI, requests.RequestException, ValueError) as e:
            # A mensagem da AGSI nunca traz a chave; o URL do requests também não (vai no cabeçalho)
            falhas.append(zona)
            print(f"::warning::{zona}: {type(e).__name__}: {e}")
            continue
        for dia, r in dias.items():
            linha = linha_csv(dia, zona, r)
            if novas.get((dia, zona)) != linha:
                mudadas += 1
            novas[(dia, zona)] = linha
        ultimo = max(dias) if dias else "—"
        print(f"  {zona}: {len(dias)} dias, o último {ultimo}")

    if len(falhas) == len(ZONAS):
        print("::error::A AGSI não respondeu para nenhuma zona — nada foi escrito.")
        return 1
    if escrever(novas):
        print(f"✓ {os.path.relpath(FICHEIRO, ROOT_DIR)}: {len(novas)} linhas ({mudadas} novas ou revistas)")
    else:
        print("= sem alterações")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
