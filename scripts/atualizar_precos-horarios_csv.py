# ============================================================
# SISTEMA COMPLETO DE ATUALIZAÇÃO DE PREÇOS-HORÁRIOS
# ============================================================

import pandas as pd
import requests
import re
from datetime import datetime
import io
import csv

# ============================================================
# 1. CARREGADOR DE DADOS LOCAIS
# ============================================================

def carregar_dados_locais(ficheiro_historico):
    """
    Carrega os dados quarto-horários do ficheiro CSV local
    preparado pela Fase 1 (atualizar_mibel_ano_atual_ACUM.py).
    
    Este script foca-se apenas no Preco_PT.
    """
    print(f"ℹ️ A carregar dados locais de '{ficheiro_historico}'...")
    try:
        df = pd.read_csv(
            ficheiro_historico,
            parse_dates=['Data'],
            usecols=['Data', 'Hora', 'Preco_PT'] # Só precisamos de PT para este script
        )
        
        # O script principal espera a coluna de preço chamada 'Preco'
        df = df.rename(columns={'Preco_PT': 'Preco'})
        
        # Garantir a ordenação
        df = df.sort_values(['Data', 'Hora']).reset_index(drop=True)
        
        print(f"✅ Dados locais carregados: {len(df)} registos")
        print(f"   - Período: {df['Data'].min().date()} a {df['Data'].max().date()}")
        
        return df
        
    except FileNotFoundError:
        print(f"❌ ERRO: Ficheiro histórico '{ficheiro_historico}' não encontrado.")
        print("   - Por favor, execute primeiro o script 'atualizar_mibel_ano_atual_ACUM.py'.")
        raise
    except Exception as e:
        print(f"❌ ERRO: Falha ao ler o ficheiro '{ficheiro_historico}': {e}")
        raise


# ============================================================
# 2. MOTOR DE CÁLCULO
# ============================================================

def gerar_tabelas_tarifarias(df_omie, ficheiro_config):
    """
    Função principal que orquestra todos os cálculos.
    Garante que todos os intervalos de tempo (96 por dia) são gerados, mesmo que faltem dados OMIE no final do dia.
    """
    print("ℹ️ A iniciar cálculos dos tarifários...")

    # 1. Carregar dados de configuração do Excel (caminho local ou URL)
    try:
        print(f"   - A ler configuração de: {ficheiro_config}")
        if str(ficheiro_config).startswith("http"):
            r = requests.get(ficheiro_config, timeout=10)
            r.raise_for_status()
            excel_bytes = io.BytesIO(r.content)
        else:
            with open(ficheiro_config, 'rb') as fh:
                excel_bytes = io.BytesIO(fh.read())
        
        constantes_df = pd.read_excel(excel_bytes, sheet_name="Constantes")
        constantes_dict = dict(zip(constantes_df["constante"], constantes_df["valor_unitário"]))
        
        # O read_excel precisa de "reiniciar" o cursor dos bytes
        excel_bytes.seek(0) 
        omie_perdas_ciclos = pd.read_excel(excel_bytes, sheet_name="OMIE_PERDAS_CICLOS")
        
        omie_perdas_ciclos['DataHora'] = omie_perdas_ciclos.apply(lambda r: pd.to_datetime(f"{r['Data']} {r['Hora']}", errors='coerce'), axis=1)
        omie_perdas_ciclos['DataHora'] = (omie_perdas_ciclos['DataHora'].dt.tz_localize('Europe/Madrid', nonexistent='shift_forward', ambiguous='NaT').dt.tz_convert('Europe/Lisbon').dt.tz_localize(None))
        omie_perdas_ciclos['DataHora'] = omie_perdas_ciclos['DataHora'] + pd.Timedelta(minutes=45)
        omie_perdas_ciclos['DataHora'] = omie_perdas_ciclos['DataHora'].dt.round('15min')
        print("   - Ficheiro de configuração lido com sucesso.")
    except Exception as e:
        print(f"❌ Erro ao carregar configuração: {e}")
        raise

    # 2. Preparar DataFrame OMIE com DataHora
    df_omie_trabalho = df_omie.copy()
    df_omie_trabalho['DataHora'] = df_omie_trabalho.apply(
        lambda row: pd.Timestamp(row['Data'], tz='Europe/Madrid') + pd.Timedelta(minutes=15 * (row['Hora'] - 1)),
        axis=1
    ).dt.tz_convert('Europe/Lisbon').dt.tz_localize(None)


    # --- Criar um "MOLDE" com todos os intervalos de 15 minutos ---
    print("   - A criar molde de tempo completo para garantir 96 registos/dia...")
    dias_a_processar = df_omie_trabalho['Data'].dt.date.unique() # Usar df_omie_trabalho que já está filtrado
    todos_intervalos = []
    
    # Lógica para garantir os 100 quartos-horários no dia da mudança
    for dia in dias_a_processar:
        # Identificar o dia da mudança de hora (último Domingo de Outubro)
        if dia.month == 10 and dia.weekday() == 6 and dia.day > (31 - 7):
             # Dia da mudança de hora de Outubro (25 horas)
             intervalos_dia = pd.date_range(start=pd.Timestamp(dia, tz='Europe/Lisbon'), periods=100, freq='15min')
        elif dia.month == 3 and dia.weekday() == 6 and dia.day > (31 - 7):
             # Dia da mudança de hora de Março (23 horas)
             intervalos_dia = pd.date_range(start=pd.Timestamp(dia, tz='Europe/Lisbon'), periods=92, freq='15min')
        else:
             # Dia normal (24 horas)
             intervalos_dia = pd.date_range(start=pd.Timestamp(dia, tz='Europe/Lisbon'), periods=96, freq='15min')
        
        todos_intervalos.extend(intervalos_dia)
    
    # DataFrame com todos os intervalos de tempo possíveis, em hora de Portugal
    df_scaffold = pd.DataFrame({'DataHora': todos_intervalos}).copy()
    df_scaffold['DataHora'] = df_scaffold['DataHora'].dt.tz_localize(None) # Remover fuso horário para o merge

    # Juntar o molde com os dados OMIE. Onde não houver dados, 'Preco' será NaN.
    df_omie_completo = pd.merge(df_scaffold, df_omie_trabalho, on='DataHora', how='left')
    # --------------------------------------------------------------------

    # 3. Fazer merge com OMIE_PERDAS_CICLOS
    df_merged = pd.merge(
        df_omie_completo,
        omie_perdas_ciclos[['DataHora', 'Perdas', 'BD', 'BS', 'TD', 'TS']],
        on='DataHora',
        how='left'
    )
    df_merged['Perdas'] = df_merged['Perdas'].fillna(1.04)

    # 4. Calcular preços para cada comercializador
    resultados = []
    comercializadores = [
        "Alfa Power Index BTN", "Coopérnico Base", "Coopérnico GO", "EDP Indexada Horária",
        "EZU Tarifa Indexada", "Galp Plano Dinâmico", "G9 Smart Dynamic",
        "MeoEnergia Tarifa Dinâmica", "Repsol Leve Sem Mais",
        "Iberdrola - Simples Indexado Dinâmico", "Plenitude - Tendência",
    ]
    ordem_ciclo = {
        'Simples': 0, 'Bi-horário - Ciclo Diário': 1, 'Bi-horário - Ciclo Semanal': 2,
        'Tri-horário - Ciclo Diário': 3, 'Tri-horário - Ciclo Semanal': 4,
        'Tri-horário > 20.7 kVA - Ciclo Diário': 5, 'Tri-horário > 20.7 kVA - Ciclo Semanal': 6
    }

    # Tarifários que só existem na opção Simples
    so_simples = {"Iberdrola - Simples Indexado Dinâmico", "Plenitude - Tendência"}

    for _, row in df_merged.iterrows():
        dt = row['DataHora']
        
        # Salvaguarda para intervalos NaT que possam aparecer do merge
        if pd.isna(dt):
            continue
            
        hora_inicio = dt.strftime('%H:%M')
        hora_fim = (dt + pd.Timedelta(minutes=15)).strftime('%H:%M')
        hora_str = f"[{hora_inicio}-{hora_fim}["
        
        # Verifica se existe preço OMIE para este intervalo de tempo
        if pd.isna(row['Preco']):
            # CASO 1: NÃO HÁ DADOS OMIE -> Criar linhas com valores vazios
            for comercializador in comercializadores:
                # É preciso determinar os ciclos mesmo sem preço
                
                ciclos_a_processar = []
                if comercializador not in so_simples:
                    if pd.notna(row.get('BD')): ciclos_a_processar.append({'opcao_nome': 'Bi-horário - Ciclo Diário'})
                    if pd.notna(row.get('BS')): ciclos_a_processar.append({'opcao_nome': 'Bi-horário - Ciclo Semanal'})
                    if pd.notna(row.get('TD')): 
                        ciclos_a_processar.append({'opcao_nome': 'Tri-horário - Ciclo Diário'})
                        ciclos_a_processar.append({'opcao_nome': 'Tri-horário > 20.7 kVA - Ciclo Diário'})
                    if pd.notna(row.get('TS')): 
                        ciclos_a_processar.append({'opcao_nome': 'Tri-horário - Ciclo Semanal'})
                        ciclos_a_processar.append({'opcao_nome': 'Tri-horário > 20.7 kVA - Ciclo Semanal'})
                ciclos_a_processar.append({'opcao_nome': 'Simples'})

                for ciclo in list({c['opcao_nome']: c for c in ciclos_a_processar}.values()): # Remove duplicados
                    resultados.append({
                        'Dia': dt.strftime('%Y-%m-%d'),
                        'Tarifário': comercializador,
                        'Opção Horária e Ciclo': ciclo['opcao_nome'],
                        'Hora': hora_str,
                        'Valor': None,  # VALORES NUMÉRICOS FICAM VAZIOS
                        'OMIE_PT': None,
                        'TAR': None,
                        'OMIE*Perdas+TAR': None,
                        '_ordem_ciclo': ordem_ciclo.get(ciclo['opcao_nome'], 99)
                    })
        else:
            # CASO 2: HÁ DADOS OMIE -> Calcular normalmente
            omie_kwh = row['Preco'] / 1000.0
            perdas = row['Perdas']
            
            for comercializador in comercializadores:
                preco_comercializador_kwh = calcular_preco_comercializador(comercializador, omie_kwh, perdas, constantes_dict)
                
                ciclos_processar = []
                ciclos_processar.append({'ciclo_codigo': 'S','periodo': None, 'opcao_nome': 'Simples','tar_key': 'TAR_Energia_Simples'})
                if comercializador not in so_simples:
                    if pd.notna(row.get('BD')): ciclos_processar.append({'ciclo_codigo': 'BD','periodo': row['BD'],'opcao_nome': 'Bi-horário - Ciclo Diário','tar_key': 'TAR_Energia_Bi_Vazio' if row['BD'] == 'V' else 'TAR_Energia_Bi_ForaVazio'})
                    if pd.notna(row.get('BS')): ciclos_processar.append({'ciclo_codigo': 'BS','periodo': row['BS'],'opcao_nome': 'Bi-horário - Ciclo Semanal','tar_key': 'TAR_Energia_Bi_Vazio' if row['BS'] == 'V' else 'TAR_Energia_Bi_ForaVazio'})
                    if pd.notna(row.get('TD')): 
                        periodo_td = row['TD']
                        tar_map_td = {'V': 'TAR_Energia_Tri_Vazio','C': 'TAR_Energia_Tri_Cheias','P': 'TAR_Energia_Tri_Ponta'}
                        ciclos_processar.append({'ciclo_codigo': 'TD','periodo': periodo_td,'opcao_nome': 'Tri-horário - Ciclo Diário','tar_key': tar_map_td.get(periodo_td)})
                        tar_map_td_alta = {'V': 'TAR_Energia_Tri_27.6_Vazio','C': 'TAR_Energia_Tri_27.6_Cheias','P': 'TAR_Energia_Tri_27.6_Ponta'}
                        ciclos_processar.append({'ciclo_codigo': 'TD>20.7','periodo': periodo_td,'opcao_nome': 'Tri-horário > 20.7 kVA - Ciclo Diário','tar_key': tar_map_td_alta.get(periodo_td)})
                    if pd.notna(row.get('TS')):
                        periodo_ts = row['TS']
                        tar_map_ts = {'V': 'TAR_Energia_Tri_Vazio','C': 'TAR_Energia_Tri_Cheias','P': 'TAR_Energia_Tri_Ponta'}
                        ciclos_processar.append({'ciclo_codigo': 'TS','periodo': periodo_ts,'opcao_nome': 'Tri-horário - Ciclo Semanal','tar_key': tar_map_ts.get(periodo_ts)})
                        tar_map_ts_alta = {'V': 'TAR_Energia_Tri_27.6_Vazio','C': 'TAR_Energia_Tri_27.6_Cheias','P': 'TAR_Energia_Tri_27.6_Ponta'}
                        ciclos_processar.append({'ciclo_codigo': 'TS>20.7','periodo': periodo_ts,'opcao_nome': 'Tri-horário > 20.7 kVA - Ciclo Semanal','tar_key': tar_map_ts_alta.get(periodo_ts)})

                for ciclo_info in ciclos_processar:
                    # Se a tar_key não for encontrada, tar_kwh será 0.0 (seguro)
                    tar_kwh = constantes_dict.get(ciclo_info['tar_key'], 0.0) 
                    valor_final_kwh = preco_comercializador_kwh + tar_kwh
                    omie_perdas_tar_kwh = (omie_kwh * perdas) + tar_kwh
                    
                    resultados.append({
                        'Dia': dt.strftime('%Y-%m-%d'),
                        'Tarifário': comercializador,
                        'Opção Horária e Ciclo': ciclo_info['opcao_nome'],
                        'Hora': hora_str,
                        'Valor': round(valor_final_kwh, 5),
                        'OMIE_PT': round(omie_kwh, 5),
                        'TAR': round(tar_kwh, 5),
                        'OMIE*Perdas+TAR': round(omie_perdas_tar_kwh, 5),
                        '_ordem_ciclo': ordem_ciclo.get(ciclo_info['opcao_nome'], 99)
                    })

    # 5. Gerar tabelas finais
    df_quarto_horario_final = pd.DataFrame(resultados)
    df_quarto_horario_final = df_quarto_horario_final.sort_values(
        by=['Tarifário', 'Dia', '_ordem_ciclo', 'Hora'],
        ascending=[True, False, True, True]
    ).reset_index(drop=True)
    df_quarto_horario_final = df_quarto_horario_final.drop(columns=['_ordem_ciclo'])
    print(f"   - Cálculos quarto-horários concluídos: {len(df_quarto_horario_final)} registos")

    # Calcular médias horárias
    df_quarto_horario_final['Hora_Int'] = df_quarto_horario_final['Hora'].str.extract(r'\[(\d{2}):\d{2}').astype(int)
    # Lidar com valores 'None' (que se tornam NaN) antes de agregar
    cols_numericas = ['OMIE_PT', 'Valor']
    for col in cols_numericas:
        df_quarto_horario_final[col] = pd.to_numeric(df_quarto_horario_final[col], errors='coerce')
        
    df_horario_final = df_quarto_horario_final.groupby(
        ['Dia', 'Tarifário', 'Opção Horária e Ciclo', 'Hora_Int']
    ).agg(
        OMIE_PT=('OMIE_PT', 'mean'),
        Valor=('Valor', 'mean')
    ).reset_index()
    
    df_horario_final['Hora'] = df_horario_final['Hora_Int'].apply(lambda h: f"[{h:02d}:00-{(h + 1) % 24:02d}:00[")
    df_horario_final['_ordem_ciclo'] = df_horario_final['Opção Horária e Ciclo'].map(ordem_ciclo)
    df_horario_final = df_horario_final.sort_values(
        by=['Tarifário', 'Dia', '_ordem_ciclo', 'Hora_Int'],
        ascending=[True, False, True, True]
    ).reset_index(drop=True)
    df_horario_final = df_horario_final.drop(columns=['_ordem_ciclo', 'Hora_Int'])
    df_horario_final = df_horario_final[['Dia', 'Tarifário', 'Opção Horária e Ciclo', 'Hora', 'OMIE_PT', 'Valor']]
    print(f"   - Tabela horária gerada: {len(df_horario_final)} registos")

    print("✅ Cálculos concluídos.")

    # Filtrar apenas as constantes efetivamente utilizadas nos cálculos
    chaves_utilizadas = {
        # Tarifários
        'Alfa_CGS', 'Alfa_K',
        'Coop_CS_CR', 'Coop_K', 'Coop_GO',
        'EDP_H_K1', 'EDP_H_K2',
        'EZU_K', 'EZU_CGS',
        'Galp_Ci',
        'G9_FA', 'G9_CGS', 'G9_AC',
        'Iberdrola_Dinamico_Q', 'Iberdrola_mFRR',
        'Meo_K',
        'Repsol_FA', 'Repsol_Q_Tarifa',
        'Plenitude_CGS', 'Plenitude_GDOs', 'Plenitude_Fee',
        'Financiamento_TSE',
        # TAR Energia
        'TAR_Energia_Simples',
        'TAR_Energia_Bi_Vazio', 'TAR_Energia_Bi_ForaVazio',
        'TAR_Energia_Tri_Vazio', 'TAR_Energia_Tri_Cheias', 'TAR_Energia_Tri_Ponta',
        'TAR_Energia_Tri_27.6_Vazio', 'TAR_Energia_Tri_27.6_Cheias', 'TAR_Energia_Tri_27.6_Ponta',
    }
    constantes_utilizadas = {k: v for k, v in constantes_dict.items() if k in chaves_utilizadas}

    return df_quarto_horario_final, df_horario_final, constantes_utilizadas


def calcular_preco_comercializador(nome_tarifario, omie_kwh, perdas, constantes_dict):
    """
    Calcula o preço do comercializador baseado nas fórmulas específicas. RETORNA: Preço em €/kWh
    """
    if "Alfa Power Index BTN" in nome_tarifario:
        return ((omie_kwh + constantes_dict.get('Alfa_CGS', 0.0)) * perdas + constantes_dict.get('Alfa_K', 0.0) + constantes_dict.get('Financiamento_TSE', 0.0))
    
    elif nome_tarifario == "Coopérnico Base":
        return (omie_kwh + constantes_dict.get('Coop_CS_CR', 0.0) + constantes_dict.get('Coop_K', 0.0)) * perdas + constantes_dict.get('Financiamento_TSE', 0.0)

    elif nome_tarifario == "Coopérnico GO":
        return (omie_kwh + constantes_dict.get('Coop_CS_CR', 0.0) + constantes_dict.get('Coop_K', 0.0)) * perdas + constantes_dict.get('Coop_GO', 0.0) + constantes_dict.get('Financiamento_TSE', 0.0)

    elif "EDP Indexada Horária" in nome_tarifario:
        return (omie_kwh * perdas * constantes_dict.get('EDP_H_K1', 1.0) + constantes_dict.get('EDP_H_K2', 0.0))
    
    elif "EZU Tarifa Indexada" in nome_tarifario:
        return (omie_kwh + constantes_dict.get('EZU_K', 0.0) + constantes_dict.get('EZU_CGS', 0.0)) * perdas + constantes_dict.get('Financiamento_TSE', 0.0)
        
    elif "Galp Plano Dinâmico" in nome_tarifario:
        return (omie_kwh + constantes_dict.get('Galp_Ci', 0.0)) * perdas    
    
    elif "G9 Smart Dynamic" in nome_tarifario:
        # REGRA G9: Se o valor OMIE for negativo, considera-se 0.
        omie_kwh_g9 = max(0, omie_kwh)
        return (omie_kwh_g9 * constantes_dict.get('G9_FA', 0.0) * perdas + constantes_dict.get('G9_CGS', 0.0) + constantes_dict.get('G9_AC', 0.0))
    
    elif "MeoEnergia Tarifa Dinâmica" in nome_tarifario:
        # REGRA MEO 1: (omie_kwh + Meo_K) não pode ser negativo
        base_meo = omie_kwh + constantes_dict.get('Meo_K', 0.0)
        base_limitada = max(0, base_meo)
        
        # Calcula o resultado da fórmula
        resultado_formula = base_limitada * perdas
        
        # REGRA MEO 2: Se o resultado final for negativo, aplica-se €0
        return max(0, resultado_formula)    
    
    elif "Repsol Leve Sem Mais" in nome_tarifario:
        return (omie_kwh * perdas * constantes_dict.get('Repsol_FA', 0.0) + constantes_dict.get('Repsol_Q_Tarifa', 0.0)) + constantes_dict.get('Financiamento_TSE', 0.0)
    
    elif "Iberdrola - Simples Indexado Dinâmico" in nome_tarifario:
        return (omie_kwh * perdas + constantes_dict.get("Iberdrola_Dinamico_Q", 0.0) + constantes_dict.get('Iberdrola_mFRR', 0.0))

    elif "Plenitude - Tendência" in nome_tarifario:
        return (omie_kwh + constantes_dict.get('Plenitude_CGS', 0.0) + constantes_dict.get('Plenitude_GDOs', 0.0)) * perdas + constantes_dict.get('Plenitude_Fee', 0.0)

    else:
        # Fallback
        return omie_kwh * perdas


# ============================================================
# 3. EXPORTADOR CSV
# ============================================================

def exportar_para_csv_compativel(df_q_horario, df_horario, constantes_dict, nome_ficheiro):
    """
    Gera o ficheiro CSV com a formatação final e exata.
    """
    print(f"ℹ️ A gerar ficheiro CSV com formato final para '{nome_ficheiro}'...")

    # --- 1. PREPARAÇÃO DA TABELA QUARTO-HORÁRIA ---
    df_q_export = df_q_horario.copy()
    
    df_q_export['Dia'] = pd.to_datetime(df_q_export['Dia']).dt.strftime('%d/%m/%Y')
    
    df_q_export = df_q_export.rename(columns={
        'Dia': 'dia', 'Tarifário': 'tarifario', 'Opção Horária e Ciclo': 'opcao',
        'Hora': 'intervalo', 'Valor': 'col', 'OMIE_PT': 'omie',
        'TAR': 'tar', 'OMIE*Perdas+TAR': 'omieTar'
    })
    colunas_qh = ['dia', 'tarifario', 'opcao', 'intervalo', 'col', 'omie', 'tar', 'omieTar']
    df_q_export = df_q_export[colunas_qh]

    # --- 2. PREPARAÇÃO DA TABELA HORÁRIA ---
    df_h_export = df_horario.copy()

    df_h_export['Dia'] = pd.to_datetime(df_h_export['Dia']).dt.strftime('%d/%m/%Y')

    df_h_export['OMIE_PT'] = df_h_export['OMIE_PT'] * 1000
    df_h_export = df_h_export.rename(columns={
        'Dia': 'CSV_Dia', 'Tarifário': 'CSV_Tarifario', 'Opção Horária e Ciclo': 'CSV_Opcao',
        'Hora': 'CSV_Hora', 'OMIE_PT': 'CSV_OMIE_Medio_MWh', 'Valor': 'CSV_Preco_Medio_kWh'
    })
    colunas_horarias = ['CSV_Dia', 'CSV_Tarifario', 'CSV_Opcao', 'CSV_Hora', 'CSV_OMIE_Medio_MWh', 'CSV_Preco_Medio_kWh']
    df_h_export = df_h_export[colunas_horarias]

    # --- 3. PREPARAÇÃO DA TABELA CONSTANTES ---
    df_const = pd.DataFrame(list(constantes_dict.items()), columns=['constante', 'valor_unitário'])

    # --- 4. CONSTRUÇÃO DO FICHEIRO CSV ---
    try:
        # Garantir que o diretório 'data/' existe
        import os
        os.makedirs(os.path.dirname(nome_ficheiro), exist_ok=True)
        
        with open(nome_ficheiro, 'w', encoding='utf-8-sig', newline='') as f:
            # Tabela 1: Quarto-Horária
            f.write(df_q_export.to_csv(index=False, header=True, decimal='.', float_format='%.5f'))
            
            # Separador + Tabela 2: Horária (offset coluna K = 10 vírgulas)
            f.write("\n" + "," * 10 + "TABELA_HORARIA\n")

            df_h_export['CSV_OMIE_Medio_MWh'] = df_h_export['CSV_OMIE_Medio_MWh'].apply(
                lambda x: f'{x:.2f}' if pd.notna(x) else ''
            )
            df_h_export['CSV_Preco_Medio_kWh'] = df_h_export['CSV_Preco_Medio_kWh'].apply(
                lambda x: f'{x:.5f}' if pd.notna(x) else ''
            )
            
            csv_tabela_h = df_h_export.to_csv(index=False, header=True, decimal='.')
            offset_h = "," * 10
            for i, linha in enumerate(csv_tabela_h.strip().split('\n')):
                f.write(offset_h + linha + '\n')

            # Separador + Tabela 3: Constantes (offset coluna S = 18 vírgulas)
            f.write("\n" + "," * 18 + "TABELA_CONSTANTES\n")

            csv_tabela_c = df_const.to_csv(index=False, header=True, decimal='.')
            offset_c = "," * 18
            for linha in csv_tabela_c.strip().split('\n'):
                f.write(offset_c + linha + '\n')

        print(f"✅ Ficheiro CSV '{nome_ficheiro}' criado com o formato definitivo.")
    
    except Exception as e:
        print(f"❌ ERRO ao escrever o ficheiro CSV '{nome_ficheiro}': {e}")
        raise

# ============================================================
# 4. MAIN (Adaptado)
# ============================================================

def main():
    print("=" * 60)
    print("SISTEMA DE ATUALIZAÇÃO DE PREÇOS-HORÁRIOS")
    print("(Fonte: MIBEL_ano_atual_ACUM.csv)")
    print("=" * 60)

    # --- Definir constantes ---
    # Caminhos ancorados no diretório do script (e não no cwd), para funcionar
    # tanto quando é corrido a partir da raiz do repositório como de scripts/.
    import os as _os
    _SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
    _ROOT_DIR = _os.path.dirname(_SCRIPT_DIR)
    # Configuração de tarifários: o xlsx do simulador vive neste mesmo repo.
    # No workflow, este script corre DEPOIS de atualizar_tarifarios_eletricidade.py,
    # pelo que lê o xlsx acabado de atualizar (OMIE+OMIP frescos).
    URL_CONFIG = _os.path.join(_ROOT_DIR, "data", "simuladores",
                               "simulador-tarifarios-eletricidade",
                               "tarifarios_eletricidade_Tiago_Felicia.xlsx")
    FICHEIRO_MIBEL_CSV = _os.path.join(_ROOT_DIR, "data", "omie", "MIBEL_ano_atual_ACUM.csv") # Input
    FICHEIRO_SAIDA_CSV = _os.path.join(_ROOT_DIR, "data", "omie", "precos-horarios.csv")     # Output

    try:
        # 1. Carregar dados locais
        df_omie = carregar_dados_locais(FICHEIRO_MIBEL_CSV)

        # 2. Determinar período (últimos 2 dias com dados)
        data_omie = df_omie['Data'].max()
        data_anterior = data_omie - pd.Timedelta(days=1)
        
        print(f"\nℹ️ Data de referência (última data no CSV): {data_omie.date()}")
        print(f"ℹ️ A processar os 2 últimos dias: {data_anterior.date()} e {data_omie.date()}")

        # 3. Filtrar para os 2 dias
        df_omie_filtrado = df_omie[df_omie['Data'].isin([data_omie, data_anterior])].copy()
        print(f"ℹ️ Registos filtrados para processamento: {len(df_omie_filtrado)}")
        
        # 4. Executar cálculos, passando o URL de configuração
        df_qh, df_h, constantes_dict = gerar_tabelas_tarifarias(
            df_omie_filtrado, 
            URL_CONFIG 
        )

        # 5. Aplicar filtro de datas (redundante mas seguro)
        df_qh['Dia_dt'] = pd.to_datetime(df_qh['Dia'])
        data_minima = pd.to_datetime(data_anterior) 
        df_qh = df_qh[df_qh['Dia_dt'] >= data_minima].copy()
        df_qh = df_qh.drop(columns=['Dia_dt'])
        
        df_h['Dia_dt'] = pd.to_datetime(df_h['Dia'])
        df_h = df_h[df_h['Dia_dt'] >= data_minima].copy()
        df_h = df_h.drop(columns=['Dia_dt'])
        print(f"✂️ Após filtro: {len(df_qh)} registos quarto-horários | {len(df_h)} registos horários")

        # 6. Exportar para CSV
        exportar_para_csv_compativel(df_qh, df_h, constantes_dict, FICHEIRO_SAIDA_CSV)
        
        print("\n" + "=" * 60)
        print("✅ PROCESSO CONCLUÍDO COM SUCESSO")
        print("=" * 60)

    except Exception as e:
        print("\n" + "=" * 60)
        print(f"❌ ERRO NO PROCESSO: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        raise

if __name__ == "__main__":
    main()