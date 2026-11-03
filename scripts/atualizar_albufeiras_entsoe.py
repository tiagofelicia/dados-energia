#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_albufeiras_entsoe.py
Energia guardada nas albufeiras portuguesas, semana a semana.

Fonte: ENTSO-E Transparency Platform, art. 16.1.D do Regulamento (UE) n.º 543/2013
("Aggregated filling rate of water reservoirs and hydro storage plants"): documento
A72, processo A16, zona de Portugal. Um valor por semana, em MWh — a energia que a
água das albufeiras e das centrais de bombagem pode produzir. Obrigatório: variável
de ambiente ENTSOE_TOKEN.

USO
---
  python atualizar_albufeiras_entsoe.py              # últimas 12 semanas (revisões incluídas)
  python atualizar_albufeiras_entsoe.py --backfill   # tudo desde 2015, um pedido por ano

SAÍDA
-----
  data/producao/albufeiras_semanal.csv

    semana_inicio   segunda-feira da semana (data de Lisboa, AAAA-MM-DD)
    ano_iso         ano ISO da semana
    semana_iso      número ISO da semana (1–53)
    energia_gwh     energia guardada (GWh; a ENTSO-E publica MWh)
    data_utc        início da semana em UTC (AAAA-MM-DD HH:MM), como o data_utc dos outros CSV:
                    segunda-feira 00:00 de Lisboa, ou seja, domingo 23:00 no horário de verão

As semanas novas juntam-se às que já estão no ficheiro; um valor revisto pela
ENTSO-E substitui o anterior. Só se reescreve o ficheiro se alguma coisa mudou.
"""

import csv
import io
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
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

CAMINHO = os.path.join(ROOT_DIR, "data", "producao", "albufeiras_semanal.csv")
COLUNAS = ["semana_inicio", "ano_iso", "semana_iso", "energia_gwh", "data_utc"]
INICIO_BACKFILL = date(2015, 1, 1)   # a ENTSO-E tem a série desde o arranque da plataforma
SEMANAS_INCREMENTAL = 12
TZ = ZoneInfo("Europe/Lisbon")


def utc_da_segunda(segunda):
    """Segunda-feira 00:00 de Lisboa em UTC. Não se usa o carimbo da ENTSO-E: o cliente soma 7 dias por
    posição ao início do período, sem a mudança de hora, e a hora de cada semana dependia da estação em
    que o pedido começava (o backfill deixou as semanas de verão com uma hora a mais)."""
    return datetime.combine(segunda, time(0), tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def semanas_da_resposta(pontos):
    """[(unix_ts, MWh)] → {semana_inicio: linha}. O início do período vem em UTC e, para a semana de
    Portugal, pode cair no domingo à noite (a ENTSO-E conta as semanas em hora da Europa Central):
    a segunda-feira tira-se da data de Lisboa 12 horas depois do início, o que acerta os dois casos."""
    linhas = {}
    for ts, mwh in pontos:
        inicio_utc = datetime.fromtimestamp(ts, timezone.utc)
        d = (inicio_utc.astimezone(TZ) + timedelta(hours=12)).date()
        segunda = d - timedelta(days=d.weekday())
        iso = segunda.isocalendar()
        linhas[segunda.isoformat()] = {
            "semana_inicio": segunda.isoformat(),
            "ano_iso": iso[0],
            "semana_iso": iso[1],
            "energia_gwh": round(mwh / 1000, 2),
            "data_utc": utc_da_segunda(segunda),
        }
    return linhas


def recolher(inicio, fim, token):
    """Um pedido por ano civil (a API limita a janela de cada pedido)."""
    linhas = {}
    ano = inicio.year
    while date(ano, 1, 1) < fim:
        a = max(inicio, date(ano, 1, 1))
        b = min(fim, date(ano + 1, 1, 1))
        ini_dt = datetime.combine(a, datetime.min.time(), tzinfo=timezone.utc)
        fim_dt = datetime.combine(b, datetime.min.time(), tzinfo=timezone.utc)
        pontos = entsoe_client.fetch_reservoir_filling(entsoe_client.EIC_MAP["pt"], ini_dt, fim_dt, token)
        print(f"  {a} → {b}: {len(pontos)} semana(s)")
        linhas.update(semanas_da_resposta(pontos))
        ano += 1
    return linhas


def ler_existente():
    if not os.path.exists(CAMINHO):
        return {}
    with open(CAMINHO, "r", encoding="utf-8-sig", newline="") as f:
        return {r["semana_inicio"]: r for r in csv.DictReader(f)}


def texto_csv(linhas):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUNAS, lineterminator="\n")
    w.writeheader()
    for k in sorted(linhas):
        w.writerow({c: linhas[k][c] for c in COLUNAS})
    return buf.getvalue()


def main():
    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        print("ERRO: variável de ambiente ENTSOE_TOKEN não definida.")
        sys.exit(1)

    backfill = "--backfill" in sys.argv
    hoje = datetime.now(timezone.utc).date()
    inicio = INICIO_BACKFILL if backfill else hoje - timedelta(weeks=SEMANAS_INCREMENTAL)
    fim = hoje + timedelta(days=7)
    print(f"Albufeiras (ENTSO-E A72): {inicio} → {fim}{' (backfill)' if backfill else ''}")

    novas = recolher(inicio, fim, token)
    if not novas:
        # No modo incremental pode acontecer (semana ainda por publicar); num backfill é um problema
        print("Nenhuma semana devolvida pela ENTSO-E.")
        sys.exit(1 if backfill else 0)

    existentes = ler_existente()
    antigo = texto_csv(existentes) if existentes else None
    existentes.update({k: {c: str(v) for c, v in r.items()} for k, r in novas.items()})
    # A data_utc refaz-se em todas as semanas, não só nas que vieram agora: corrige as gravadas antes
    # de 03/11/2026, que vinham do carimbo da ENTSO-E
    for r in existentes.values():
        r["data_utc"] = utc_da_segunda(date.fromisoformat(r["semana_inicio"]))
    novo = texto_csv(existentes)
    if novo == antigo:
        print(f"= {os.path.relpath(CAMINHO, ROOT_DIR)} sem alterações ({len(existentes)} semanas)")
        return
    os.makedirs(os.path.dirname(CAMINHO), exist_ok=True)
    with open(CAMINHO, "w", encoding="utf-8-sig", newline="") as f:
        f.write(novo)
    ultima = max(existentes)
    print(f"✓ {os.path.relpath(CAMINHO, ROOT_DIR)}: {len(existentes)} semanas, a última de {ultima} "
          f"({existentes[ultima]['energia_gwh']} GWh)")


if __name__ == "__main__":
    main()
