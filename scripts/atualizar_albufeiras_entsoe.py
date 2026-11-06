#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_albufeiras_entsoe.py
Energia guardada nas albufeiras de Portugal e de Espanha, semana a semana.

Fonte: ENTSO-E Transparency Platform, art. 16.1.D do Regulamento (UE) n.º 543/2013
("Aggregated filling rate of water reservoirs and hydro storage plants"): documento
A72, processo A16, zona de cada país. Um valor por semana, em MWh — a energia que a
água das albufeiras e das centrais de bombagem pode produzir. Obrigatório: variável
de ambiente ENTSOE_TOKEN.

USO
---
  python atualizar_albufeiras_entsoe.py                       # Portugal, últimas 12 semanas (revisões incluídas)
  python atualizar_albufeiras_entsoe.py --pais es             # Espanha
  python atualizar_albufeiras_entsoe.py --pais es --backfill  # tudo desde 2015, um pedido por ano

Se o ficheiro do país ainda não existe, faz sempre o backfill.

SAÍDA
-----
  data/producao/albufeiras_semanal.csv      Portugal
  data/producao/albufeiras_semanal_es.csv   Espanha (zona peninsular da ENTSO-E)

    semana_inicio   segunda-feira da semana (data local do país, AAAA-MM-DD: Lisboa ou Madrid)
    ano_iso         ano ISO da semana
    semana_iso      número ISO da semana (1–53)
    energia_gwh     energia guardada (GWh; a ENTSO-E publica MWh)
    data_utc        início da semana em UTC (AAAA-MM-DD HH:MM), como o data_utc dos outros CSV:
                    segunda-feira 00:00 local — em Portugal, domingo 23:00 no horário de verão;
                    em Espanha, domingo 23:00 no de inverno e 22:00 no de verão

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

PAISES = {
    "pt": dict(nome="Albufeiras", ficheiro="albufeiras_semanal.csv", tz=ZoneInfo("Europe/Lisbon")),
    "es": dict(nome="Albufeiras de Espanha", ficheiro="albufeiras_semanal_es.csv", tz=ZoneInfo("Europe/Madrid")),
}
COLUNAS = ["semana_inicio", "ano_iso", "semana_iso", "energia_gwh", "data_utc"]
INICIO_BACKFILL = date(2015, 1, 1)   # a ENTSO-E tem a série desde o arranque da plataforma
SEMANAS_INCREMENTAL = 12


def utc_da_segunda(segunda, tz):
    """Segunda-feira 00:00 local em UTC. Não se usa o carimbo da ENTSO-E: o cliente soma 7 dias por
    posição ao início do período, sem a mudança de hora, e a hora de cada semana dependia da estação em
    que o pedido começava (o backfill deixou as semanas de verão com uma hora a mais)."""
    return datetime.combine(segunda, time(0), tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def semanas_da_resposta(pontos, tz):
    """[(unix_ts, MWh)] → {semana_inicio: linha}. O início do período vem em UTC e, para a semana de
    Portugal, pode cair no domingo à noite (a ENTSO-E conta as semanas em hora da Europa Central):
    a segunda-feira tira-se da data local 12 horas depois do início, o que acerta os dois casos."""
    linhas = {}
    for ts, mwh in pontos:
        inicio_utc = datetime.fromtimestamp(ts, timezone.utc)
        d = (inicio_utc.astimezone(tz) + timedelta(hours=12)).date()
        segunda = d - timedelta(days=d.weekday())
        iso = segunda.isocalendar()
        linhas[segunda.isoformat()] = {
            "semana_inicio": segunda.isoformat(),
            "ano_iso": iso[0],
            "semana_iso": iso[1],
            "energia_gwh": round(mwh / 1000, 2),
            "data_utc": utc_da_segunda(segunda, tz),
        }
    return linhas


def recolher(pais, inicio, fim, token):
    """Um pedido por ano civil (a API limita a janela de cada pedido)."""
    linhas = {}
    ano = inicio.year
    while date(ano, 1, 1) < fim:
        a = max(inicio, date(ano, 1, 1))
        b = min(fim, date(ano + 1, 1, 1))
        ini_dt = datetime.combine(a, datetime.min.time(), tzinfo=timezone.utc)
        fim_dt = datetime.combine(b, datetime.min.time(), tzinfo=timezone.utc)
        pontos = entsoe_client.fetch_reservoir_filling(entsoe_client.EIC_MAP[pais], ini_dt, fim_dt, token)
        print(f"  {a} → {b}: {len(pontos)} semana(s)")
        linhas.update(semanas_da_resposta(pontos, PAISES[pais]["tz"]))
        ano += 1
    return linhas


def ler_existente(caminho):
    if not os.path.exists(caminho):
        return {}
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
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

    pais = sys.argv[sys.argv.index("--pais") + 1] if "--pais" in sys.argv else "pt"
    if pais not in PAISES:
        print(f"ERRO: país desconhecido: {pais} (há {', '.join(PAISES)}).")
        sys.exit(1)
    cfg = PAISES[pais]
    caminho = os.path.join(ROOT_DIR, "data", "producao", cfg["ficheiro"])

    # Sem ficheiro (a primeira corrida de um país novo), a série inteira
    backfill = "--backfill" in sys.argv or not os.path.exists(caminho)
    hoje = datetime.now(timezone.utc).date()
    inicio = INICIO_BACKFILL if backfill else hoje - timedelta(weeks=SEMANAS_INCREMENTAL)
    fim = hoje + timedelta(days=7)
    print(f"{cfg['nome']} (ENTSO-E A72): {inicio} → {fim}{' (backfill)' if backfill else ''}")

    novas = recolher(pais, inicio, fim, token)
    if not novas:
        # No modo incremental pode acontecer (semana ainda por publicar); num backfill é um problema
        print("Nenhuma semana devolvida pela ENTSO-E.")
        sys.exit(1 if backfill else 0)

    existentes = ler_existente(caminho)
    antigo = texto_csv(existentes) if existentes else None
    existentes.update({k: {c: str(v) for c, v in r.items()} for k, r in novas.items()})
    # A data_utc refaz-se em todas as semanas, não só nas que vieram agora: corrige as gravadas antes
    # de 03/11/2026, que vinham do carimbo da ENTSO-E
    for r in existentes.values():
        r["data_utc"] = utc_da_segunda(date.fromisoformat(r["semana_inicio"]), cfg["tz"])
    # Uma lacuna que fique (a ENTSO-E sem a semana) não impede a escrita, mas fica no log
    semanas = [date.fromisoformat(k) for k in sorted(existentes)]
    falta = [(a + timedelta(weeks=1)).isoformat() for a, b in zip(semanas, semanas[1:]) if b - a != timedelta(weeks=1)]
    if falta:
        print(f"  Aviso: semanas em falta pelo meio (a 1.ª de cada lacuna): {', '.join(falta[:10])}")
    novo = texto_csv(existentes)
    if novo == antigo:
        print(f"= {os.path.relpath(caminho, ROOT_DIR)} sem alterações ({len(existentes)} semanas)")
        return
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        f.write(novo)
    ultima = max(existentes)
    print(f"✓ {os.path.relpath(caminho, ROOT_DIR)}: {len(existentes)} semanas, a última de {ultima} "
          f"({existentes[ultima]['energia_gwh']} GWh)")


if __name__ == "__main__":
    main()
