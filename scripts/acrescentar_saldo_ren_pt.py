#!/usr/bin/env python3
"""
Acrescenta aos ficheiros semanais da ENTSO-E de Portugal (data/producao/producao-entsoe/pt/AAAA-WSS.json)
o saldo importador da REN — Importação − Exportação, em MW médios, + = importação líquida — no campo
«cross_border_ren», alinhado com «unix_seconds»:

    "cross_border_ren": {"source": "REN …", "data": [2662.17, …, null]}

Porquê: desde julho de 2026, os fluxos físicos Portugal–Espanha da ENTSO-E (12.1.G, A11) ficam abaixo do
saldo da REN — em outubro de 2026, 1 682 GWh contra 1 778 GWh (−5,4 %), até 7,5 GWh num só dia; até junho
batiam certo. Tudo indica que não contam a linha de 400 kV Ponte de Lima–Fontefría, em serviço desde
julho de 2026. A série da ENTSO-E fica tal e qual em «production_types»; o campo novo dá a da REN, que é
o operador da rede portuguesa e a mesma fonte das páginas de Portugal do site.

Corre no workflow da produção ENTSO-E, depois do atualizar_producao_entsoe.py (que reescreve sem este
campo os ficheiros das semanas que atualiza). Não usa a rede: lê os CSV da REN do próprio repositório
(producao_dados_atuais.csv e historico/producao_historico_AAAA.csv) e só reescreve os ficheiros que mudam.

Um intervalo só leva o valor da REN se a REN tiver todos os quartos de hora dele; senão fica null, e quem
lê usa o da ENTSO-E (a REN publica com algum atraso, e às vezes para).

Uso: python scripts/acrescentar_saldo_ren_pt.py [--dados pasta-data]
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTE = "REN (Data Hub): Importação − Exportação, MW médios no intervalo; + = importação líquida"
QUARTO = 900  # segundos


def ler_ren(caminho, saldo):
    """Junta a «saldo» {início do quarto de hora em segundos UNIX (UTC): MW} os valores de um CSV da REN."""
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.strptime(r["data_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                saldo[int(t.timestamp())] = float(r["Importação"]) - float(r["Exportação"])
            except (KeyError, TypeError, ValueError):
                continue   # linha sem data_utc ou sem os dois valores (ainda por publicar)


def anos_das_semanas(ficheiros):
    anos = set()
    for p in ficheiros:
        with open(p, encoding="utf-8") as f:
            ts = json.load(f).get("unix_seconds") or []
        for t in (ts[:1] + ts[-1:]):
            anos.add(datetime.fromtimestamp(t, timezone.utc).year)
    return anos


def main():
    ap = argparse.ArgumentParser(description="Saldo importador da REN nos ficheiros semanais da ENTSO-E de Portugal.")
    ap.add_argument("--dados", default=os.path.join(RAIZ, "data"), help="pasta data/ do repositório")
    a = ap.parse_args()

    pasta_pt = os.path.join(a.dados, "producao", "producao-entsoe", "pt")
    semanas = sorted(glob.glob(os.path.join(pasta_pt, "*.json")))
    if not semanas:
        print(f"::warning::Sem ficheiros semanais de Portugal em {pasta_pt}")
        return

    # O histórico do ano primeiro e o do ano em curso por cima (na passagem de ano podem sobrepor-se)
    saldo = {}
    for ano in sorted(anos_das_semanas(semanas)):
        hist = os.path.join(a.dados, "producao", "historico", f"producao_historico_{ano}.csv")
        if os.path.exists(hist):
            ler_ren(hist, saldo)
    atuais = os.path.join(a.dados, "producao", "producao_dados_atuais.csv")
    if os.path.exists(atuais):
        ler_ren(atuais, saldo)
    if not saldo:
        # Sem a REN não se mexe em nada: a página usa a ENTSO-E (não deve chumbar a recolha da ENTSO-E)
        print("::warning::Sem dados da REN — o saldo da REN não foi acrescentado")
        return

    mudados = com_ren = sem_ren = 0
    for p in semanas:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        passo = int(d.get("interval_minutes") or 60) * 60
        n_quartos = max(1, passo // QUARTO)
        valores = []
        for t in d.get("unix_seconds") or []:
            q = [saldo.get(t + k * QUARTO) for k in range(n_quartos)]
            if all(v is not None for v in q):
                valores.append(round(sum(q) / n_quartos, 2))
                com_ren += 1
            else:
                valores.append(None)
                sem_ren += 1
        novo = {"source": FONTE, "data": valores} if any(v is not None for v in valores) else None
        if d.get("cross_border_ren") == novo:
            continue
        if novo is None:
            d.pop("cross_border_ren", None)
        else:
            d["cross_border_ren"] = novo
        # Mesmo formato do atualizar_producao_entsoe.py (compacto)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        mudados += 1

    print(f"Saldo da REN em Portugal: {len(semanas)} semanas, {mudados} ficheiro(s) atualizado(s); "
          f"{com_ren} intervalos com o valor da REN, {sem_ren} sem (ficam com o da ENTSO-E)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
