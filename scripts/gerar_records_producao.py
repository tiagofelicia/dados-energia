#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_records_producao.py — Gera/atualiza data/records_producao.json com
recordes históricos do Balanço Energético em Portugal a partir dos dados
de produção da REN (15 min, serviço 1354) + Produção por Bombagem diária
(serviço 1363).

Modos:
  python gerar_records_producao.py --full
        Lê TODOS os producao_historico_*.csv + producao_dados_atuais.csv
        (deve correr na 1ª vez ou em rebuilds periódicos).

  python gerar_records_producao.py
        Modo incremental: usa records_producao.json existente + apenas
        producao_dados_atuais.csv (default — para corridas diárias).
        Fallback automático para --full se records_producao.json não existir.

Recordes calculados (todos por DIA):
  • Maior % Renováveis (Hídrica ajustada s/ bombagem turbinada quando disponível)
  • Maior produção Solar / Eólica / Hídrica
  • Maior consumo (energia, GWh)
  • Maior pico de consumo (MW + hora do intervalo)
  • Maior saldo importador / exportador (GWh líquidos com Espanha)
  • Maior consumo de bombagem (GWh)

Aggregates (para incremental e gráficos YoY/mensais):
  • percRenovMensal: { 'MM/YYYY': % }
  • percRenovAnual:  { 'YYYY': % }
  • consumoMensalGwh: { 'MM/YYYY': GWh }
  • consumoAnualGwh:  { 'YYYY': GWh }

Output: data/records_producao.json com chaves 'recordes' e 'aggregates'.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from glob import glob

import pandas as pd

# === Caminhos ===
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, '..', 'data', 'producao'))
RECORDS_PATH = os.path.join(DATA_DIR, 'records_producao.json')
ATUAIS_PATH = os.path.join(DATA_DIR, 'producao_dados_atuais.csv')
BOMBAGEM_PATH = os.path.join(DATA_DIR, 'producao_bombagem_diaria.csv')
HISTORICO_GLOB = os.path.join(DATA_DIR, 'historico', 'producao_historico_*.csv')

# Forçar UTF-8 no stdout (necessário em Windows para emojis)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============================================================
# Leitura de ficheiros
# ============================================================

def ler_csv_producao(path):
    """Lê CSV de produção REN (15 min). Encoding utf-8-sig para lidar com BOM."""
    return pd.read_csv(path, encoding='utf-8-sig')


def ler_bombagem():
    """Lê producao_bombagem_diaria.csv → dict {DD/MM/YYYY: GWh_float}."""
    bomb = {}
    if not os.path.exists(BOMBAGEM_PATH):
        return bomb
    with open(BOMBAGEM_PATH, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            dia = (row.get('dia') or '').strip()
            val = (row.get('producao_bombagem_gwh') or '').strip()
            if dia and val:
                try:
                    bomb[dia] = float(val.replace(',', '.'))
                except ValueError:
                    pass
    return bomb


# ============================================================
# Agregação diária a partir de DataFrame de 15 min
# ============================================================

COLS_15MIN = [
    'Hídrica', 'Eólica', 'Solar', 'Biomassa', 'Ondas',
    'Carvão', 'Outra Térmica',
    'Importação', 'Exportação', 'Bombagem',
    'Injeção de Baterias', 'Consumo Baterias', 'Consumo'
]


def preparar_df(df):
    """Filtra linhas válidas, agrega 'Gás Natural', coerce numéricos."""
    df = df.copy()
    df = df[df['dia'].notna() & (df['dia'].astype(str).str.strip() != '')]
    df['Gás Natural'] = (
        pd.to_numeric(df.get('Gás Natural - Ciclo Combinado'), errors='coerce').fillna(0)
        + pd.to_numeric(df.get('Gás natural - Cogeração'), errors='coerce').fillna(0)
    )
    cols = COLS_15MIN + ['Gás Natural']
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)
    return df, cols


def agregar_diario(df, bombagem):
    """Agrega DataFrame de 15min em totais diários (MWh) + picos (MW)."""
    QH = 0.25
    df, cols = preparar_df(df)

    daily_sum = df.groupby('dia', sort=False)[cols].sum() * QH  # MWh
    daily_max = df.groupby('dia', sort=False)[cols].max()        # MW

    # Hora do pico de Consumo
    idx_pico = df.groupby('dia', sort=False)['Consumo'].idxmax()
    pico_hora = df.loc[idx_pico].set_index('dia')['hora'].astype(str).to_dict()

    daily = pd.DataFrame(index=daily_sum.index.copy())
    for c in cols:
        daily[f"{c}_e"] = daily_sum[c]   # MWh
        daily[f"{c}_p"] = daily_max[c]   # MW
    daily.index.name = 'dia'
    daily = daily.reset_index()

    daily['picoConsumoHora'] = daily['dia'].map(pico_hora)
    daily['bombGwh'] = daily['dia'].map(bombagem)

    # Derivadas em GWh
    g = lambda c: daily[f"{c}_e"] / 1000
    daily['hidricaGwh'] = g('Hídrica')
    daily['eolicaGwh'] = g('Eólica')
    daily['solarGwh'] = g('Solar')
    daily['biomassaGwh'] = g('Biomassa') + g('Ondas')
    daily['gasNaturalGwh'] = g('Gás Natural')
    daily['carvaoGwh'] = g('Carvão')
    daily['outraGwh'] = g('Outra Térmica')
    daily['baterIngGwh'] = g('Injeção de Baterias')
    daily['consumoGwh'] = g('Consumo')
    daily['bombagemConsumoGwh'] = g('Bombagem')

    # Hídrica renovável: subtrai bombagem turbinada quando disponível
    daily['hidricaRenovGwh'] = daily['hidricaGwh'].where(
        daily['bombGwh'].isna(),
        (daily['hidricaGwh'] - daily['bombGwh']).clip(lower=0)
    )

    daily['renovavelGwh'] = (
        daily['hidricaRenovGwh']
        + daily['eolicaGwh']
        + daily['solarGwh']
        + daily['biomassaGwh']
    )
    daily['producaoNacGwh'] = (
        daily['hidricaGwh']
        + daily['eolicaGwh']
        + daily['solarGwh']
        + daily['biomassaGwh']
        + daily['gasNaturalGwh']
        + daily['carvaoGwh']
        + daily['outraGwh']
        + daily['baterIngGwh']
    )
    daily['percRenov'] = (daily['renovavelGwh'] / daily['producaoNacGwh'] * 100)
    daily['percRenov'] = daily['percRenov'].replace([float('inf'), -float('inf')], 0).fillna(0)
    daily['saldoImpGwh'] = (g('Importação') - g('Exportação'))

    # Conversão de data para datetime (para ordenação e mês/ano)
    daily['dt'] = pd.to_datetime(daily['dia'], format='%d/%m/%Y', errors='coerce')
    daily = daily.dropna(subset=['dt']).reset_index(drop=True)
    return daily


# ============================================================
# Computação de recordes e aggregates
# ============================================================

def computar_recordes(daily):
    records = {}
    if daily.empty:
        return records

    def pegar(col, key, *, transform=None, max_=True, casas=2, extra=None):
        idx = daily[col].idxmax() if max_ else daily[col].idxmin()
        r = daily.loc[idx]
        v = float(r[col])
        if transform is not None:
            v = transform(v)
        rec = {'data': r['dia'], 'valor': round(v, casas)}
        if extra is not None:
            rec.update(extra(r))
        return rec

    records['diaMaisRenovavel'] = pegar('percRenov', 'diaMaisRenovavel', casas=2)
    records['diaMaiorConsumo'] = pegar('consumoGwh', 'diaMaiorConsumo', casas=2)
    records['diaMaiorPicoConsumo'] = pegar(
        'Consumo_p', 'diaMaiorPicoConsumo',
        casas=0,
        extra=lambda r: {'hora': str(r['picoConsumoHora']) if pd.notna(r['picoConsumoHora']) else None}
    )
    records['diaMaiorSolar'] = pegar('solarGwh', 'diaMaiorSolar', casas=2)
    records['diaMaiorEolica'] = pegar('eolicaGwh', 'diaMaiorEolica', casas=2)
    records['diaMaiorHidrica'] = pegar('hidricaGwh', 'diaMaiorHidrica', casas=2)
    records['diaMaiorBombagem'] = pegar('bombagemConsumoGwh', 'diaMaiorBombagem', casas=2)
    records['diaMaiorImportador'] = pegar('saldoImpGwh', 'diaMaiorImportador', casas=2)
    records['diaMaiorExportador'] = pegar('saldoImpGwh', 'diaMaiorExportador', max_=False, casas=2)
    return records


def computar_aggregates(daily):
    """Agregados mensais e anuais (para mostrar tendência e/ou retomar incremental)."""
    aggs = {'percRenovMensal': {}, 'percRenovAnual': {},
            'consumoMensalGwh': {}, 'consumoAnualGwh': {}}
    if daily.empty:
        return aggs
    daily = daily.copy()
    daily['mes'] = daily['dt'].dt.strftime('%m/%Y')
    daily['ano'] = daily['dt'].dt.year

    grp_m = daily.groupby('mes')
    for mes, g in grp_m:
        ren = float(g['renovavelGwh'].sum())
        prod = float(g['producaoNacGwh'].sum())
        cons = float(g['consumoGwh'].sum())
        aggs['percRenovMensal'][mes] = round(ren / prod * 100, 2) if prod > 0 else 0.0
        aggs['consumoMensalGwh'][mes] = round(cons, 2)

    grp_a = daily.groupby('ano')
    for ano, g in grp_a:
        ren = float(g['renovavelGwh'].sum())
        prod = float(g['producaoNacGwh'].sum())
        cons = float(g['consumoGwh'].sum())
        aggs['percRenovAnual'][str(int(ano))] = round(ren / prod * 100, 2) if prod > 0 else 0.0
        aggs['consumoAnualGwh'][str(int(ano))] = round(cons, 2)
    return aggs


# ============================================================
# Modos full / incremental
# ============================================================

def full_compute():
    csvs = sorted(glob(HISTORICO_GLOB))
    if os.path.exists(ATUAIS_PATH):
        csvs.append(ATUAIS_PATH)
    if not csvs:
        sys.exit('Nenhum CSV de produção encontrado em ' + DATA_DIR)

    print(f'[full] A ler {len(csvs)} ficheiros...')
    bombagem = ler_bombagem()
    print(f'[full] Bombagem diária: {len(bombagem)} dias')

    dfs = []
    for path in csvs:
        try:
            df = ler_csv_producao(path)
            dfs.append(df)
            print(f'  [OK] {os.path.basename(path)} ({len(df)} linhas)')
        except Exception as exc:
            print(f'  [ERR] {os.path.basename(path)} - erro: {exc}', file=sys.stderr)

    df_full = pd.concat(dfs, ignore_index=True)
    print(f'[full] Total: {len(df_full):,} quartos-horarios')

    daily = agregar_diario(df_full, bombagem)
    print(f'[full] Total: {len(daily):,} dias completos')

    records = computar_recordes(daily)
    aggs = computar_aggregates(daily)
    return records, aggs


def incremental_update():
    """Carrega só producao_dados_atuais.csv e funde com records_producao.json existente."""
    if not os.path.exists(RECORDS_PATH):
        print('records_producao.json não existe — a fazer fallback para --full')
        return full_compute()

    with open(RECORDS_PATH, 'r', encoding='utf-8') as f:
        old = json.load(f)

    if not os.path.exists(ATUAIS_PATH):
        sys.exit('producao_dados_atuais.csv não encontrado')

    print('[incremental] A ler producao_dados_atuais.csv...')
    df_at = ler_csv_producao(ATUAIS_PATH)
    bombagem = ler_bombagem()
    daily_at = agregar_diario(df_at, bombagem)
    print(f'[incremental] {len(daily_at)} dias no ficheiro atual')

    new_records = computar_recordes(daily_at)
    new_aggs = computar_aggregates(daily_at)

    # Merge aggregates: meses/anos do CSV atual sobrepõem-se aos antigos
    old_aggs = old.get('aggregates', {}) or {}
    merged_aggs = {
        'percRenovMensal':   {**old_aggs.get('percRenovMensal', {}),   **new_aggs.get('percRenovMensal', {})},
        'percRenovAnual':    {**old_aggs.get('percRenovAnual', {}),    **new_aggs.get('percRenovAnual', {})},
        'consumoMensalGwh':  {**old_aggs.get('consumoMensalGwh', {}),  **new_aggs.get('consumoMensalGwh', {})},
        'consumoAnualGwh':   {**old_aggs.get('consumoAnualGwh', {}),   **new_aggs.get('consumoAnualGwh', {})},
    }

    # Records "rolling" — comparar e manter extremo
    final_records = dict(old.get('recordes', {}) or {})

    def replace_if_higher(field, value_key='valor'):
        if field not in new_records:
            return
        old_v = (final_records.get(field) or {}).get(value_key)
        new_v = new_records[field][value_key]
        if old_v is None or new_v > old_v:
            final_records[field] = new_records[field]

    def replace_if_lower(field, value_key='valor'):
        if field not in new_records:
            return
        old_v = (final_records.get(field) or {}).get(value_key)
        new_v = new_records[field][value_key]
        if old_v is None or new_v < old_v:
            final_records[field] = new_records[field]

    replace_if_higher('diaMaisRenovavel')
    replace_if_higher('diaMaiorConsumo')
    replace_if_higher('diaMaiorPicoConsumo')
    replace_if_higher('diaMaiorSolar')
    replace_if_higher('diaMaiorEolica')
    replace_if_higher('diaMaiorHidrica')
    replace_if_higher('diaMaiorBombagem')
    replace_if_higher('diaMaiorImportador')
    replace_if_lower('diaMaiorExportador')  # mais negativo = exportador maior

    return final_records, merged_aggs


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Gera/atualiza data/records_producao.json')
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
