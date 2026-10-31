#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_albufeiras_snirh.py
Água guardada em cada albufeira hidroelétrica, dia a dia.

Fonte: SNIRH — Sistema Nacional de Informação de Recursos Hídricos, da Agência Portuguesa do
Ambiente (snirh.apambiente.pt), «Dados de base» da rede hidrométrica: o parâmetro «Volume
armazenado na última hora» (dam3), um valor por dia, das 23:00. Não precisa de chave; o SNIRH
pede que se refira a origem dos dados.

As albufeiras são as das centrais hidroelétricas com dados diários no SNIRH, 40 ao todo, com o
Alqueva e o Pedrógão e as que não entram no boletim mensal do SNIRH (Baixo Sabor, Foz Tua, as
três do Tâmega, a cadeia do Douro, Ribeiradio, Alto Ceira, Belver e Fratel).

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

SAÍDA
-----
  data/producao/albufeiras_snirh_diario.csv
    data        AAAA-MM-DD
    <coluna>    uma por albufeira: volume armazenado nesse dia (hm3, 2 casas); vazio sem valor

  data/producao/albufeiras_snirh.csv — as albufeiras
    coluna, codigo_snirh, nome, bacia
    capacidade_hm3            capacidade total, da página de características do SNIRH (vazio: sem registo)
    maximo_registado_hm3      o maior volume da série, e data_maximo
    inicio_serie, ultimo_dia  primeiro e último dia com valor

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

# coluna, código SNIRH, site (id interno do SNIRH), nome, bacia, capacidade total (dam3, página de
# características do SNIRH; None = sem registo). Por bacia, de norte para sul, e por capacidade.
ALBUFEIRAS = [
    ("alto_lindoso", "02H/01A", 1627743428, "Alto Lindoso", "Lima", 390000),
    ("touvedo", "03G/01A", 1627759476, "Touvedo", "Lima", 15500),
    ("alto_rabagao", "03J/03A", 1627743430, "Alto Rabagão", "Cávado", 568690),
    ("paradela", "03J/01A", 1627758928, "Paradela", "Cávado", 164390),
    ("canicada", "04H/01A", 1627743674, "Caniçada", "Cávado", 159300),
    ("vilarinho_das_furnas", "03H/01A", 1627759560, "Vilarinho das Furnas", "Cávado", 117690),
    ("venda_nova", "03J/04A", 1627759522, "Venda Nova", "Cávado", 94500),
    ("salamonde", "03I/01A", 1627759378, "Salamonde", "Cávado", 65000),
    ("guilhofrei", "04I/01A", 1627758686, "Guilhofrei", "Ave", 21149),
    ("carrapatelo", "07I/01A", 1627743682, "Carrapatelo", "Douro", 148400),
    ("bemposta", "06S/01A", 1627743548, "Bemposta", "Douro", 128800),
    ("torrao", "07H/01A", 1627759468, "Torrão", "Douro", 123990),
    ("crestuma_lever", "07G/01A", 1627751912, "Crestuma-Lever", "Douro", 110000),
    ("vilar_tabuaco", "08L/01A", 1627759554, "Vilar-Tabuaço", "Douro", 99750),
    ("valeira", "07M/02A", 1627759510, "Valeira", "Douro", 98540),
    ("regua", "07K/01A", 1627759340, "Régua", "Douro", 95000),
    ("pocinho", "07O/02A", 1627758976, "Pocinho", "Douro", 83070),
    ("picote", "05S/01A", 1627758968, "Picote", "Douro", 62700),
    ("miranda", "05T/01A", 1627758786, "Miranda", "Douro", 28100),
    ("varosa", "07K/02A", 1627759512, "Varosa", "Douro", 12943),
    ("alto_tamega", "04K/04A", 11508860178, "Alto Tâmega", "Douro", None),
    ("baixo_sabor", "06O/09A", 7554777518, "Baixo Sabor", "Douro", None),
    ("daivoes", "04J/08A", 9978398426, "Daivões", "Douro", None),
    ("foz_tua", "06M/06A", 9321904688, "Foz Tua", "Douro", None),
    ("gouvaes", "05K/02A", 10908513554, "Gouvães", "Douro", None),
    ("ribeiradio", "09H/06A", 7554777512, "Ribeiradio", "Vouga", None),
    ("aguieira", "11H/01A", 1627743384, "Aguieira", "Mondego", 423030),
    ("fronhas", "12I/01A", 1627758668, "Fronhas", "Mondego", 62100),
    ("lagoa_comprida", "11L/03A", 1627758736, "Lagoa Comprida", "Mondego", 14000),
    ("caldeirao", "10M/01A", 1627743662, "Caldeirão", "Mondego", 5520),
    ("vale_do_rossim", "11L/02A", 1627759502, "Vale do Rossim", "Mondego", 3500),
    ("alto_ceira", "12J/01A", 1627743426, "Alto Ceira", "Mondego", None),
    ("castelo_de_bode", "16H/01A", 1627743896, "Castelo de Bode", "Tejo", 1095000),
    ("cabril", "14I/01A", 1627743586, "Cabril", "Tejo", 720000),
    ("pracana", "16K/01A", 1627759292, "Pracana", "Tejo", 111900),
    ("fratel", "16K/02A", 1627758658, "Fratel", "Tejo", 92500),
    ("povoa", "17L/01A", 1627759290, "Póvoa", "Tejo", 19300),
    ("belver", "17J/01A", 1627743544, "Belver", "Tejo", 12500),
    ("alqueva", "24M/07A", 1627743416, "Alqueva", "Guadiana", 4150000),
    ("pedrogao", "25L/01A", 1627758944, "Pedrógão", "Guadiana", None),
]
COLUNAS_META = ["coluna", "codigo_snirh", "nome", "bacia", "capacidade_hm3", "maximo_registado_hm3",
                "data_maximo", "referencia_hm3", "referencia", "inicio_serie", "ultimo_dia"]


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


def limpar(dias, desde):
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
    Os dias recentes voltam a ser pedidos durante 30 dias, por isso um valor verdadeiro cortado por só
    ter vizinhos de um lado volta a entrar quando chegarem os dias seguintes."""
    retirados = {}
    for col, _cod, _site, _nome, _bacia, cap in ALBUFEIRAS:
        vivos = {date.fromisoformat(d): float(v[col]) for d, v in dias.items() if col in v}
        if not vivos:
            continue
        ref = referencia(col, cap, vivos.values())
        s = sorted(vivos.items())
        variacao = mediana([abs(b - a) for (da, a), (db, b) in zip(s, s[1:]) if (db - da).days == 1] or [0])
        d0 = date.fromisoformat(desde) if desde > "0001" else date.min
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
        # Percentagem: face à capacidade do SNIRH; sem ela, ou se no último ano andou mais de 10% acima dela
        # (Belver tem 12,5 hm3 de capacidade e anda nos 19 hm3 desde novembro de 2023), face ao máximo registado.
        # Um máximo isolado acima da capacidade (cheias, erros que escaparam à limpeza) não conta.
        ultimo_ano = sorted(x for d, x in serie[-365:])
        p90 = ultimo_ano[int(len(ultimo_ano) * 0.9)] if ultimo_ano else None
        usa_cap = bool(cap) and (p90 is None or p90 <= 1.10 * cap / 1000)
        ref = cap / 1000 if usa_cap else mx[1]
        w.writerow({"coluna": col, "codigo_snirh": cod, "nome": nome, "bacia": bacia,
                    "capacidade_hm3": f"{cap / 1000:.2f}" if cap else "",
                    "maximo_registado_hm3": f"{mx[1]:.2f}" if serie else "", "data_maximo": mx[0] or "",
                    "referencia_hm3": f"{ref:.2f}" if ref else "",
                    "referencia": ("capacidade" if usa_cap else "maximo_registado") if ref else "",
                    "inicio_serie": serie[0][0] if serie else "", "ultimo_dia": serie[-1][0] if serie else ""})
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
    falhas = recolhidos = 0
    for col, cod, site, nome, _bacia, _cap in ALBUFEIRAS:
        v = volumes(site, inicio, hoje)
        if v is None:
            falhas += 1
            print(f"  ✗ {nome} ({cod}): sem resposta")
            if falhas == 3 and not recolhidos:
                # Fora de Portugal o SNIRH recusa ou deixa pendurado: não vale a pena tentar as outras 37
                print("ERRO: o SNIRH não respondeu aos 3 primeiros pedidos (só aceita ligações de Portugal).")
                sys.exit(1)
        else:
            print(f"  {nome} ({cod}): {len(v)} dia(s), último {max(v) if v else '—'}")
            # Os valores novos substituem os guardados (revisões); os que não vieram ficam como estavam
            for d, x in v.items():
                dias.setdefault(d, {})[col] = f"{x:.2f}"
            recolhidos += len(v)
        time.sleep(PAUSA)

    if not recolhidos:
        # Nenhum valor: o SNIRH em baixo, a recusar os pedidos ou com outro formato — não mexer nos ficheiros
        print(f"ERRO: nenhum valor recolhido ({falhas} pedido(s) sem resposta).")
        sys.exit(1)
    retirados = limpar(dias, inicio.isoformat())
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
