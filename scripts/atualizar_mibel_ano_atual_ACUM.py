import pandas as pd
import numpy as np
import requests
import re
import io
import shutil
from datetime import datetime, timedelta
import os

# Detecta se o script está a correr dentro do GitHub Actions
RUNNING_IN_GITHUB = "GITHUB_ACTIONS" in os.environ

# ============================================================
# CONFIGURAÇÕES
# ============================================================
# Caminhos ancorados no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
# Output em data/omie/ — partilhado pelas Fases 2 do site e dos simuladores.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_OMIE = os.path.join(ROOT_DIR, "data", "omie")
FICHEIRO_MIBEL_CSV = os.path.join(PASTA_OMIE, "MIBEL_ano_atual_ACUM.csv")
FICHEIRO_MIBEL_XLS = os.path.join(PASTA_OMIE, "MIBEL_ano_atual_ACUM.xlsx")
os.makedirs(PASTA_OMIE, exist_ok=True)
DIAS_MINIMOS_ACUM = 365
BACKUP_SUFFIX = ".bak"

# ============================================================
# SISTEMA DE LOGS
# ============================================================
def header(msg): print(f"\n🔵 {msg}")
def log(msg): print(f"   - {msg}")
def sub(msg): print(f"       • {msg}")

# ============================================================
# FUNÇÃO: Extrair dados OMIE (DIÁRIO)
# ============================================================
def tentar_extrair_dados_omie_diario(data_inicio, data_fim):

    if data_inicio > data_fim:
        header("[DIÁRIO] Nenhum intervalo para preencher.")
        return pd.DataFrame()

    header(f"[DIÁRIO] A preencher buracos de {data_inicio} até {data_fim}")
    dias = pd.date_range(data_inicio, data_fim, freq="D")
    sub(f"{len(dias)} dia(s) a verificar…")

    headers = {"User-Agent": "Mozilla/5.0"}
    lista = []
    dias_futuros = 0
    dias_ok = 0
    dias_vazios = 0

    for d in dias:
        data_str = d.strftime('%Y%m%d')
        url = f"https://www.omie.es/es/file-download?parents=marginalpdbcpt&filename=marginalpdbcpt_{data_str}.1"

        try:
            r = requests.get(url, headers=headers, timeout=12)

            if r.status_code == 404:
                dias_futuros += 1
                continue

            r.raise_for_status()

            df = pd.read_csv(
                io.BytesIO(r.content),
                sep=';', skiprows=1, decimal=',', encoding='windows-1252',
                header=None, usecols=[3,4,5],
                names=['Hora','Preco_PT','Preco_ES']
            )

            df.dropna(inplace=True)
            df['Data'] = d.date()

            if df.empty:
                dias_vazios += 1
                continue

            dias_ok += 1
            lista.append(df)

        except Exception:
            dias_vazios += 1
            continue

    # -------- LOG FINAL DOS DIÁRIOS --------
    if dias_ok == 0:
        if dias_futuros + dias_vazios == len(dias):
            sub("ℹ️ Nenhum dia disponível — pode ser futuro ou falha OMIE.")
        return pd.DataFrame()

    sub(f"✅ {dias_ok} dias recolhidos com sucesso.")
    return pd.concat(lista, ignore_index=True)

# ============================================================
# FUNÇÃO: Extrair dados LIVE (ACUM + INDICADORES)
# ============================================================
def tentar_extrair_dados_live():

    header("[LIVE] A extrair dados ACUM + INDICADORES")
    fontes = []
    df_acum = pd.DataFrame()
    df_ind = pd.DataFrame()

    # -------- ACUM --------
    try:
        sub("[ACUM] A descarregar…")

        df_acum = pd.read_csv(
            "https://www.omie.es/sites/default/files/dados/NUEVA_SECCION/INT_PBC_EV_H_ACUM.TXT",
            sep=';', skiprows=2, decimal=',', encoding='windows-1252',
            usecols=[0,1,2,3], names=['Data','Hora','Preco_ES','Preco_PT']
        )

        df_acum['Data'] = pd.to_datetime(df_acum['Data'], format="%d/%m/%Y", errors="coerce")
        df_acum.dropna(subset=['Data'], inplace=True)
        df_acum['Data'] = df_acum['Data'].dt.date
        fontes.append(df_acum)
        sub(f"[ACUM] OK ({len(df_acum)} registos)")

        # --- Intervalo de datas do ACUM ---
        try:
            data_min = df_acum['Data'].min()
            data_max = df_acum['Data'].max()
            sub(f"[ACUM] Intervalo temporal: {data_min} → {data_max}")
        except:
            sub("[ACUM] Intervalo temporal: indisponível")
    except Exception as e:
        sub(f"[ACUM] ❌ Falhou ({e})")

    # -------- INDICADORES --------
    try:
        sub("[INDICADORES] A descarregar…")
        r = requests.get("https://www.omie.es/sites/default/files/dados/diario/INDICADORES.DAT", timeout=10)
        lines = r.content.decode("utf-8", errors="ignore").splitlines()

        sess = [l for l in lines if l.startswith("SESION;")][0]
        data_sessao = pd.to_datetime(sess.split(";")[1], format="%d/%m/%Y").date()

        dados = []
        for l in lines:
            m = re.match(r"^H(\d{1,2})Q([1-4]);", l)
            if not m: continue

            partes = l.split(";")
            hora_h = int(m.group(1))
            quarto = int(m.group(2))
            hora_seq = (hora_h - 1)*4 + quarto

            preco_es = float(partes[1].replace(",", "."))
            preco_pt = float(partes[2].replace(",", "."))

            dados.append([data_sessao, hora_seq, preco_pt, preco_es])

        df_temp_ind = pd.DataFrame(dados, columns=['Data','Hora','Preco_PT','Preco_ES'])
        
        # --- VALIDAÇÃO ROBUSTA DE LINHAS ---
        qtd_linhas = len(df_temp_ind)
        if qtd_linhas in [92, 96, 100]:
            df_ind = df_temp_ind
            fontes.append(df_ind)
            sub(f"[INDICADORES] OK, dia {data_sessao} ({qtd_linhas} registos)")
        else:
            sub(f"[INDICADORES] ⚠️ Ignorado: {qtd_linhas} registos (esperado 96/100/92).")
            # df_ind continua vazio
            
    except Exception as e:
        sub(f"[INDICADORES] ❌ Falhou ({e})")

    if not fontes:
        return pd.DataFrame(), 0, pd.DataFrame()

    df_live = pd.concat(fontes, ignore_index=True)
    num_dias_acum = df_acum['Data'].nunique() if len(df_acum) else 0

    return df_live, num_dias_acum, df_ind

# ============================================================
# FUNÇÃO: Ler histórico local
# ============================================================
def ler_historico_local():

    header("[HISTÓRICO] A carregar histórico local")

    try:
        df = pd.read_csv(FICHEIRO_MIBEL_CSV, parse_dates=['Data'])
        df['Data'] = df['Data'].dt.date
        log(f"✔️ CSV carregado ({len(df)} registos)")
        return df
    except:
        log("ℹ️ CSV inexistente — tentar XLS…")

    try:
        df = pd.read_excel(FICHEIRO_MIBEL_XLS)
        df['Data'] = pd.to_datetime(df['Data']).dt.date
        log(f"✔️ XLS carregado ({len(df)} registos)")
        return df
    except:
        log("⚠️ Nem CSV nem XLS disponíveis — histórico vazio.")
        return pd.DataFrame(columns=['Data','Hora','Preco_PT','Preco_ES'])

# ============================================================
# FUNÇÃO PRINCIPAL
# ============================================================
def run_update_historico():

    header("🚀 A iniciar atualização MIBEL")
    today = datetime.now().date()

    # ---- 1. Ler dados LIVE ----
    df_live, num_acum, df_ind = tentar_extrair_dados_live()
    
    # ---- 2. Ler histórico LOCAL (Sempre necessário) ----
    df_local = ler_historico_local()

    # ---- 3. Definir a BASE ----
    if num_acum >= DIAS_MINIMOS_ACUM:
        header("[PLANO A] ACUM válido — usar como base principal")
        df_base = df_live.copy()
        df_base['Data'] = pd.to_datetime(df_base['Data']).dt.date
        
        # Se tivermos dados LOCAIS que são mais recentes que o ACUM,
        # temos de os preservar para não perder os dias "do meio" (o gap).
        if not df_base.empty and not df_local.empty:
            
            # Para descobrir onde acaba o ACUM real, temos de ignorar 
            # a data do Indicador se ela já estiver misturada no df_base.
            if not df_ind.empty:
                ind_date_val = df_ind['Data'].iloc[0]
                # Filtra tudo o que for MENOR que a data do indicador para achar o max do ACUM
                mask_acum = df_base['Data'] < ind_date_val
                if mask_acum.any():
                    ultima_data_acum = df_base.loc[mask_acum, 'Data'].max()
                else:
                    ultima_data_acum = df_base['Data'].max()
            else:
                ultima_data_acum = df_base['Data'].max()
            # ---------------------

            # Filtrar dados locais que são POSTERIORES ao ACUM real
            df_gap_local = df_local[df_local['Data'] > ultima_data_acum].copy()
            
            if not df_gap_local.empty:
                log(f"⚠️ A recuperar {len(df_gap_local)} registos do local (gap entre ACUM e Hoje)...")
                dias_recup = sorted(df_gap_local['Data'].unique())
                log(f"   Dias recuperados: {dias_recup}")
                df_base = pd.concat([df_base, df_gap_local], ignore_index=True)
        
    else:
        header(f"[PLANO B] ACUM insuficiente ({num_acum} dias)")
        if not df_local.empty:
            df_base = df_local.copy()
        else:
            df_base = df_live.copy() 
            if not df_base.empty:
                df_base['Data'] = pd.to_datetime(df_base['Data']).dt.date

    # Para o passo seguinte (preencher buracos), 'ultima' deve ser a data contínua,
    # ignorando o "salto" do indicador se existir um buraco antes dele.
    if not df_base.empty:
        if not df_ind.empty:
            ind_date_val = df_ind['Data'].iloc[0]
            # Se a base tem o indicador, mas queremos saber onde acaba o histórico contínuo
            # Verificamos se há um buraco. Mas simplificando: usamos o max da base completa
            # SE a recuperação do local funcionou bem.
            ultima = df_base['Data'].max()
        else:
            ultima = df_base['Data'].max()
    else:
        ultima = today - timedelta(days=1)

    log(f"Última data na base consolidada: {ultima}")

    # ---- 4. Preencher buracos antigos (se necessário) ----
    ind_date = df_ind['Data'].iloc[0] if not df_ind.empty else None

    if ind_date:
        log(f"INDICADORES disponível para: {ind_date}")
        # Se temos indicador válido, o buraco fecha ontem
        fim_buracos = ind_date - timedelta(days=1)
    else:
        # Se NÃO temos indicador, assumimos que queremos dados até amanhã 
        fim_buracos = today + timedelta(days=1)

    df_diarios = pd.DataFrame()
    
    # Recalcular 'ultima_continua' ignorando o indicador para ver se falta algo no meio
    ultima_continua = ultima
    if ind_date and ultima >= ind_date:
        # Tenta achar o maximo excluindo o indicador
        mask = df_base['Data'] < ind_date
        if mask.any():
            ultima_continua = df_base.loc[mask, 'Data'].max()
    
    if ultima_continua < fim_buracos:
        df_diarios = tentar_extrair_dados_omie_diario(ultima_continua + timedelta(days=1), fim_buracos)

    # ---- 5. Adicionar INDICADORES ----
    df_ind_to_add = pd.DataFrame()
    
    if ind_date:
        if ind_date > ultima:
            # Caso 1: O indicador é NOVO
            df_ind_to_add = df_ind.copy()
            log(f"➕ A adicionar INDICADORES do dia {ind_date} (Novo)...")
            
        elif ind_date == ultima:
            # Caso 2: O indicador JÁ LÁ ESTÁ 
            log(f"✅ O dia dos INDICADORES ({ind_date}) já foi integrado via Download Live.")
            
        else:
            # Caso 3: O indicador é VELHO
            log(f"ℹ️ O dia dos INDICADORES ({ind_date}) é antigo e desnecessário. Ignorado.")

    # ---- 6. Combinar tudo ----
    header("[FINAL] A combinar e guardar")
    dfs = [df_base]

    if not df_diarios.empty:
        dfs.append(df_diarios)

    if not df_ind_to_add.empty:
        dfs.append(df_ind_to_add)

    df_final = pd.concat(dfs, ignore_index=True)
    
    # ---- Normalização FINAL do DataFrame ----

    # Hora numérica
    df_final['Hora'] = pd.to_numeric(df_final['Hora'], errors='coerce')

    # Preços
    df_final['Preco_PT'] = df_final['Preco_PT'].astype(str).str.replace(',', '.', regex=False)
    df_final['Preco_PT'] = pd.to_numeric(df_final['Preco_PT'], errors='coerce')

    df_final['Preco_ES'] = df_final['Preco_ES'].astype(str).str.replace(',', '.', regex=False)
    df_final['Preco_ES'] = pd.to_numeric(df_final['Preco_ES'], errors='coerce')

    # Remover linhas com NaN críticos
    df_final.dropna(subset=['Data', 'Hora', 'Preco_PT'], inplace=True)

    # ORDEM das colunas
    df_final = df_final[['Data','Hora','Preco_PT','Preco_ES']]

    # Ordenação final e remoção de duplicados exatos
    df_final = df_final.sort_values(['Data','Hora']).drop_duplicates(['Data','Hora'], keep='last')

    # BACKUP INTELIGENTE
    backup_path = FICHEIRO_MIBEL_CSV + BACKUP_SUFFIX

    if not RUNNING_IN_GITHUB:
        try:
            shutil.copy(FICHEIRO_MIBEL_CSV, backup_path)
            log(f"💾 Backup criado (local): {backup_path}")
        except Exception as e:
            log(f"⚠️ Falha ao criar backup local: {e}")
    else:
        log("ℹ️ Backup ignorado (GitHub Actions)")

    df_final.to_csv(FICHEIRO_MIBEL_CSV, index=False, encoding='utf-8-sig', float_format="%.2f")

    log(f"✅ Atualização concluída: {len(df_final)} registos")
    if not df_final.empty:
        log(f"   📅 Dados de: {df_final['Data'].min()} até {df_final['Data'].max()}")
    log("🏁 FIM")

# ============================================================
# ENTRY
# ============================================================
if __name__ == "__main__":
    run_update_historico()
