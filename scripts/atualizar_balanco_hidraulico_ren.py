#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_balanco_hidraulico_ren.py
O «Balanço Hidráulico» diário da REN (Data Hub → Eletricidade → Balanço Diário, modelo 1535):
a energia afluente às albufeiras, aos fios de água e às mini-hídricas, o que cada um produziu,
a bombagem, a energia guardada nas albufeiras (GWh e %), a variação do dia e os descarregamentos.
Há dados desde 01/01/2010.

Output: data/producao/balanco_hidraulico_ren.csv — um dia por linha, as colunas de COLUNAS.

Uso:
  python atualizar_balanco_hidraulico_ren.py                    # últimos 14 dias + os que faltarem
  python atualizar_balanco_hidraulico_ren.py --dias 365         # revisão (no workflow, ao domingo)
  python atualizar_balanco_hidraulico_ren.py --desde 2010-01-01 # voltar a pedir tudo desde uma data
Sem ficheiro, traz a série inteira desde 01/01/2010 (~6 200 pedidos, um por dia).

O que a REN dá (o mesmo CSV do botão «CSV» da página), em GWh:
                               Albufeiras  Fios de Água  Mini Hídricas  Total
  Energia Afluente Total           x            x             x           x
  Energia Afluente Útil            x            x             x           x
  Produção                         x            x             x           x
  Bombagem                         x                                      x
  Armazenamento                    x                                      x
  [%]                              x                                      x
  Variação Diária                  x                                      x
  Descarregamento Total            x            x                         x
  Descarregamento Turbinável       x                                      x
Nos fios de água e nas mini-hídricas a REN considera a energia afluente igual à produção do dia
(pouca ou nenhuma capacidade de armazenamento). A produção inclui a da água antes bombeada (é a «Hídrica»
mais a «Produção por Bombagem» do modelo 1363). Nas albufeiras, as contas fecham assim:
  variação = afluente − produção − descarregamento + bombagem
           = afluente útil − produção − descarregamento turbinável + bombagem
  afluente útil = afluente − (descarregamento − descarregamento turbinável)
A água «turbinável» que se descarregou conta como útil (podia ter passado nas turbinas), mas saiu pelos
descarregadores. Em 16 anos só 7 dias falham por mais de 3 GWh.

ARMADILHAS DA FONTE
  • Um pedido = um dia. Com startDateString ≠ endDateString a REN devolve só o primeiro dia.
  • Para um dia ainda não publicado, ou anterior a 2010, a REN NÃO dá erro: devolve os valores do
    último dia publicado, sem a linha «Variação Diária». Por isso um dia só conta se trouxer essa
    linha — exceto 01/01/2010, o primeiro da série, que não tem dia anterior.
  • A energia afluente das albufeiras sai do balanço e pode ser negativa (−1 GWh a 06/11/2026).
  • GWh inteiros; o % com uma casa. A capacidade de referência do % foi subindo com as albufeiras
    novas (~3 080 GWh em 2015, ~3 250 GWh em 2026): guardam-se os GWh e o % como vêm.
  • A REN revê os valores semanas depois de os publicar (como na bombagem do modelo 1363, ver
    atualizar_producao_bombagem.py): cada corrida volta a pedir os últimos 14 dias e a de domingo
    os últimos 365. Os dias que mudam aparecem no log e contam no resumo.
  • A REN já alternou entre dois endpoints (ver atualizar_producao_bombagem.py): tentam-se os dois,
    e o CSV é lido com ',' ou ';'. Em 13/11/2026 só o /service/exports/csv respondia.

Antes de gravar valida tudo (limites, nenhum dia perdido, o balanço do dia a fechar) e só reescreve
o ficheiro se alguma coisa mudou: quase todas as corridas terminam sem commit.
"""

import argparse
import csv
import io
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

MODELO = '1535'
ENDPOINTS_REN = (
    ('https://datahub.ren.pt/service/exports/csv', {'country': 'PT', 'modelId': MODELO}),
    (f'https://datahub.ren.pt/service/download/csv/{MODELO}', {}),
)
HEADERS = {'Cache-Control': 'no-cache, no-store, must-revalidate', 'Pragma': 'no-cache', 'Expires': '0'}
SESSAO = requests.Session()      # reutiliza a ligação: a série inteira são ~6 200 pedidos
LISBOA = ZoneInfo('Europe/Lisbon')
PRIMEIRO_DIA = date(2010, 1, 1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, '..', 'data', 'producao'))
OUT_PATH = os.path.join(DATA_DIR, 'balanco_hidraulico_ren.csv')

# As colunas do CSV da REN (pelo nome do cabeçalho) → sufixo das colunas do ficheiro
COL_REN = {'Albufeiras': 'albufeiras', 'Fios de Água': 'fios_agua', 'Mini Hídricas': 'mini_hidricas', 'Total': 'total'}
# As linhas → (prefixo, colunas que se guardam). Nas linhas que só existem nas albufeiras guarda-se
# um valor só (o das albufeiras, que é igual ao total).
LINHAS = {
    'Energia Afluente Total': ('afluente', ('albufeiras', 'fios_agua', 'mini_hidricas', 'total')),
    'Energia Afluente Útil': ('afluente_util', ('albufeiras', 'fios_agua', 'mini_hidricas', 'total')),
    'Produção': ('producao', ('albufeiras', 'fios_agua', 'mini_hidricas', 'total')),
    'Bombagem': ('bombagem', None),
    'Armazenamento': ('armazenamento', None),
    '[%]': ('armazenamento_pct', None),
    'Variação Diária': ('variacao', None),
    'Descarregamento Total': ('descarregamento', ('albufeiras', 'fios_agua', 'total')),
    'Descarregamento Turbinável': ('descarregamento_turbinavel', None),
}


def nome_coluna(prefixo, sufixo=None):
    if prefixo == 'armazenamento_pct':
        return prefixo
    return f'{prefixo}_{sufixo}_gwh' if sufixo else f'{prefixo}_gwh'


COLUNAS = ['data']
for _rot, (_pref, _sufs) in LINHAS.items():
    COLUNAS += [nome_coluna(_pref, s) for s in _sufs] if _sufs else [nome_coluna(_pref)]
COLUNAS.append('data_utc')
VALORES = [c for c in COLUNAS if c not in ('data', 'data_utc')]

# Limites de bom senso (GWh por dia; % de 0 a 100). Fora disto não se grava nada.
LIMITES = {'armazenamento_pct': (0, 100), 'armazenamento_gwh': (0, 5000), 'variacao_gwh': (-500, 500)}
LIMITE_GERAL = (-100, 1500)   # afluências (podem ser negativas), produção, bombagem, descarregamentos


def data_utc(d):
    """Meia-noite de Lisboa desse dia, em UTC («AAAA-MM-DD HH:MM»), como nos outros ficheiros."""
    return datetime(d.year, d.month, d.day, tzinfo=LISBOA).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')


def numero(txt, sep=','):
    t = (txt or '').strip().replace(' ', '')
    if t in ('', '-'):
        return None
    return float(t.replace(',', '.')) if sep == ';' else float(t)   # com ';' a REN usa a vírgula decimal


def ler_csv_ren(texto, dia):
    """O CSV da REN → dict coluna → valor (float ou None). None se não for um dia válido."""
    sep = ';' if texto.count(';') > texto.count(',') else ','
    linhas = [l for l in csv.reader(io.StringIO(texto), delimiter=sep) if any(c.strip() for c in l)]
    if not linhas:
        return None
    cab = [c.strip() for c in linhas[0]]
    idx = {}
    for i, c in enumerate(cab):
        base = c.split('[')[0].strip()
        if base in COL_REN:
            idx[COL_REN[base]] = i
    if set(idx) != set(COL_REN.values()):
        raise ValueError(f'cabeçalho inesperado: {cab}')
    vistos, reg = set(), {}
    for l in linhas[1:]:
        rot = l[0].strip()
        if rot not in LINHAS:
            continue
        vistos.add(rot)
        pref, sufs = LINHAS[rot]
        val = lambda s: numero(l[idx[s]], sep) if idx[s] < len(l) else None
        if sufs:
            for s in sufs:
                reg[nome_coluna(pref, s)] = val(s)
        else:
            v = val('albufeiras')
            reg[nome_coluna(pref)] = v if v is not None else val('total')
    falta = set(LINHAS) - vistos
    # Sem «Variação Diária» é a resposta de recurso da REN (dia por publicar ou fora da série)
    if 'Variação Diária' in falta and dia != PRIMEIRO_DIA:
        return None
    falta.discard('Variação Diária')
    if falta:
        raise ValueError(f'faltam linhas: {sorted(falta)}')
    return reg


def buscar_dia(dia, max_tentativas=3, timeout=60):
    """O balanço hidráulico de um dia (dict) ou None (dia ainda não publicado / fora da série)."""
    params = {'startDateString': dia.isoformat(), 'endDateString': dia.isoformat(), 'culture': 'pt-PT'}
    erro = None
    for tentativa in range(1, max_tentativas + 1):
        for url, extra in ENDPOINTS_REN:
            try:
                r = SESSAO.get(url, params={**params, **extra}, headers=HEADERS, timeout=timeout)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                texto = r.content.decode('utf-8-sig')
                if texto.lstrip().startswith('<'):
                    continue          # página de erro em HTML: tenta o outro endpoint
                return ler_csv_ren(texto, dia)
            except ValueError:
                raise
            except Exception as e:     # rede, timeout, 5xx
                erro = e
        if tentativa < max_tentativas:
            time.sleep(2 ** tentativa)
    raise RuntimeError(f'{dia}: nenhum endpoint da REN respondeu ({erro})')


def carregar():
    dados = {}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                if row.get('data'):
                    dados[row['data']] = {c: (float(row[c]) if row.get(c) not in (None, '') else None) for c in VALORES}
    return dados


def fmt(c, v):
    if v is None:
        return ''
    if c == 'armazenamento_pct':
        return f'{v:.1f}'
    return str(int(v)) if float(v).is_integer() else f'{v:g}'


def texto_csv(dados):
    out = io.StringIO()
    w = csv.writer(out, lineterminator='\n')
    w.writerow(COLUNAS)
    for k in sorted(dados):
        d = date.fromisoformat(k)
        w.writerow([k] + [fmt(c, dados[k].get(c)) for c in VALORES] + [data_utc(d)])
    return out.getvalue()


def validar(novos, antigos):
    """Lista de problemas (vazia = pode gravar)."""
    probs = []
    perdidos = set(antigos) - set(novos)
    if perdidos:
        probs.append(f'{len(perdidos)} dias desapareciam (ex.: {sorted(perdidos)[:3]})')
    for k, reg in novos.items():
        for c in VALORES:
            v = reg.get(c)
            if v is None:
                continue
            lo, hi = LIMITES.get(c, LIMITE_GERAL)
            if not lo <= v <= hi:
                probs.append(f'{k} {c} = {v} fora de [{lo}, {hi}]')
        for c in ('armazenamento_gwh', 'armazenamento_pct', 'afluente_total_gwh', 'producao_total_gwh'):
            if reg.get(c) is None:
                probs.append(f'{k}: {c} vazio')
    return probs


def avisos_balanco(novos):
    """Os dias em que as contas da REN não fecham nas albufeiras (afluente − produção − descarregamento + bombagem
    ≠ variação, com ±3 GWh de folga para os arredondamentos; em 2010–2026, 7 dias). É da fonte: fica só no log,
    não impede a gravação."""
    maus = []
    for k in sorted(novos):
        reg = novos[k]
        a, p, d, b, v = (reg.get(c) for c in ('afluente_albufeiras_gwh', 'producao_albufeiras_gwh', 'descarregamento_albufeiras_gwh',
                                              'bombagem_gwh', 'variacao_gwh'))
        if None not in (a, p, v):
            desvio = a - p - (d or 0) + (b or 0) - v
            if abs(desvio) > 3:
                maus.append((k, desvio))
    if maus:
        print(f'  [AVISO] em {len(maus)} dias o balanço da REN não fecha (afluente − produção − descarregamento + bombagem − variação): '
              + ', '.join(f'{k} {d:+g}' for k, d in maus[-8:]))


def main():
    ap = argparse.ArgumentParser(description='Balanço Hidráulico diário da REN (modelo 1535).')
    ap.add_argument('--dias', type=int, default=14, help='dias recentes a pedir sempre de novo (14)')
    ap.add_argument('--desde', type=date.fromisoformat, help='voltar a pedir tudo desde esta data (AAAA-MM-DD)')
    args = ap.parse_args()

    antigos = carregar()
    ontem = datetime.now(LISBOA).date() - timedelta(days=1)
    if args.desde or not antigos:
        inicio = max(args.desde or PRIMEIRO_DIA, PRIMEIRO_DIA)
        pedir = [inicio + timedelta(days=i) for i in range((ontem - inicio).days + 1)]
        print(f'Série desde {inicio} até {ontem}: {len(pedir)} dias')
    else:
        recentes = {ontem - timedelta(days=i) for i in range(args.dias)}
        primeiro = min(date.fromisoformat(k) for k in antigos)
        em_falta = {primeiro + timedelta(days=i) for i in range((ontem - primeiro).days + 1)} - {date.fromisoformat(k) for k in antigos}
        pedir = sorted(d for d in recentes | em_falta if d >= PRIMEIRO_DIA)
        print(f'A pedir {len(pedir)} dias ({len(recentes)} recentes, {len(em_falta)} em falta)')

    novos = {k: dict(v) for k, v in antigos.items()}
    revistos, acrescentados, por_publicar, falhas = [], 0, [], []
    t0 = time.time()
    for n, d in enumerate(pedir, 1):
        try:
            reg = buscar_dia(d)
        except Exception as e:
            falhas.append(f'{d}: {e}')
            print(f'  [ERR] {d}: {e}', file=sys.stderr)
            continue
        k = d.isoformat()
        if reg is None:
            por_publicar.append(k)
            continue
        if k in antigos:
            mud = [c for c in VALORES if antigos[k].get(c) != reg.get(c)]
            if mud:
                revistos.append(k)
                print(f'  ~ {k}: ' + ', '.join(f'{c} {fmt(c, reg.get(c))} (era {fmt(c, antigos[k].get(c))})' for c in mud[:4]))
        else:
            acrescentados += 1
        novos[k] = reg
        if n % 500 == 0:
            print(f'  … {n}/{len(pedir)} ({time.time() - t0:.0f} s)')

    # Dias por publicar só são normais no fim (a REN publica o dia anterior durante a manhã)
    antigos_por_publicar = [k for k in por_publicar if date.fromisoformat(k) < ontem - timedelta(days=3)]
    if antigos_por_publicar:
        print(f'  [AVISO] {len(antigos_por_publicar)} dias sem dados na REN (ex.: {antigos_por_publicar[:5]})')

    print(f'Pedidos: {len(pedir)} em {time.time() - t0:.0f} s · novos {acrescentados} · revistos {len(revistos)} · '
          f'por publicar {len(por_publicar)} · falhas {len(falhas)}')

    if not novos:
        print('[ERRO] nenhum dia com dados', file=sys.stderr)
        return 1
    pedidos = {d.isoformat() for d in pedir}
    avisos_balanco({k: v for k, v in novos.items() if k in pedidos})   # só os dias pedidos nesta corrida
    probs = validar(novos, antigos)
    if probs:
        print('[ERRO] validação falhou, o ficheiro fica como estava:', file=sys.stderr)
        for p in probs[:20]:
            print('   - ' + p, file=sys.stderr)
        return 1

    texto = texto_csv(novos)
    atual = open(OUT_PATH, encoding='utf-8').read() if os.path.exists(OUT_PATH) else None
    if texto == atual:
        print(f'Sem alterações ({len(novos)} dias, último {max(novos)}).')
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(OUT_PATH, 'w', encoding='utf-8', newline='') as f:
            f.write(texto)
        print(f'Gravado {OUT_PATH}: {len(novos)} dias, {min(novos)} → {max(novos)}.')
    # Falhas de rede num ou noutro dia não deitam abaixo a corrida (voltam a ser pedidos na
    # seguinte, como dias em falta); muitas falhas sim.
    return 1 if len(falhas) > max(10, len(pedir) * 0.05) else 0


if __name__ == '__main__':
    sys.exit(main())
