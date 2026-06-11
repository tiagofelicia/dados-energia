#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_records_omie.py — Gera/atualiza data/omie/records_omie.json com recordes históricos OMIE.

Modos:
  python gerar_records_omie.py --full   Lê TODOS os omie_historico_*.csv + omie_dados_atuais.csv
                                   (deve correr na 1ª vez ou em rebuilds periódicos)
  python gerar_records_omie.py          Modo incremental: usa records_omie.json existente +
                                   omie_dados_atuais.csv (default — para corridas diárias).
                                   Se records_omie.json não existir, faz fallback automático para --full.

Recordes calculados:
  • Dia mais caro / mais barato (média diária PT)
  • Hora mais cara / mais barata (média horária PT)
  • Mês mais caro (média mensal PT)
  • Ano mais caro / mais barato (média anual PT)
  • 1ª hora com preço PT ≤ 0
  • Mês com mais horas negativas
  • Maior spread PT>ES diário / ES>PT diário
  • Maior range intra-diário (máx − mín no mesmo dia, PT)
  • Sequência mais longa de dias com média > 100 €/MWh
  • Ano mais volátil (desvio-padrão dos quartos PT)

Output: data/omie/records_omie.json com chaves 'recordes' (para a UI) e 'aggregates' (para incremental).
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from glob import glob

import pandas as pd

# === Caminhos ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, '..', 'data', 'omie'))
RECORDS_PATH = os.path.join(DATA_DIR, 'records_omie.json')
ATUAIS_PATH = os.path.join(DATA_DIR, 'omie_dados_atuais.csv')
HISTORICO_GLOB = os.path.join(DATA_DIR, 'historico', 'omie_historico_*.csv')

# === Tradução dos dias da semana (para display) ===
DIAS_SEMANA_PT = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']

# ============================================================
# Leitura de CSVs
# ============================================================

def ler_csv_omie(path):
    """
    Lê um CSV OMIE e devolve um DataFrame só com as linhas de preços
    (ignora tudo depois de 'TABELA_' em omie_dados_atuais.csv).
    """
    with open(path, 'r', encoding='utf-8-sig') as f:
        text = f.read()
    idx = text.find('\nTABELA_')
    if idx >= 0:
        text = text[:idx]
    return pd.read_csv(io.StringIO(text))


def adicionar_colunas(df):
    """Adiciona colunas 'data', 'ano', 'mes', 'hora_inicio', 'dia_hora'."""
    df = df.copy()
    df['data'] = pd.to_datetime(df['dia'], format='%d/%m/%Y', errors='coerce')
    df = df.dropna(subset=['data'])
    df['ano'] = df['data'].dt.year
    df['mes'] = df['data'].dt.strftime('%m/%Y')
    df['hora_inicio'] = df['intervalo'].astype(str).str[1:].str.split('-').str[0]
    df['hora_h'] = df['hora_inicio'].str.split(':').str[0]
    df['dia_hora'] = df['dia'] + ' ' + df['hora_h']
    df['preco_pt'] = pd.to_numeric(df['preco_pt'], errors='coerce')
    df['preco_es'] = pd.to_numeric(df['preco_es'], errors='coerce')
    return df


# ============================================================
# Cálculo de recordes a partir de um DataFrame
# ============================================================

def computar_recordes(df):
    """
    Computa todos os recordes + agregados a partir do DataFrame combinado.
    Devolve (records: dict, aggregates: dict).
    """
    records = {}
    aggregates = {}

    df_pt = df.dropna(subset=['preco_pt'])

    # ─── Dia mais caro / mais barato ───
    daily_pt = df_pt.groupby('dia', as_index=False).agg(
        media=('preco_pt', 'mean'),
        max_q=('preco_pt', 'max'),
        min_q=('preco_pt', 'min'),
    )
    daily_pt['data'] = pd.to_datetime(daily_pt['dia'], format='%d/%m/%Y')

    if not daily_pt.empty:
        d_caro = daily_pt.loc[daily_pt['media'].idxmax()]
        records['diaMaisCaro'] = {
            'data': d_caro['dia'],
            'valor': round(float(d_caro['media']), 2),
            'diaSemana': DIAS_SEMANA_PT[d_caro['data'].weekday()],
        }
        d_barato = daily_pt.loc[daily_pt['media'].idxmin()]
        records['diaMaisBarato'] = {
            'data': d_barato['dia'],
            'valor': round(float(d_barato['media']), 2),
            'diaSemana': DIAS_SEMANA_PT[d_barato['data'].weekday()],
        }

    # ─── Hora mais cara / mais barata (média horária) ───
    hourly_pt = df_pt.groupby(['dia_hora', 'dia', 'hora_h'], as_index=False).agg(
        media=('preco_pt', 'mean'),
    )
    if not hourly_pt.empty:
        h_caro = hourly_pt.loc[hourly_pt['media'].idxmax()]
        records['horaMaisCara'] = {
            'data': h_caro['dia'],
            'hora': f"{h_caro['hora_h']}:00",
            'valor': round(float(h_caro['media']), 2),
        }
        h_barato = hourly_pt.loc[hourly_pt['media'].idxmin()]
        records['horaMaisBarata'] = {
            'data': h_barato['dia'],
            'hora': f"{h_barato['hora_h']}:00",
            'valor': round(float(h_barato['media']), 2),
        }

    # ─── Médias mensais e anuais ───
    monthly_pt = df_pt.groupby('mes', as_index=False).agg(media=('preco_pt', 'mean'))
    yearly_pt = df_pt.groupby('ano', as_index=False).agg(
        media=('preco_pt', 'mean'),
        std=('preco_pt', 'std'),
    )

    aggregates['mediasMensaisPT'] = {
        r['mes']: round(float(r['media']), 2) for _, r in monthly_pt.iterrows()
    }
    aggregates['mediasAnuaisPT'] = {
        str(int(r['ano'])): round(float(r['media']), 2) for _, r in yearly_pt.iterrows()
    }
    aggregates['stdAnualPT'] = {
        str(int(r['ano'])): round(float(r['std']), 2) for _, r in yearly_pt.iterrows()
        if pd.notna(r['std'])
    }

    # ─── Mês mais caro ───
    if not monthly_pt.empty:
        m_caro = monthly_pt.loc[monthly_pt['media'].idxmax()]
        records['mesMaisCaro'] = {
            'mes': m_caro['mes'],
            'valor': round(float(m_caro['media']), 2),
        }

    # ─── Ano mais caro / mais barato / mais volátil ───
    if not yearly_pt.empty:
        a_caro = yearly_pt.loc[yearly_pt['media'].idxmax()]
        records['anoMaisCaro'] = {
            'ano': int(a_caro['ano']),
            'valor': round(float(a_caro['media']), 2),
        }
        a_barato = yearly_pt.loc[yearly_pt['media'].idxmin()]
        records['anoMaisBarato'] = {
            'ano': int(a_barato['ano']),
            'valor': round(float(a_barato['media']), 2),
        }
        yp_std = yearly_pt.dropna(subset=['std'])
        if not yp_std.empty:
            a_vol = yp_std.loc[yp_std['std'].idxmax()]
            records['anoMaisVolatil'] = {
                'ano': int(a_vol['ano']),
                'desvioPadrao': round(float(a_vol['std']), 2),
            }

    # ─── 1ª hora com preço < 0 (estritamente negativo) ───
    df_neg = df_pt[df_pt['preco_pt'] < 0].copy()
    if not df_neg.empty:
        # construir timestamp para ordenar
        df_neg['ts'] = pd.to_datetime(
            df_neg['dia'] + ' ' + df_neg['hora_inicio'],
            format='%d/%m/%Y %H:%M',
            errors='coerce',
        )
        df_neg = df_neg.dropna(subset=['ts']).sort_values('ts')
        primeira = df_neg.iloc[0]
        records['primeiraHoraNegativa'] = {
            'data': primeira['dia'],
            'hora': primeira['hora_inicio'],
            'valor': round(float(primeira['preco_pt']), 2),
        }

    # ─── Mês com mais horas negativas ───
    hourly_with_mes = df_pt.groupby(['mes', 'dia_hora'], as_index=False).agg(
        media_hora=('preco_pt', 'mean'),
    )
    horas_neg = hourly_with_mes[hourly_with_mes['media_hora'] < 0]
    if not horas_neg.empty:
        contagem = horas_neg.groupby('mes').size().reset_index(name='count')
        aggregates['horasNegativasPorMes'] = {
            r['mes']: int(r['count']) for _, r in contagem.iterrows()
        }
        m_neg = contagem.loc[contagem['count'].idxmax()]
        records['mesMaisHorasNegativas'] = {
            'mes': m_neg['mes'],
            'horas': int(m_neg['count']),
        }
    else:
        aggregates['horasNegativasPorMes'] = {}

    # ─── Maior spread PT>ES / ES>PT (diário) ───
    df_spread = df.dropna(subset=['preco_pt', 'preco_es']).copy()
    df_spread['spread'] = df_spread['preco_pt'] - df_spread['preco_es']
    daily_spread = df_spread.groupby('dia', as_index=False).agg(spread_media=('spread', 'mean'))
    if not daily_spread.empty:
        s_max = daily_spread.loc[daily_spread['spread_media'].idxmax()]
        records['maiorSpreadPTmaiorES'] = {
            'data': s_max['dia'],
            'valor': round(float(s_max['spread_media']), 2),
        }
        s_min = daily_spread.loc[daily_spread['spread_media'].idxmin()]
        records['maiorSpreadESmaiorPT'] = {
            'data': s_min['dia'],
            'valor': round(float(s_min['spread_media']), 2),  # valor negativo
        }

    # ─── Maior range intra-diário (PT) ───
    daily_pt['range'] = daily_pt['max_q'] - daily_pt['min_q']
    if not daily_pt.empty:
        r_max = daily_pt.loc[daily_pt['range'].idxmax()]
        records['maiorRangeIntraDiario'] = {
            'data': r_max['dia'],
            'valor': round(float(r_max['range']), 2),
        }

    # ─── Sequência mais longa de dias com média > 100 €/MWh ───
    daily_sorted = daily_pt.sort_values('data').reset_index(drop=True)
    daily_sorted['above_100'] = daily_sorted['media'] > 100
    max_seq, max_inicio, max_fim = 0, None, None
    cur_seq, cur_inicio = 0, None
    last_data = None
    for _, row in daily_sorted.iterrows():
        if row['above_100']:
            # Check continuity (sem gaps de dias)
            if last_data is not None and (row['data'] - last_data).days > 1:
                cur_seq = 0
                cur_inicio = None
            if cur_seq == 0:
                cur_inicio = row['dia']
            cur_seq += 1
            if cur_seq > max_seq:
                max_seq = cur_seq
                max_inicio = cur_inicio
                max_fim = row['dia']
        else:
            cur_seq = 0
            cur_inicio = None
        last_data = row['data']

    if max_seq > 0:
        records['sequenciaMaiorDe100'] = {
            'dias': max_seq,
            'inicio': max_inicio,
            'fim': max_fim,
        }

    return records, aggregates


# ============================================================
# Modos full / incremental
# ============================================================

def full_compute():
    """Carrega TODOS os CSVs históricos + atuais e computa do zero."""
    csvs = sorted(glob(HISTORICO_GLOB))
    if os.path.exists(ATUAIS_PATH):
        csvs.append(ATUAIS_PATH)

    if not csvs:
        sys.exit('Nenhum CSV OMIE encontrado em ' + DATA_DIR)

    print(f'[full] A ler {len(csvs)} ficheiros...')
    dfs = []
    for path in csvs:
        try:
            df = ler_csv_omie(path)
            dfs.append(df)
            print(f'  [OK] {os.path.basename(path)} ({len(df)} linhas)')
        except Exception as exc:
            print(f'  [ERR] {os.path.basename(path)} - erro: {exc}', file=sys.stderr)

    df_full = pd.concat(dfs, ignore_index=True)
    df_full = adicionar_colunas(df_full)
    print(f'[full] Total: {len(df_full):,} quartos-horarios')

    return computar_recordes(df_full)


def incremental_update():
    """Carrega só omie_dados_atuais.csv e funde com records_omie.json existente."""
    if not os.path.exists(RECORDS_PATH):
        print('records_omie.json não existe — a fazer fallback para --full')
        return full_compute()

    with open(RECORDS_PATH, 'r', encoding='utf-8') as f:
        old = json.load(f)

    if not os.path.exists(ATUAIS_PATH):
        sys.exit('omie_dados_atuais.csv não encontrado')

    print(f'[incremental] A ler omie_dados_atuais.csv...')
    df = ler_csv_omie(ATUAIS_PATH)
    df = adicionar_colunas(df)
    print(f'[incremental] {len(df):,} quartos-horarios no ficheiro atual')

    new_records, new_aggs = computar_recordes(df)

    # Merge aggregates: meses/anos do CSV atual sobrepõem-se aos antigos
    old_aggs = old.get('aggregates', {}) or {}
    merged_aggs = {
        'mediasMensaisPT':       {**old_aggs.get('mediasMensaisPT', {}),       **new_aggs.get('mediasMensaisPT', {})},
        'mediasAnuaisPT':        {**old_aggs.get('mediasAnuaisPT', {}),        **new_aggs.get('mediasAnuaisPT', {})},
        'stdAnualPT':            {**old_aggs.get('stdAnualPT', {}),            **new_aggs.get('stdAnualPT', {})},
        'horasNegativasPorMes':  {**old_aggs.get('horasNegativasPorMes', {}),  **new_aggs.get('horasNegativasPorMes', {})},
    }

    # Records derivados (recomputados a partir dos aggregates mesclados)
    final_records = dict(old.get('recordes', {}) or {})

    if merged_aggs['mediasMensaisPT']:
        mes, valor = max(merged_aggs['mediasMensaisPT'].items(), key=lambda x: x[1])
        final_records['mesMaisCaro'] = {'mes': mes, 'valor': round(valor, 2)}

    if merged_aggs['mediasAnuaisPT']:
        ano_caro, vc = max(merged_aggs['mediasAnuaisPT'].items(), key=lambda x: x[1])
        final_records['anoMaisCaro'] = {'ano': int(ano_caro), 'valor': round(vc, 2)}
        ano_barato, vb = min(merged_aggs['mediasAnuaisPT'].items(), key=lambda x: x[1])
        final_records['anoMaisBarato'] = {'ano': int(ano_barato), 'valor': round(vb, 2)}

    if merged_aggs['horasNegativasPorMes']:
        mes, horas = max(merged_aggs['horasNegativasPorMes'].items(), key=lambda x: x[1])
        final_records['mesMaisHorasNegativas'] = {'mes': mes, 'horas': int(horas)}

    if merged_aggs['stdAnualPT']:
        ano, std = max(merged_aggs['stdAnualPT'].items(), key=lambda x: x[1])
        final_records['anoMaisVolatil'] = {'ano': int(ano), 'desvioPadrao': round(std, 2)}

    # Records "rolling" — comparar e manter extremo
    def replace_if_higher(field):
        if field not in new_records:
            return
        old_v = (final_records.get(field) or {}).get('valor')
        new_v = new_records[field]['valor']
        if old_v is None or new_v > old_v:
            final_records[field] = new_records[field]

    def replace_if_lower(field):
        if field not in new_records:
            return
        old_v = (final_records.get(field) or {}).get('valor')
        new_v = new_records[field]['valor']
        if old_v is None or new_v < old_v:
            final_records[field] = new_records[field]

    replace_if_higher('diaMaisCaro')
    replace_if_lower('diaMaisBarato')
    replace_if_higher('horaMaisCara')
    replace_if_lower('horaMaisBarata')
    replace_if_higher('maiorSpreadPTmaiorES')
    replace_if_lower('maiorSpreadESmaiorPT')   # ES>PT é spread mais negativo
    replace_if_higher('maiorRangeIntraDiario')

    # Sequência > 100 — manter a maior
    if 'sequenciaMaiorDe100' in new_records:
        old_seq = (final_records.get('sequenciaMaiorDe100') or {}).get('dias', 0)
        if new_records['sequenciaMaiorDe100']['dias'] > old_seq:
            final_records['sequenciaMaiorDe100'] = new_records['sequenciaMaiorDe100']

    # 1ª hora negativa — manter a MAIS ANTIGA (não trocar se já houver)
    if 'primeiraHoraNegativa' in new_records and 'primeiraHoraNegativa' not in final_records:
        final_records['primeiraHoraNegativa'] = new_records['primeiraHoraNegativa']

    return final_records, merged_aggs


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Gera/atualiza data/omie/records_omie.json')
    parser.add_argument('--full', action='store_true', help='Recalcular tudo a partir dos CSVs')
    args = parser.parse_args()

    if args.full:
        records, aggs = full_compute()
        modo = 'full'
    else:
        records, aggs = incremental_update()
        modo = 'incremental' if os.path.exists(RECORDS_PATH) else 'full (fallback)'

    out = {
        'geradoEm': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'modo': modo,
        'recordes': records,
        'aggregates': aggs,
    }

    with open(RECORDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f'\n[OK] Escrito {RECORDS_PATH}')
    print(f'  Modo: {modo}')
    print(f'  Recordes: {len(records)} ({", ".join(records.keys())})')


if __name__ == '__main__':
    main()
