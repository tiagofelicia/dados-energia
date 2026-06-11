# --- Carregar as bibliotecas necessárias ---
import pandas as pd
import numpy as np
import requests
import openpyxl
import csv
from datetime import datetime
import io
import re
import os

print("✅ Bibliotecas carregadas para [Atualizar omie dados atuais]")

# ===================================================================
# ---- CONFIGURAÇÕES ----
# ===================================================================
# Caminhos ancorados no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
FICHEIRO_CSV = os.path.join(ROOT_DIR, "data", "omie", "omie_dados_atuais.csv")        # output final
FICHEIRO_MIBEL_CSV = os.path.join(ROOT_DIR, "data", "omie", "MIBEL_ano_atual_ACUM.csv") # input

print(f"ℹ️ Fonte de dados: '{FICHEIRO_MIBEL_CSV}'")
print("⚠️ Dados OMIE e futuros")
# ===================================================================

# ===================================================================
# FUNÇÃO PARA EXTRAIR E PROCESSAR FUTUROS (OMIP)
# ===================================================================
def extrair_dados_futuros_omip(product_code, data_relatorio_omip, ficheiro_omip_bytes):
    """
    Extrai e processa dados de futuros (FPB para PT, FTB para ES) a partir
    do conteúdo do ficheiro OMIPdaily.xlsx.
    """
    pais = "Portugal" if product_code == 'FPB' else "Espanha"
    print(f"\n⏳ A extrair futuros para {pais} ({product_code})...")
    
    try:
        # Usar o conteúdo do ficheiro já descarregado
        with io.BytesIO(ficheiro_omip_bytes) as ficheiro_memoria:
            df = pd.read_excel(ficheiro_memoria, sheet_name="OMIP Daily", header=None, skiprows=10, usecols=[1, 10], names=['Nome', 'Preco'])

        df = df.dropna(subset=['Nome'])
        df = df[df['Nome'].str.startswith(product_code)]

        conditions = [
            df['Nome'].str.contains(" D "), df['Nome'].str.contains(" Wk"),
            df['Nome'].str.contains(" M "), df['Nome'].str.contains(" Q"),
            df['Nome'].str.contains(" YR-")
        ]
        choices = ["Dia", "Semana", "Mês", "Trimestre", "Ano"]
        df['Classificacao'] = np.select(conditions, choices, default=None)
        df = df.dropna(subset=['Classificacao'])

        df['Preco'] = pd.to_numeric(df['Preco'], errors='coerce')
        df['AnoRaw'] = "20" + df['Nome'].str.extract(r'(\d{2})$')[0]

        datas = []
        for _, row in df.iterrows():
            nome, ano = row['Nome'], row['AnoRaw']
            try:
                if row['Classificacao'] == 'Dia':
                    match = re.search(r'(\d{2}[A-Za-z]{3})', nome)
                    datas.append(pd.to_datetime(match.group(1) + ano, format='%d%b%Y'))
                elif row['Classificacao'] == 'Semana':
                    week_num = int(re.search(r'Wk(\d+)', nome).group(1))
                    datas.append(datetime.fromisocalendar(int(ano), week_num, 1))
                elif row['Classificacao'] == 'Mês':
                    mes_str = re.search(r' M ([A-Za-z]{3})-', nome).group(1)
                    datas.append(pd.to_datetime(f'01-{mes_str}-{ano}', format='%d-%b-%Y'))
                elif row['Classificacao'] == 'Trimestre':
                    trimestre = int(re.search(r' Q(\d)', nome).group(1))
                    mes_inicio = (trimestre - 1) * 3 + 1
                    datas.append(pd.to_datetime(f'{ano}-{mes_inicio:02d}-01'))
                elif row['Classificacao'] == 'Ano':
                     datas.append(pd.to_datetime(f'{ano}-01-01'))
                else: datas.append(pd.NaT)
            except Exception: datas.append(pd.NaT)
        
        df['Data'] = pd.to_datetime(datas)
        df.dropna(subset=['Preco', 'Data'], inplace=True)

        meses_pt = {'Jan':'Janeiro', 'Feb':'Fevereiro', 'Mar':'Março', 'Apr':'Abril', 'May':'Maio', 'Jun':'Junho', 'Jul':'Julho', 'Aug':'Agosto', 'Sep':'Setembro', 'Oct':'Outubro', 'Nov':'Novembro', 'Dec':'Dezembro'}
        
        def formatar_descricao_futuro(row):
            cat, data, nome = row['Classificacao'], row['Data'], row['Nome']
            if cat == 'Dia': return data.strftime('%d/%m/%Y')
            if cat == 'Semana': return f"Semana {data.isocalendar().week}, {data.isocalendar().year}"
            if cat == 'Mês':
                mes_abbr = re.search(r' M ([A-Za-z]{3})-', nome).group(1)
                return f"{meses_pt.get(mes_abbr, mes_abbr)} {data.year}"
            if cat == 'Trimestre':
                trimestre = re.search(r' Q(\d)', nome).group(1)
                return f"{trimestre}º Trimestre {data.year}"
            if cat == 'Ano': return f"Ano {data.year}"
            return ""

        df['Descricao'] = df.apply(formatar_descricao_futuro, axis=1)
        df['Data_Atualizacao'] = data_relatorio_omip.strftime('%d/%m/%Y')
        df_final = df[['Nome', 'Descricao', 'Preco', 'Data_Atualizacao']].copy()
        df_final.rename(columns={'Nome': 'Contrato', 'Preco': 'Valor', 'Data_Atualizacao': 'Data de Atualizacao'}, inplace=True)
        
        print(f"   - ✅ {len(df_final)} contratos de futuros para {pais} processados.")
        return df_final

    except Exception as e:
        print(f"   - ❌ ERRO ao processar futuros para {pais}: {e}")
        return pd.DataFrame()
    

def run_analysis_process():
    """
    Função principal que pega no histórico ATUALIZADO e aplica
    os futuros, fusos horários e ciclos.
    """
    try:
        # ========================================================
        # PASSO 1: Extração de Dados de Futuros (OMIP)
        # ========================================================
        
        print("\n⏳ Passo 1: A extrair dados de futuros do ficheiro OMIPdaily.xlsx...")
        url_omip_excel = "https://www.omip.pt/sites/default/files/dados/eod/omipdaily.xlsx"
        resposta_http = requests.get(url_omip_excel, timeout=20)
        resposta_http.raise_for_status()

        ficheiro_omip_bytes = resposta_http.content 
        
        ficheiro_omip_memoria = io.BytesIO(ficheiro_omip_bytes)
        valor_celula_data = pd.read_excel(ficheiro_omip_memoria, sheet_name="OMIP Daily", header=None, skiprows=4, usecols="E", nrows=1).iloc[0, 0]
        data_relatorio_omip = pd.to_datetime(valor_celula_data, dayfirst=True)
        print(f"   - Data do relatório extraída: {data_relatorio_omip.date()}")

        ficheiro_omip_memoria.seek(0)
        df = pd.read_excel(ficheiro_omip_memoria, sheet_name="OMIP Daily", header=None, skiprows=10, usecols=[1, 10], names=['Nome', 'Preco'])
        
        # --- O parsing original para `dados_web` (futuros de PT) ---
        
        df = df.dropna(subset=['Nome'])
        df = df[df['Nome'].str.startswith('FPB')]
        conditions = [
            df['Nome'].str.contains(" D "), df['Nome'].str.contains(" Wk"),
            df['Nome'].str.contains(" M "), df['Nome'].str.contains(" Q"),
            df['Nome'].str.contains(" YR-")
        ]
        choices = ["Dia", "Semana", "Mês", "Trimestre", "Ano"]
        df['Classificacao'] = np.select(conditions, choices, default=None)
        df = df.dropna(subset=['Classificacao'])
        df['Preco'] = pd.to_numeric(df['Preco'], errors='coerce')
        df['AnoRaw'] = "20" + df['Nome'].str.extract(r'(\d{2})$')[0]
        
        datas = []
        for index, row in df.iterrows():
            nome, ano = row['Nome'], row['AnoRaw']
            try:
                if row['Classificacao'] == 'Dia':
                    match = re.search(r'(\d{2}[A-Za-z]{3})', nome)
                    datas.append(pd.to_datetime(match.group(1) + ano, format='%d%b%Y'))
                elif row['Classificacao'] == 'Semana':
                    week_num = int(re.search(r'Wk(\d+)', nome).group(1))
                    datas.append(datetime.fromisocalendar(int(ano), week_num, 1))
                elif row['Classificacao'] == 'Mês':
                    mes_str = re.search(r' M ([A-Za-z]{3})-', nome).group(1)
                    datas.append(pd.to_datetime(f'01-{mes_str}-{ano}', format='%d-%b-%Y'))
                elif row['Classificacao'] == 'Trimestre':
                    trimestre = int(re.search(r' Q(\d)', nome).group(1))
                    mes_inicio = (trimestre - 1) * 3 + 1
                    datas.append(pd.to_datetime(f'{ano}-{mes_inicio:02d}-01'))
                elif row['Classificacao'] == 'Ano':
                     datas.append(pd.to_datetime(f'{ano}-01-01'))
                else: datas.append(pd.NaT)
            except Exception: datas.append(pd.NaT)
        
        df['Data'] = pd.to_datetime(datas)
        dados_web = df.dropna(subset=['Preco', 'Data'])[['Data', 'Preco', 'Classificacao', 'Nome']]
        dados_web = dados_web.drop_duplicates(subset=['Nome'], keep='first').reset_index(drop=True)
        print("✅ Dados de futuros (OMIP) extraídos e processados.")
        
        # --- Extrair as tabelas finais de futuros ---
        futuros_pt = extrair_dados_futuros_omip('FPB', data_relatorio_omip, ficheiro_omip_bytes)
        futuros_es = extrair_dados_futuros_omip('FTB', data_relatorio_omip, ficheiro_omip_bytes)


        # ========================================================
        # PASSO 2: Leitura do Histórico OMIE
        # ========================================================

        print(f"\n⏳ Passo 2: A ler o histórico MIBEL/OMIE pré-atualizado de '{FICHEIRO_MIBEL_CSV}'...")
        
        try:
            # Ler o CSV que a Fase 1 criou
            dados_combinados_qh = pd.read_csv(FICHEIRO_MIBEL_CSV, parse_dates=['Data'])
            
            if dados_combinados_qh.empty:
                raise FileNotFoundError("O ficheiro histórico está vazio.")
                
            print(f"✅ {len(dados_combinados_qh)} registos históricos lidos com sucesso.")
            
        except FileNotFoundError:
            print(f"❌ ERRO CRÍTICO: O ficheiro '{FICHEIRO_MIBEL_CSV}' não foi encontrado.")
            print("   - Por favor, execute primeiro o script 'atualizar_mibel_ano_atual_ACUM.py'.")
            return
        except Exception as e:
            print(f"❌ ERRO CRÍTICO ao ler o ficheiro histórico: {e}")
            return

        # =================================================================
        # PASSO 3: Criar calendário e aplicar futuros
        # =================================================================

        print("\n⏳ Passo 3: A criar calendário e aplicar futuros...")
        
        # 3a. Criar calendário base
        calendario_es = pd.DataFrame({
            'Data': pd.date_range(start='2026-01-01', end='2027-12-31', freq='D')
        })
        calendario_es['Ano'] = calendario_es['Data'].dt.year
        calendario_es['Mes'] = calendario_es['Data'].dt.month
        calendario_es['Trimestre'] = calendario_es['Data'].dt.quarter
        calendario_es['Semana'] = calendario_es['Data'].dt.isocalendar().week

        # 3b. Preparar futuros por tipo
        print("   - A preparar futuros (diários, semanais, mensais, trimestrais)...")
        dados_web_dia = dados_web[dados_web['Classificacao'] == 'Dia'][['Data', 'Preco']].rename(columns={'Preco': 'Preco_Dia'}).drop_duplicates(subset=['Data'])
        dados_web_semana = dados_web[dados_web['Classificacao'] == 'Semana'].copy()
        dados_web_semana['Semana'] = dados_web_semana['Data'].dt.isocalendar().week
        dados_web_semana['Ano'] = dados_web_semana['Data'].dt.isocalendar().year
        dados_web_semana = dados_web_semana[['Ano', 'Semana', 'Preco']].rename(columns={'Preco': 'Preco_Semana'}).drop_duplicates(subset=['Ano', 'Semana'])
        dados_web_mes = dados_web[dados_web['Classificacao'] == 'Mês'][['Data', 'Preco']].rename(columns={'Preco': 'Preco_Mes'}).drop_duplicates(subset=['Data'])
        dados_web_trimestre = dados_web[dados_web['Classificacao'] == 'Trimestre'][['Data', 'Preco']].rename(columns={'Preco': 'Preco_Trimestre'}).drop_duplicates(subset=['Data'])

        # 3c. Juntar futuros ao calendário
        print("   - A fazer merge dos futuros...")
        calendario_es = pd.merge(calendario_es, dados_web_semana, on=['Ano', 'Semana'], how='left')
        calendario_es = pd.merge(calendario_es, dados_web_mes, on='Data', how='left')
        calendario_es = pd.merge(calendario_es, dados_web_trimestre, on='Data', how='left')

        # 3d. Aplicar fill (propagação) dentro de cada grupo
        print("   - A propagar futuros dentro dos períodos (fill)...")
        calendario_es['Preco_Semana'] = calendario_es.groupby(['Ano', 'Semana'])['Preco_Semana'].ffill().bfill()
        calendario_es['Preco_Mes'] = calendario_es.groupby(['Ano', 'Mes'])['Preco_Mes'].ffill().bfill()
        calendario_es['Preco_Trimestre'] = calendario_es.groupby(['Ano', 'Trimestre'])['Preco_Trimestre'].ffill().bfill()

        # 3e. Juntar dados históricos reais (agregados por dia)
        print("   - A juntar dados históricos reais...")
        dados_historicos_diarios = dados_combinados_qh.groupby('Data')['Preco_PT'].mean().rename('Preco_Diario_Real')
        calendario_es = pd.merge(calendario_es, dados_historicos_diarios, left_on='Data', right_index=True, how='left')

        # 3f. Juntar futuros diários (último, para terem prioridade)
        calendario_es = pd.merge(calendario_es, dados_web_dia, left_on='Data', right_on='Data', how='left')

        # 3g. Aplicar a hierarquia de preços
        print("   - A aplicar hierarquia de preços...")
        calendario_es['Preco_Final_Diario'] = (
            calendario_es['Preco_Diario_Real']
            .fillna(calendario_es['Preco_Dia'])
            .fillna(calendario_es['Preco_Semana'])
            .fillna(calendario_es['Preco_Mes'])
            .fillna(calendario_es['Preco_Trimestre'])
        )
        print("✅ Preços diários (reais e projetados) calculados.")

        # 3h. Criar grelha quarto-horária (para datas futuras)
        print("   - A criar grelha quarto-horária futura...")
        
        def num_quartos_dia(data_obj):
            """
            Calcula número de quartos horários considerando DST.
            Usa a diferença entre 'Meia noite de hoje' e 'Meia noite de amanhã'
            para garantir que apanha as 23h ou 25h nos dias de mudança de hora.
            """
            tz_es = 'Europe/Madrid'
            
            # Garantir que estamos a usar apenas a data (sem horas misturadas)
            dia_atual = data_obj.date() if hasattr(data_obj, 'date') else data_obj
            dia_seguinte = dia_atual + pd.Timedelta(days=1)
            
            # Criar Timestamps "localizados" para as duas datas
            dt0 = pd.Timestamp(f"{dia_atual} 00:00:00", tz=tz_es)
            dt_next = pd.Timestamp(f"{dia_seguinte} 00:00:00", tz=tz_es)
            
            # A diferença exata em horas (pode ser 23, 24 ou 25)
            horas = (dt_next - dt0).total_seconds() / 3600
            
            return int(round(horas * 4)) # Multiplica por 4 para ter quartos de hora

        ultima_data_historica = dados_combinados_qh['Data'].max()
        
        # Até 2027-01-01
        datas_futuras = pd.date_range(start=ultima_data_historica + pd.Timedelta(days=1), end='2027-01-01', freq='D')

        futuro_qh = []
        for data in datas_futuras:
            n_quartos = num_quartos_dia(data)
            for hora in range(1, n_quartos + 1):
                futuro_qh.append({'Data': data, 'Hora': hora})
        
        if futuro_qh:
            futuro_qh = pd.DataFrame(futuro_qh)
        else:
            futuro_qh = pd.DataFrame(columns=['Data', 'Hora'])

        # Combinar histórico + futuros
        dados_finais_es = pd.concat([dados_combinados_qh, futuro_qh], ignore_index=True)
        
        # Juntar o preço diário calculado
        dados_finais_es = dados_finais_es.merge(
            calendario_es[['Data', 'Preco_Final_Diario']], 
            on='Data', 
            how='left'
        )

        # Preencher APENAS o Preco_PT (futuro)
        dados_finais_es['Preco_PT'] = dados_finais_es['Preco_PT'].fillna(dados_finais_es['Preco_Final_Diario'])
        # Preco_ES fica com NaN para o futuro
        
        dados_finais_es = dados_finais_es.sort_values(['Data', 'Hora']).reset_index(drop=True)
        print("✅ Estrutura ES (passado e futuro) criada com número correto de quartos-horários.")

        # ============================================================
        # PASSO 4: Conversão para hora de Portugal
        # ============================================================

        print("\n⏳ Passo 4: A converter para hora de Portugal...")

        # Gerar datetime em hora de Espanha
        def gerar_datetime_es(row):
            """Gera timestamp correto considerando DST"""
            data = row['Data']
            hora = row['Hora']
            inicio_dia = pd.Timestamp(f"{data} 00:00:00", tz='Europe/Madrid')
            return inicio_dia + pd.Timedelta(minutes=15 * (hora - 1))

        dados_finais_es['datetime_es'] = dados_finais_es.apply(gerar_datetime_es, axis=1)
        
        # Esta é a conversão chave que lida corretamente com a mudança de hora
        dados_finais_es['datetime_pt'] = dados_finais_es['datetime_es'].dt.tz_convert('Europe/Lisbon')
        
        # Criar as colunas de Data e Hora de Portugal a partir do timestamp correto
        dados_finais_pt = dados_finais_es.sort_values('datetime_pt').copy()
        dados_finais_pt['Data'] = dados_finais_pt['datetime_pt'].dt.date
        dados_finais_pt['Hora'] = dados_finais_pt.groupby('Data').cumcount() + 1

        # Selecionar apenas 2026 e 2027
        dados_finais_pt = dados_finais_pt[dados_finais_pt['datetime_pt'].dt.year.isin([2026, 2027])].copy()

        # Selecionar as colunas finais, MANTENDO o datetime_pt para o passo seguinte
        dados_finais_pt = dados_finais_pt[['Data', 'Hora', 'Preco_PT', 'Preco_ES', 'datetime_pt']].copy()
        
        # Remover quaisquer linhas onde o Preco_PT não pôde ser calculado (nem histórico, nem futuro)
        dados_finais_pt = dados_finais_pt.dropna(subset=['Preco_PT']).reset_index(drop=True)
        
        print(f"✅ {len(dados_finais_pt)} registos finais preparados em hora de Portugal.")

        # =======================================================================
        # PASSO 5: Adicionar Ciclos Horários e Exportar para CSV
        # =======================================================================

        print(f"\n⏳ Passo 5: A adicionar ciclos horários e a exportar dados para CSV...")

        # --- 5a. Adicionar Colunas de Ciclos Horários ---
        df_com_ciclos = dados_finais_pt.copy()
        
        # (Funções de cálculo de ciclos)
        def last_sunday(year, month):
            d = datetime(year, month, 1) + pd.DateOffset(months=1) - pd.DateOffset(days=1)
            offset = (d.weekday() - 6) % 7
            return (d - pd.DateOffset(days=offset)).date()

        def is_summer_time(dt):
            year = dt.year
            start_summer = last_sunday(year, 3)
            end_summer = last_sunday(year, 10)
            return start_summer <= dt < end_summer

        def calcular_ciclos(row):
            ts = row['datetime_pt']
            time = ts.time()
            weekday = ts.weekday()
            is_weekday, is_saturday, is_sunday = weekday <= 4, weekday == 5, weekday == 6
            is_summer = is_summer_time(ts.date())
            simples, bd, bs, td, ts_period = 'S', '', '', '', ''
            if time >= datetime.strptime("22:00", "%H:%M").time() or time < datetime.strptime("08:00", "%H:%M").time(): bd = 'V'
            else: bd = 'F'
            if time >= datetime.strptime("22:00", "%H:%M").time() or time < datetime.strptime("08:00", "%H:%M").time(): td = 'V'
            else:
                if is_summer:
                    if (time >= datetime.strptime("10:30", "%H:%M").time() and time < datetime.strptime("13:00", "%H:%M").time()) or \
                       (time >= datetime.strptime("19:30", "%H:%M").time() and time < datetime.strptime("21:00", "%H:%M").time()): td = 'P'
                    else: td = 'C'
                else:
                    if (time >= datetime.strptime("09:00", "%H:%M").time() and time < datetime.strptime("10:30", "%H:%M").time()) or \
                       (time >= datetime.strptime("18:00", "%H:%M").time() and time < datetime.strptime("20:30", "%H:%M").time()): td = 'P'
                    else: td = 'C'
            if is_sunday:
                bs, ts_period = 'V', 'V'
            elif is_saturday:
                if is_summer:
                    if (time >= datetime.strptime("09:00", "%H:%M").time() and time < datetime.strptime("14:00", "%H:%M").time()) or \
                       (time >= datetime.strptime("20:00", "%H:%M").time() and time < datetime.strptime("22:00", "%H:%M").time()): bs, ts_period = 'F', 'C'
                    else: bs, ts_period = 'V', 'V'
                else:
                    if (time >= datetime.strptime("09:30", "%H:%M").time() and time < datetime.strptime("13:00", "%H:%M").time()) or \
                       (time >= datetime.strptime("18:30", "%H:%M").time() and time < datetime.strptime("22:00", "%H:%M").time()): bs, ts_period = 'F', 'C'
                    else: bs, ts_period = 'V', 'V'
            elif is_weekday:
                if time < datetime.strptime("07:00", "%H:%M").time(): bs, ts_period = 'V', 'V'
                else:
                    bs = 'F'
                    if is_summer:
                        if time >= datetime.strptime("09:15", "%H:%M").time() and time < datetime.strptime("12:15", "%H:%M").time(): ts_period = 'P'
                        else: ts_period = 'C'
                    else:
                        if (time >= datetime.strptime("09:30", "%H:%M").time() and time < datetime.strptime("12:00", "%H:%M").time()) or \
                           (time >= datetime.strptime("18:30", "%H:%M").time() and time < datetime.strptime("21:00", "%H:%M").time()): ts_period = 'P'
                        else: ts_period = 'C'
            return simples, bd, bs, td, ts_period

        df_com_ciclos[['Simples', 'BD', 'BS', 'TD', 'TS']] = df_com_ciclos.apply(
            calcular_ciclos, axis=1, result_type='expand'
        )

        # --- 5b. Preparar a Tabela Principal ---
        df_principal = df_com_ciclos.copy()
        df_principal['dia'] = df_principal['datetime_pt'].dt.strftime('%d/%m/%Y')
        start_time = df_principal['datetime_pt']
        end_time = start_time + pd.Timedelta(minutes=15)
        df_principal['hora'] = end_time.dt.strftime('%H:%M')
        df_principal.loc[df_principal['hora'] == '00:00', 'hora'] = '23:59'
        df_principal['intervalo'] = '[' + start_time.dt.strftime('%H:%M') + '-' + end_time.dt.strftime('%H:%M') + '['
        df_principal.rename(columns={'Preco_PT': 'preco_pt', 'Preco_ES': 'preco_es'}, inplace=True)
        colunas_finais = ['dia', 'hora', 'intervalo', 'Simples', 'BD', 'BS', 'TD', 'TS', 'preco_pt', 'preco_es']
        df_principal = df_principal[colunas_finais]

        # --- 5c. Preparar Tabela de Atualização ---
        # A data OMIE é agora a data máxima do nosso ficheiro histórico
        ultima_data_omie = pd.to_datetime(dados_combinados_qh['Data']).max() 
        dados_atualizacao = {
            'chave': ['Data_Valores_OMIE', 'Data_Valores_OMIP'],
            'valor': [
                ultima_data_omie.strftime('%d/%m/%Y'), 
                data_relatorio_omip.strftime('%d/%m/%Y')
            ]
        }
        df_atualizacao = pd.DataFrame(dados_atualizacao)

        # --- 5d. Calcular variações face à sessão anterior ---
        def carregar_futuros_anteriores(ficheiro, tabela_tag):
            """Lê os futuros da sessão anterior do CSV existente."""
            try:
                with open(ficheiro, 'r', encoding='utf-8-sig') as f:
                    conteudo = f.read()
                partes = conteudo.split('\nTABELA_')
                raw = next((p for p in partes if p.startswith(tabela_tag)), None)
                if not raw:
                    return {}
                linhas = raw.strip().split('\n')
                linhas.pop(0)  # tag
                if not linhas:
                    return {}
                headers = next(csv.reader([linhas.pop(0)]))
                headers = [h.strip() for h in headers]
                resultado = {}
                for linha in linhas:
                    if not linha.strip():
                        continue
                    valores = next(csv.reader([linha]))
                    row = {headers[i]: valores[i].strip() for i in range(min(len(headers), len(valores)))}
                    contrato = row.get('Contrato', '')
                    valor_str = row.get('Valor', '')
                    if contrato and valor_str:
                        try:
                            resultado[contrato] = float(valor_str)
                        except ValueError:
                            pass
                return resultado
            except FileNotFoundError:
                return {}
            except Exception as e:
                print(f"   ⚠️ Aviso: não foi possível ler futuros anteriores: {e}")
                return {}

        def adicionar_variacao(df_novo, anteriores):
            """Adiciona coluna Variacao comparando com sessão anterior (formato: direção:diff)."""
            def calcular(row):
                prev = anteriores.get(row['Contrato'])
                if prev is None:
                    return 'new:0'
                diff = round(row['Valor'] - prev, 4)
                if diff > 0:
                    return f'up:+{diff}'
                elif diff < 0:
                    return f'down:{diff}'
                else:
                    return 'equal:0'
            df_novo = df_novo.copy()
            df_novo['Variacao'] = df_novo.apply(calcular, axis=1)
            return df_novo

        print("\n⏳ A calcular variações face à sessão anterior...")

        # Ler a data OMIP do CSV existente antes de o sobrescrever
        def ler_data_omip_anterior(ficheiro):
            try:
                with open(ficheiro, 'r', encoding='utf-8-sig') as f:
                    conteudo = f.read()
                partes = conteudo.split('\nTABELA_')
                raw = next((p for p in partes if p.startswith('ATUALIZACOES')), None)
                if not raw:
                    return None
                linhas = raw.strip().split('\n')
                linhas.pop(0)  # tag
                if not linhas:
                    return None
                headers = next(csv.reader([linhas.pop(0)]))
                headers = [h.strip() for h in headers]
                for linha in linhas:
                    if not linha.strip():
                        continue
                    valores = next(csv.reader([linha]))
                    row = {headers[i]: valores[i].strip() for i in range(min(len(headers), len(valores)))}
                    if row.get('chave') == 'Data_Valores_OMIP':
                        return row.get('valor')
                return None
            except FileNotFoundError:
                return None
            except Exception as e:
                print(f"   ⚠️ Aviso: não foi possível ler data OMIP anterior: {e}")
                return None

        data_omip_anterior = ler_data_omip_anterior(FICHEIRO_CSV)
        data_omip_nova = data_relatorio_omip.strftime('%d/%m/%Y')

        if data_omip_anterior and data_omip_anterior == data_omip_nova:
            print(f"   ℹ️ Data OMIP inalterada ({data_omip_nova}) — variação preservada do CSV anterior.")
            # Preservar variação anterior em vez de recalcular
            def preservar_variacao(df_novo, anteriores_variacao):
                df_novo = df_novo.copy()
                df_novo['Variacao'] = df_novo['Contrato'].map(anteriores_variacao).fillna('new:0')
                return df_novo

            def carregar_variacao_anterior(ficheiro, tabela_tag):
                try:
                    with open(ficheiro, 'r', encoding='utf-8-sig') as f:
                        conteudo = f.read()
                    partes = conteudo.split('\nTABELA_')
                    raw = next((p for p in partes if p.startswith(tabela_tag)), None)
                    if not raw:
                        return {}
                    linhas = raw.strip().split('\n')
                    linhas.pop(0)
                    if not linhas:
                        return {}
                    headers = next(csv.reader([linhas.pop(0)]))
                    headers = [h.strip() for h in headers]
                    resultado = {}
                    for linha in linhas:
                        if not linha.strip():
                            continue
                        valores = next(csv.reader([linha]))
                        row = {headers[i]: valores[i].strip() for i in range(min(len(headers), len(valores)))}
                        contrato = row.get('Contrato', '')
                        variacao = row.get('Variacao', 'new')
                        if contrato:
                            resultado[contrato] = variacao
                    return resultado
                except Exception:
                    return {}

            var_ant_pt = carregar_variacao_anterior(FICHEIRO_CSV, 'FUTUROS_PT')
            var_ant_es = carregar_variacao_anterior(FICHEIRO_CSV, 'FUTUROS_ES')
            if not futuros_pt.empty:
                futuros_pt = preservar_variacao(futuros_pt, var_ant_pt)
            if not futuros_es.empty:
                futuros_es = preservar_variacao(futuros_es, var_ant_es)
        else:
            if data_omip_anterior:
                print(f"   ✅ Data OMIP avançou ({data_omip_anterior} → {data_omip_nova}) — a calcular variações.")
            else:
                print(f"   ℹ️ Sem CSV anterior — a marcar todos os contratos como novos.")
            anteriores_pt = carregar_futuros_anteriores(FICHEIRO_CSV, 'FUTUROS_PT')
            anteriores_es = carregar_futuros_anteriores(FICHEIRO_CSV, 'FUTUROS_ES')
            print(f"   - {len(anteriores_pt)} contratos PT anteriores encontrados.")
            print(f"   - {len(anteriores_es)} contratos ES anteriores encontrados.")
            if not futuros_pt.empty:
                futuros_pt = adicionar_variacao(futuros_pt, anteriores_pt)
            if not futuros_es.empty:
                futuros_es = adicionar_variacao(futuros_es, anteriores_es)

        # --- 5e. Escrever as tabelas no ficheiro CSV final ---
        try:
            with open(FICHEIRO_CSV, 'w', encoding='utf-8-sig', newline='') as f:
                df_principal.to_csv(f, index=False, decimal='.', float_format='%.2f')
                f.write("\nTABELA_ATUALIZACOES\n")
                df_atualizacao.to_csv(f, index=False, header=True)
                if not futuros_pt.empty:
                    f.write("\nTABELA_FUTUROS_PT\n")
                    futuros_pt.to_csv(f, index=False, decimal='.', float_format='%.2f')
                if not futuros_es.empty:
                    f.write("\nTABELA_FUTUROS_ES\n")
                    futuros_es.to_csv(f, index=False, decimal='.', float_format='%.2f')
            
            print(f"✅ Ficheiro CSV final '{FICHEIRO_CSV}' criado com sucesso.")
        except Exception as e:
            print(f"❌ Erro ao escrever o ficheiro CSV final: {e}")
            raise

    except Exception as e:
        import traceback
        print(f"❌ Ocorreu um erro inesperado no processo: {e}")
        traceback.print_exc()

# PONTO DE ENTRADA DO SCRIPT
if __name__ == "__main__":
    run_analysis_process()