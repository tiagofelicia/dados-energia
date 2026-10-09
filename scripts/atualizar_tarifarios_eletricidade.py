# --- Carregar as bibliotecas necessárias ---
import pandas as pd
import numpy as np
import requests
import openpyxl
from datetime import datetime
import io
import re
import os
import json
import hashlib

# ERC quarto-horário (coluna M). Fica na mesma pasta: quando o script corre, a
# pasta dele está no sys.path.
import erc_previsao
# Validação da BD antes de exportar os CSVs (Passo 6)
import validar_tarifarios_eletricidade

print("✅ Bibliotecas carregadas")

# ===================================================================
# ---- CONFIGURAÇÕES ----
# ===================================================================
# Caminhos ancorados no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_SIMULADOR = os.path.join(ROOT_DIR, "data", "simuladores", "simulador-tarifarios-eletricidade")

FICHEIRO_EXCEL = os.path.join(PASTA_SIMULADOR, "tarifarios_eletricidade_Tiago_Felicia.xlsx")
ABA_EXCEL = "OMIE_PERDAS_CICLOS"
COLUNA_PARA_ESCREVER = 11 # Coluna K
# ERC da REN em €/MWh, a seguir às Perdas (L): o real até ao último dia publicado
# e uma previsão no resto (erc_previsao.py). Os simuladores usam-no quarto de hora
# a quarto de hora nos tarifários que o faturam assim, e a média do período nos
# que faturam a média (Luzigas).
COLUNA_ERC = 13 # Coluna M
PASTA_ERC_ISP = os.path.join(ROOT_DIR, "data", "erc", "isp")
# Intermédio da Fase 1 (atualizar_mibel_ano_atual_ACUM.py) — partilhado com o pipeline do site
FICHEIRO_MIBEL_CSV = os.path.join(ROOT_DIR, "data", "omie", "MIBEL_ano_atual_ACUM.csv")

# CSVs individuais (espelham as abas do Excel para o simulador)
PASTA_CSV = os.path.join(PASTA_SIMULADOR, "csv")
ABAS_PARA_CSV = ["Constantes", "Tarifarios_fixos", "Indexados", "OMIE_PERDAS_CICLOS"]

print(f"ℹ️ Fonte de dados: '{FICHEIRO_MIBEL_CSV}'")
print("⚠️ Dados OMIE e futuros")
# ===================================================================

def escrever_coluna_erc(sheet):
    """
    Escreve o ERC (€/MWh) de cada linha da folha OMIE_PERDAS_CICLOS na coluna M:
    o real onde a REN já publicou e a previsão no resto (erc_previsao.py).

    Cada linha é localizada pelo instante em que começa: o dia (coluna A, texto
    'MM/DD/AAAA') mais a posição da linha dentro do dia, em hora de Portugal — o
    mesmo alinhamento que o Passo 5 usa para o OMIE. Assim os dias de 92 e 100
    quartos de hora ficam certos sem depender das etiquetas da coluna Hora.

    A coluna é sempre reescrita de ponta a ponta: sem dados da REN fica vazia e
    os simuladores usam as constantes da BD, nunca valores de uma corrida antiga.

    Devolve o último dia publicado pela REN (daí em diante é previsão), ou None.
    """
    print(f"   - A escrever o ERC na Coluna {COLUNA_ERC} (M)...")
    linhas, dias = [], []
    for (celula,) in sheet.iter_rows(min_row=2, max_col=1):
        valor = celula.value
        if valor is None or str(valor).strip() == "":
            continue
        try:
            dia = (pd.Timestamp(valor) if isinstance(valor, datetime)
                   else pd.to_datetime(str(valor).strip(), format="%m/%d/%Y"))
        except (ValueError, TypeError):
            continue
        linhas.append(celula.row)
        dias.append(dia.normalize())

    tabela = pd.DataFrame({"linha": linhas, "dia": dias})
    posicao = tabela.groupby("dia").cumcount()
    inicio_utc = (tabela["dia"].dt.tz_localize("Europe/Lisbon")
                  + pd.to_timedelta(15 * posicao, unit="m")).dt.tz_convert("UTC")

    dados = erc_previsao.carregar(PASTA_ERC_ISP)
    if dados is not None:
        print(f"     ERC: dados da REN até {dados['ultimo'].strftime('%d/%m/%Y')} "
              f"({dados['atraso']} dias de atraso)")
        if dados["desatualizado"]:
            print(f"     ⚠️ Mais de {erc_previsao.ATRASO_MAX_DIAS} dias de atraso: só os valores reais, "
                  f"sem previsões (os simuladores usam as constantes nesses dias).")
    valores, contagem = erc_previsao.erc_instantes(dados, inicio_utc)

    sheet.cell(row=1, column=COLUNA_ERC, value="ERC")
    for r in range(2, sheet.max_row + 1):
        sheet.cell(row=r, column=COLUNA_ERC, value=None)
    for linha, valor in zip(tabela["linha"], valores):
        if not np.isnan(valor):
            sheet.cell(row=linha, column=COLUNA_ERC, value=round(float(valor), 2))
    print(f"     ERC escrito: {contagem['real']} quartos de hora reais, {contagem['curto']} previstos "
          f"a curto prazo, {contagem['longo']} a longo prazo, {contagem['sem']} vazios.")
    return dados["ultimo"] if dados is not None else None


def sinalizar_bd_bloqueada(motivo):
    """
    Os CSVs do simulador não foram publicados: ficam os da última versão boa.

    Não é uma excepção de propósito. As fases 2B (dados do site) correm na mesma
    e a corrida acaba verde — o gerar_hoje.yml só dispara se ela acabar verde.
    O workflow lê o output bd_bloqueada para avisar e saltar o deploy no HF; o
    alarme (email) é do validar_bd_eletricidade.yml.
    """
    print(f"   ❌ BD NÃO PUBLICADA: {motivo}")
    print("      Os CSVs e o manifest anteriores ficam como estão.")
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a", encoding="utf-8") as f:
            f.write("bd_bloqueada=true\n")


def validar_e_exportar_csvs():
    """
    Valida as quatro abas e, só se não houver erros, exporta-as como CSVs + manifest.

    A exportação é tudo ou nada: os ficheiros novos são escritos ao lado (.tmp) e
    só substituem os publicados depois de os quatro estarem escritos, com o
    manifest em último lugar. Antes, uma aba que falhasse ficava de fora do
    manifest e as outras eram publicadas na mesma.
    """
    try:
        abas = validar_tarifarios_eletricidade.ler_abas(FICHEIRO_EXCEL)
        relatorio = validar_tarifarios_eletricidade.validar(abas)
    except Exception as e:
        # Um xlsx ilegível ou um bug do validador não pode parar as fases 2B:
        # não se publica (na dúvida, fica a última versão boa) e segue-se.
        import traceback
        traceback.print_exc()
        sinalizar_bd_bloqueada(f"a validação não correu: {e}")
        return None
    print("   " + relatorio.texto().replace("\n", "\n   "))
    relatorio.escrever_resumo_github("Validação da BD do simulador de eletricidade (Fase 2A)")
    if relatorio.erros:
        sinalizar_bd_bloqueada(f"{len(relatorio.erros)} erro(s) na validação")
        return None

    os.makedirs(PASTA_CSV, exist_ok=True)
    temporarios = []
    manifest = {}
    try:
        for aba in ABAS_PARA_CSV:
            tmp = os.path.join(PASTA_CSV, f"{aba}.csv.tmp")
            temporarios.append(tmp)
            abas[aba].to_csv(tmp, index=False, encoding='utf-8-sig')
            # Hash MD5 do conteúdo para o manifest (o simulador compara-o com o da cache)
            with open(tmp, 'rb') as f:
                manifest[aba] = hashlib.md5(f.read()).hexdigest()[:8]
            print(f"   ✅ {aba}.csv ({len(abas[aba])} registos) [{manifest[aba]}]")
        tmp_manifest = os.path.join(PASTA_CSV, "manifest.json.tmp")
        temporarios.append(tmp_manifest)
        with open(tmp_manifest, 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
    except Exception as e:
        for tmp in temporarios:
            if os.path.exists(tmp):
                os.remove(tmp)
        sinalizar_bd_bloqueada(f"falha ao exportar: {e}")
        return None

    for aba in ABAS_PARA_CSV:
        os.replace(os.path.join(PASTA_CSV, f"{aba}.csv.tmp"), os.path.join(PASTA_CSV, f"{aba}.csv"))
    os.replace(tmp_manifest, os.path.join(PASTA_CSV, "manifest.json"))
    print(f"   ✅ manifest.json gerado: {manifest}")
    print("✅ Exportação de CSVs concluída.")
    return manifest


def run_update_process():
    """
    Função principal que encapsula todo o processo de ETL.
    """
    try:
        # ========================================================
        # PASSO 1: Extração de Dados de Futuros (OMIP)
        # ========================================================
        
        print("\n⏳ Passo 1: A extrair dados de futuros do ficheiro OMIPdaily.xlsx...")
        url_omip_excel = "https://www.omip.pt/sites/default/files/dados/eod/omipdaily.xlsx"
        resposta_http = requests.get(url_omip_excel, timeout=20)
        resposta_http.raise_for_status()

        ficheiro_omip_memoria = io.BytesIO(resposta_http.content)
        valor_celula_data = pd.read_excel(ficheiro_omip_memoria, sheet_name="OMIP Daily", header=None, skiprows=4, usecols="E", nrows=1).iloc[0, 0]
        data_relatorio_omip = pd.to_datetime(valor_celula_data, dayfirst=True)
        print(f"   - Data do relatório OMIP extraída: {data_relatorio_omip.date()}")

        ficheiro_omip_memoria.seek(0)
        df = pd.read_excel(ficheiro_omip_memoria, sheet_name="OMIP Daily", header=None, skiprows=10, usecols=[1, 10], names=['Nome', 'Preco'])

        df = df.dropna(subset=['Nome'])
        df = df[df['Nome'].str.startswith('FPB')]

        # (Parsing dos futuros...)
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
        print("✅ Dados de futuros extraídos e processados.")


        # ========================================================
        # PASSO 2: Leitura dos Dados
        # ========================================================
        
        print(f"\n⏳ Passo 2: A ler dados históricos do '{FICHEIRO_MIBEL_CSV}'...")
        try:
            dados_combinados_qh = pd.read_csv(FICHEIRO_MIBEL_CSV, parse_dates=['Data'])
            
            # Este script usa internamente a coluna 'Preco' para o preço de PT
            dados_combinados_qh = dados_combinados_qh.rename(columns={'Preco_PT': 'Preco'})
            
            # Selecionar apenas as colunas que o PASSO 3 precisa
            dados_combinados_qh = dados_combinados_qh[['Data', 'Hora', 'Preco']]
            dados_combinados_qh = dados_combinados_qh.dropna(subset=['Data', 'Hora']) # Garantir que não há lixo
            
            print(f"✅ {len(dados_combinados_qh)} registos históricos lidos com sucesso.")
            
        # 'raise' e não 'return': um erro crítico tem de chegar ao handler de
        # topo e fazer o script sair com código != 0. Com 'return' o script
        # terminava normalmente e o step do GitHub Actions ficava verde.
        except FileNotFoundError:
            print(f"❌ ERRO CRÍTICO: O ficheiro '{FICHEIRO_MIBEL_CSV}' não foi encontrado.")
            print("   - Por favor, execute primeiro o script 'atualizar_mibel_ano_atual_ACUM.py'.")
            raise
        except Exception as e:
            print(f"❌ ERRO CRÍTICO ao ler o ficheiro histórico: {e}")
            raise


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
        dados_web_semana['Ano'] = dados_web_semana['Data'].dt.year
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

        # 3e. Juntar dados históricos reais
        print("   - A juntar dados históricos reais...")
        dados_historicos_diarios = dados_combinados_qh.groupby('Data')['Preco'].mean().rename('Preco_Diario_Real')
        calendario_es = pd.merge(calendario_es, dados_historicos_diarios, left_on='Data', right_index=True, how='left')

        # 3f. Juntar futuros diários (último)
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
            n_quartos = num_quartos_dia(data.date())
            for hora in range(1, n_quartos + 1):
                futuro_qh.append({'Data': data, 'Hora': hora})
        
        if futuro_qh:
            futuro_qh = pd.DataFrame(futuro_qh)
        else:
            futuro_qh = pd.DataFrame(columns=['Data', 'Hora'])

        # Combinar histórico + futuros
        dados_finais_es = pd.concat([dados_combinados_qh, futuro_qh], ignore_index=True)
        dados_finais_es = dados_finais_es.merge(
            calendario_es[['Data', 'Preco_Final_Diario']], 
            on='Data', 
            how='left'
        )

        # Manter histórico real; preencher apenas futuros
        dados_finais_es['Preco'] = dados_finais_es['Preco'].fillna(dados_finais_es['Preco_Final_Diario'])
        dados_finais_es = dados_finais_es.sort_values(['Data', 'Hora']).reset_index(drop=True)
        print("✅ Estrutura ES criada com número correto de quartos-horários.")

                
        # ============================================================
        # PASSO 4: Conversão para hora de Portugal
        # ============================================================

        print("\n⏳ Passo 4: A converter para hora de Portugal...")

        def gerar_datetime_es(row):
            """Gera timestamp correto considerando DST"""
            data = row['Data']
            hora = row['Hora']
            inicio_dia = pd.Timestamp(f"{data} 00:00:00", tz='Europe/Madrid')
            return inicio_dia + pd.Timedelta(minutes=15 * (hora - 1))

        dados_finais_es['datetime_es'] = dados_finais_es.apply(gerar_datetime_es, axis=1)
        dados_finais_es['datetime_pt'] = dados_finais_es['datetime_es'].dt.tz_convert('Europe/Lisbon')
        dados_finais_es['Data_PT'] = dados_finais_es['datetime_pt'].dt.date

        # Renumerar horas em hora de Portugal
        dados_finais_pt = dados_finais_es.sort_values('datetime_pt').copy()
        dados_finais_pt['Hora_PT'] = dados_finais_pt.groupby('Data_PT').cumcount() + 1

        # Selecionar apenas 2026 e 2027
        dados_finais_pt = dados_finais_pt[dados_finais_pt['datetime_pt'].dt.year.isin([2026, 2027])].copy()
        dados_finais_pt = dados_finais_pt[['Data_PT', 'Hora_PT', 'Preco']].rename(
            columns={'Data_PT': 'Data', 'Hora_PT': 'Hora'}
        )
        dados_finais_pt = dados_finais_pt.dropna(subset=['Preco']).reset_index(drop=True)

        print(f"✅ {len(dados_finais_pt)} registos finais (em PT) preparados.")

        # ============================================================
        # PASSO 5: Atualização do ficheiro Excel
        # ============================================================

        print(f"\n⏳ Passo 5: A preparar dados para o ficheiro '{FICHEIRO_EXCEL}'...")

        # 1. Ler a pauta de tempo 'master' do Excel (Colunas A e B)
        print(f"   - A ler a pauta de tempo da aba '{ABA_EXCEL}' para alinhamento...")
        df_pauta_excel = pd.read_excel(
            FICHEIRO_EXCEL,
            sheet_name=ABA_EXCEL,
            usecols=['Data', 'Hora'] 
        )
        df_pauta_excel.dropna(subset=['Data', 'Hora'], inplace=True)
        # Preservar a ordem original do Excel (o índice 0-based)
        df_pauta_excel = df_pauta_excel.reset_index() 
        
        # 2. Preparar a pauta do Excel para o merge
        df_pauta_excel['Data'] = pd.to_datetime(df_pauta_excel['Data']).dt.date
        df_pauta_excel['Hora'] = df_pauta_excel.groupby('Data').cumcount() + 1
        
        # 3. Preparar os nossos dados calculados (do Passo 4)
        df_dados_pt_merge = dados_finais_pt.copy()
        df_dados_pt_merge['Data'] = pd.to_datetime(df_dados_pt_merge['Data']).dt.date
        df_dados_pt_merge['Hora'] = df_dados_pt_merge['Hora'].astype(int)

        # 4. Fazer o MERGE para alinhar os preços à pauta do Excel
        print("   - A alinhar preços calculados com a pauta do Excel...")
        df_final_excel = pd.merge(
            df_pauta_excel,
            df_dados_pt_merge[['Data', 'Hora', 'Preco']], 
            on=['Data', 'Hora'],
            how='left' # Manter todas as linhas da pauta
        )
        
        # 5. Ordenar pela ordem original do Excel
        df_final_excel = df_final_excel.sort_values('index').reset_index(drop=True)
        
        # 6. FILTRAR apenas os dados que TÊM preço (ignorar os NaN)
        #    Manter o 'index' original do Excel e o 'Preco'
        dados_para_escrever = df_final_excel.dropna(subset=['Preco'])[['index', 'Preco']]
        
        print(f"   - {len(dados_para_escrever)} preços (2026) alinhados e prontos a escrever.")

        # 7. Escrever no ficheiro Excel (de forma seletiva)
        print(f"   - A carregar '{FICHEIRO_EXCEL}' para escrita...")
        wb = openpyxl.load_workbook(FICHEIRO_EXCEL)
        sheet = wb[ABA_EXCEL]
        
        print(f"   - A escrever {len(dados_para_escrever)} preços na Coluna {COLUNA_PARA_ESCREVER} (K)...")
        
        # Iterar APENAS sobre as linhas que TÊM dados
        for _, row in dados_para_escrever.iterrows():
            # Usar o 'index' original para encontrar a linha correta no Excel
            excel_row_index = int(row['index']) + 2  # +1 (0-based to 1-based) +1 (skip header)
            preco = row['Preco']
            sheet.cell(row=excel_row_index, column=COLUNA_PARA_ESCREVER, value=preco)

        # 7b. ERC da REN na coluna M (real + previsão)
        ultimo_dia_erc = escrever_coluna_erc(sheet)

        # ===================================================================
            
        # 8. Atualizar as datas de OMIE/OMIP
        sheet_const = wb["Constantes"]
        # Precisamos da última data OMIE *em hora de Espanha* (antes da conversão)
        # Temos de ler o ficheiro CSV novamente para obter a data máxima
        ultima_data_omie = pd.read_csv(FICHEIRO_MIBEL_CSV, parse_dates=['Data'])['Data'].max()
        sheet_const['B90'] = ultima_data_omie.strftime('%m/%d/%Y')
        sheet_const['B91'] = data_relatorio_omip.strftime('%m/%d/%Y')
        # Último dia com ERC real da REN (a coluna M é previsão daí em diante); o
        # simulador assinala a previsão no "ERC Médio do Perfil". O rótulo também é
        # escrito aqui, para não depender de o xlsx do git já ter a linha 92.
        sheet_const['A92'] = 'Data_Valores_ERC'
        sheet_const['B92'] = ultimo_dia_erc.strftime('%m/%d/%Y') if ultimo_dia_erc is not None else None

        wb.save(FICHEIRO_EXCEL)
        print(f"✅ O ficheiro Excel foi atualizado com sucesso!\n   Data_Valores_OMIE = {ultima_data_omie.date()}\n   Data_Valores_OMIP = {data_relatorio_omip.date()}\n   Data_Valores_ERC = {ultimo_dia_erc.date() if ultimo_dia_erc is not None else '(sem dados da REN)'}")

        # ============================================================
        # PASSO 6: Validar a BD e exportar as abas como CSVs individuais
        # ============================================================

        print(f"\n⏳ Passo 6: A validar a BD e a exportar as abas como CSVs individuais...")
        validar_e_exportar_csvs()

    except Exception as e:
        import traceback
        print(f"❌ Ocorreu um erro inesperado: {e}")
        traceback.print_exc()
        # Relançar para o script sair com código != 0. Este é o primeiro passo
        # do pipeline diário: se falhar em silêncio, as fases seguintes correm
        # sobre um xlsx desactualizado e publicam-no como se fosse fresco.
        raise

if __name__ == "__main__":
    run_update_process()