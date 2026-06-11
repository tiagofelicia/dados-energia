#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_producao_REN.py
Atualiza data/producao_dados_atuais.csv com dados da Repartição da Produção
da REN (serviço 1354), a 15 min.

Uso:
  python atualizar_producao_REN.py                  # últimos 14 dias (re-pedidos sempre)
  python atualizar_producao_REN.py --dias 30        # últimos 30 dias
  python atualizar_producao_REN.py --from 2026-01-01 --to 2026-06-03   # backfill custom
  python atualizar_producao_REN.py --ano-completo   # 01/01 do ano corrente até hoje (full refresh)

Comportamento:
  • Modo default (sem args): pede sempre os últimos 14 dias e funde no CSV existente
    (substitui as linhas desses dias). A REN publica correções retroativas em dias
    provisórios, daí re-pedir sempre.
  • Fallback automático para "ano completo" quando:
        – o CSV ainda não existe; ou
        – o ano do CSV é diferente do ano corrente (rollover de ano).
  • Cada pedido tem retry com exponential backoff (3 tentativas: 2s, 4s, 8s).
  • Timeout de 60s (REN datahub tem latência variável).
"""

import argparse
import io
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

# --- 1. CONFIGURAÇÕES ---

API_REN_SERVICE_ID = "1354"
URL_REN_API = f"https://datahub.ren.pt/service/download/csv/{API_REN_SERVICE_ID}"
# Caminho ancorado no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
OUT_PATH = os.path.join(ROOT_DIR, "data", "producao", "producao_dados_atuais.csv")

HEADERS = {
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Pragma': 'no-cache',
    'Expires': '0',
}


# --- 2. BUSCA NA API ---

def buscar_dados_ren(data_inicio, data_fim, max_tentativas=3, timeout=60):
    """
    Busca dados de Repartição da Produção da REN para um intervalo.
    Retorna DataFrame ou None em caso de falha (após retries).
    """
    print(f"Buscando dados da REN de {data_inicio} a {data_fim}...")
    params = {
        "startDateString": data_inicio,
        "endDateString": data_fim,
        "culture": "pt-PT",
    }
    for tentativa in range(1, max_tentativas + 1):
        try:
            response = requests.get(URL_REN_API, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            response_text = response.content.decode('utf-8')
            df = pd.read_csv(io.StringIO(response_text), sep=';', skiprows=2, engine='python')
            df['datetime'] = pd.to_datetime(df['Data e Hora'], format='%Y-%m-%d %H:%M:%S')
            print(f"  [OK] {len(df)} linhas carregadas (sem cache).")
            return df
        except Exception as e:
            if tentativa < max_tentativas:
                espera = 2 ** tentativa  # 2s, 4s, 8s
                print(f"  [!] tentativa {tentativa}/{max_tentativas} falhou ({type(e).__name__}); a tentar de novo em {espera}s...", file=sys.stderr)
                time.sleep(espera)
            else:
                print(f"  [ERR] {max_tentativas} tentativas falharam: {e}", file=sys.stderr)
    return None


# --- 3. TRANSFORMAÇÃO ---

def transformar_dados(df_ren):
    """
    Transforma o DataFrame da REN para o formato esperado:
    cria as colunas 'dia', 'hora' e 'intervalo'.
    """
    if df_ren is None or 'datetime' not in df_ren.columns:
        print("DataFrame de entrada inválido. Transformação cancelada.")
        return None

    print("Transformando para o formato OMIE-compatível...")
    df = df_ren.copy()
    df['dia'] = df['datetime'].dt.strftime('%d/%m/%Y')

    hora_inicio_str = df['datetime'].dt.strftime('%H:%M')
    datetime_fim = df['datetime'] + pd.Timedelta(minutes=15)
    hora_fim_str = datetime_fim.dt.strftime('%H:%M')

    df['intervalo'] = '[' + hora_inicio_str + '-' + hora_fim_str + '['
    # 'hora' é o fim do intervalo; '00:00' representa-se como '23:59'
    df['hora'] = hora_fim_str.replace('00:00', '23:59')

    cols_remover = ['Data e Hora', 'datetime']
    cols_dados = [c for c in df.columns if c not in cols_remover]
    cols_finais = ['dia', 'hora', 'intervalo'] + \
                  [c for c in cols_dados if c not in ['dia', 'hora', 'intervalo']]
    return df[cols_finais]


# --- 4. MERGE COM CSV EXISTENTE ---

def carregar_csv_existente():
    """Carrega o CSV existente como DataFrame. Retorna None se não existir/falhar."""
    if not os.path.exists(OUT_PATH):
        return None
    try:
        return pd.read_csv(OUT_PATH, encoding='utf-8-sig')
    except Exception as e:
        print(f"  [!] Erro a ler {OUT_PATH}: {e}", file=sys.stderr)
        return None


def fundir_e_gravar(df_existente, df_novo):
    """
    Funde df_novo no df_existente:
      - Remove de df_existente as linhas cujos 'dia' aparecem em df_novo
      - Anexa df_novo
      - Ordena cronologicamente por (dia, intervalo inicial)
      - Grava em OUT_PATH
    """
    if df_existente is None or df_existente.empty:
        final = df_novo.copy()
    else:
        dias_novos = set(df_novo['dia'].unique())
        df_keep = df_existente[~df_existente['dia'].isin(dias_novos)].copy()
        final = pd.concat([df_keep, df_novo], ignore_index=True)

    # Ordenação cronológica: usar o INÍCIO do intervalo (extraído de 'intervalo' [HH:MM-...]).
    # Evita problemas com 'hora' = '23:59' representar o fim de [23:45-00:00[.
    inicio = final['intervalo'].astype(str).str[1:6]  # '[00:00' → '00:00'
    final['_ord'] = pd.to_datetime(final['dia'] + ' ' + inicio,
                                   format='%d/%m/%Y %H:%M', errors='coerce')
    final = final.dropna(subset=['_ord']).sort_values('_ord').drop(columns='_ord').reset_index(drop=True)

    final.to_csv(OUT_PATH, index=False, encoding='utf-8-sig')
    print(f"\nSUCESSO! '{OUT_PATH}' guardado com {len(final)} registos.")
    if not final.empty:
        print(f"Intervalo: {final['dia'].iloc[0]} a {final['dia'].iloc[-1]}")


# --- 5. EXECUÇÃO PRINCIPAL ---

def main():
    ap = argparse.ArgumentParser(description='Atualiza dados de produção REN (serviço 1354).')
    ap.add_argument('--from', dest='dfrom', help='Data início YYYY-MM-DD (backfill custom)')
    ap.add_argument('--to', dest='dto', help='Data fim YYYY-MM-DD (backfill custom)')
    ap.add_argument('--dias', type=int, default=14,
                    help='Nº de dias recentes a re-pedir (default 14, re-pedidos sempre)')
    ap.add_argument('--ano-completo', action='store_true',
                    help='Re-baixar 01/01 do ano corrente até hoje (full refresh)')
    args = ap.parse_args()

    print("--- INICIANDO atualizar_producao_REN.py ---")
    hoje = date.today()

    df_existente = carregar_csv_existente()

    # Determinar intervalo de pedido
    if args.ano_completo:
        d0, d1 = date(hoje.year, 1, 1), hoje
        descartar_existente = True  # ano completo substitui tudo do ano corrente
        modo = "ano completo"
    elif args.dfrom and args.dto:
        d0 = datetime.strptime(args.dfrom, '%Y-%m-%d').date()
        d1 = datetime.strptime(args.dto, '%Y-%m-%d').date()
        descartar_existente = False
        modo = "backfill custom"
    else:
        # Modo default: últimos N dias
        d1 = hoje
        d0 = d1 - timedelta(days=max(1, args.dias) - 1)
        descartar_existente = False
        modo = f"últimos {args.dias} dias"

        # Fallback: se o CSV ainda não existe, ou é de um ano anterior, baixar ano completo
        precisa_fallback = False
        if df_existente is None or df_existente.empty:
            precisa_fallback = True
            print("  [!] CSV ainda não existe — fallback para ano completo.")
        else:
            ano_existente = str(df_existente['dia'].iloc[-1]).split('/')[-1]
            if ano_existente != str(hoje.year):
                precisa_fallback = True
                print(f"  [!] CSV existente é de {ano_existente}, ano corrente é {hoje.year} — fallback para ano completo.")
        if precisa_fallback:
            d0, d1 = date(hoje.year, 1, 1), hoje
            descartar_existente = True
            modo = "ano completo (fallback)"

    print(f"[modo: {modo}] {d0} a {d1}")

    df_bruto = buscar_dados_ren(d0.strftime('%Y-%m-%d'), d1.strftime('%Y-%m-%d'))
    if df_bruto is None:
        sys.exit("Falha na busca de dados. Script terminado.")

    df_novo = transformar_dados(df_bruto)
    if df_novo is None or df_novo.empty:
        sys.exit("Falha na transformação ou dados vazios.")

    fundir_e_gravar(None if descartar_existente else df_existente, df_novo)
    print("--- SCRIPT TERMINADO ---")


if __name__ == "__main__":
    main()
