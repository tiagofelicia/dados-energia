# --- Carregar as bibliotecas necessárias ---
import pandas as pd
import numpy as np
import requests
import openpyxl
from datetime import datetime, date
import io
import re
import os
import json
import hashlib

# ERC quarto-horário (coluna ERC). Fica na mesma pasta: quando o script corre, a
# pasta dele está no sys.path.
import erc_previsao
# Validação da BD antes de exportar os CSVs (Passo 6)
import validar_tarifarios_eletricidade
# Ficheiros por ano da tabela OMIE_PERDAS_CICLOS (caminhos, colunas, leitura, estado)
import omie_perdas_ciclos as opc

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
# ERC da REN em €/MWh (coluna ERC dos ficheiros por ano): o real até ao último dia
# publicado e uma previsão no resto (erc_previsao.py). Os simuladores usam-no quarto
# de hora a quarto de hora nos tarifários que o faturam assim, e a média do período
# nos que faturam a média (Luzigas).
PASTA_ERC_ISP = os.path.join(ROOT_DIR, "data", "erc", "isp")
# Intermédio da Fase 1 (atualizar_mibel_ano_atual_ACUM.py) — partilhado com o pipeline do site
FICHEIRO_MIBEL_CSV = os.path.join(ROOT_DIR, "data", "omie", "MIBEL_ano_atual_ACUM.csv")

# CSVs individuais: as três abas do xlsx e a OMIE_PERDAS_CICLOS, que já não vem do
# xlsx — um ficheiro por ano (csv/OMIE_PERDAS_CICLOS_AAAA.csv), montado a partir da
# base do ano (base/OMIE_PERDAS_CICLOS_base_AAAA.csv). Ver omie_perdas_ciclos.py.
PASTA_CSV = opc.PASTA_CSV
ABAS_PARA_CSV = ["Constantes", "Tarifarios_fixos", "Indexados"]
# Transição: o v64 e o Dual v18 publicados leem um ficheiro único com o ano anterior
# e o atual (csv/OMIE_PERDAS_CICLOS.csv). Passa a False — e sai do manifest — quando
# todas as versões publicadas dos simuladores lerem os ficheiros por ano.
PUBLICAR_COMBINADO = True

print(f"ℹ️ Fonte de dados: '{FICHEIRO_MIBEL_CSV}'")
print("⚠️ Dados OMIE e futuros")
# ===================================================================

def montar_ano(base, anterior, precos_pt):
    """
    Monta o ficheiro de um ano aberto: as colunas da base (calendário, ciclos,
    perfis, perdas) + OMIE + ERC (preenchido a seguir, em calcular_erc).

    Cada linha é localizada pelo dia (texto 'MM/DD/AAAA') e pela posição dentro do
    dia, em hora de Portugal — o alinhamento que o Passo 5 sempre usou. Assim os
    dias de 92 e 100 quartos de hora ficam certos sem depender das etiquetas Hora.

    O OMIE parte do ficheiro já publicado (anterior) e só é substituído onde há
    preço novo (precos_pt: real do MIBEL ou futuros OMIP). O MIBEL acumulado só
    cobre os últimos ~12 meses: sem isto, os primeiros dias do ano perdiam o preço
    quando saíssem dessa janela — é o que o xlsx fazia, ao manter o que lá estava.
    """
    df = base[opc.COLUNAS_BASE].reset_index(drop=True)
    df["_dia"] = pd.to_datetime(df["Data"], format="%m/%d/%Y").dt.date
    df["_pos"] = df.groupby("_dia").cumcount() + 1

    if anterior is not None:
        a = anterior[["Data", "OMIE"]].reset_index(drop=True)
        a["_dia"] = pd.to_datetime(a["Data"], format="%m/%d/%Y").dt.date
        a["_pos"] = a.groupby("_dia").cumcount() + 1
        df = df.merge(a[["_dia", "_pos", "OMIE"]], on=["_dia", "_pos"], how="left")
    else:
        df["OMIE"] = np.nan

    if precos_pt is not None and len(precos_pt):
        p = precos_pt.rename(columns={"Data": "_dia", "Hora": "_pos"})[["_dia", "_pos", "Preco"]]
        df = df.merge(p, on=["_dia", "_pos"], how="left")
        df["OMIE"] = df["Preco"].where(df["Preco"].notna(), df["OMIE"])
        df = df.drop(columns="Preco")

    df["OMIE"] = df["OMIE"] + 0.0   # -0.0 → 0.0, como na ida e volta pelo xlsx
    df["ERC"] = np.nan
    return df


def carregar_erc():
    """Os dados do ERC da REN (erc_previsao.carregar), ou None se não houver."""
    print("   - A ler o ERC da REN...")
    dados = erc_previsao.carregar(PASTA_ERC_ISP)
    if dados is not None:
        print(f"     ERC: dados da REN até {dados['ultimo'].strftime('%d/%m/%Y')} "
              f"({dados['atraso']} dias de atraso)")
        if dados["desatualizado"]:
            print(f"     ⚠️ Mais de {erc_previsao.ATRASO_MAX_DIAS} dias de atraso: só os valores reais, "
                  f"sem previsões (os simuladores usam as constantes nesses dias).")
    return dados


def instantes_utc(df):
    """Instante em que começa cada linha (dia + posição no dia, em hora de Portugal), em UTC."""
    return (pd.to_datetime(df["_dia"]).dt.tz_localize("Europe/Lisbon")
            + pd.to_timedelta(15 * (df["_pos"] - 1), unit="m")).dt.tz_convert("UTC")


def arredondar_erc(valores):
    # round() do Python, valor a valor — o mesmo arredondamento que ia para o xlsx.
    # O "+ 0.0" faz de -0.0 um 0.0, como fazia a ida e volta pelo xlsx (um ERC de
    # -0,004 €/MWh saía "-0.0" no CSV)
    return [round(float(v), 2) + 0.0 if not np.isnan(v) else np.nan for v in valores]


def calcular_erc(anos, dados):
    """
    Preenche o ERC (€/MWh) dos anos abertos ({ano: DataFrame de montar_ano}): o
    real onde a REN já publicou e a previsão própria no resto (erc_previsao.py).

    O valor de cada quarto de hora não depende dos outros pedidos, por isso
    calcular só os anos abertos dá o mesmo que dava a folha inteira.

    A coluna é sempre recalculada de ponta a ponta: sem dados da REN fica vazia e
    os simuladores usam as constantes da BD, nunca valores de uma corrida antiga.
    """
    if not anos:
        return
    ordem = sorted(anos)
    todos = pd.concat([anos[a][["_dia", "_pos"]] for a in ordem], ignore_index=True)
    valores, contagem = erc_previsao.erc_instantes(dados, instantes_utc(todos))
    valores = arredondar_erc(valores)
    inicio = 0
    for a in ordem:
        n = len(anos[a])
        anos[a]["ERC"] = pd.Series(valores[inicio:inicio + n], dtype="float64").to_numpy()
        inicio += n
    print(f"     ERC: {contagem['real']} quartos de hora reais, {contagem['curto']} previstos "
          f"a curto prazo, {contagem['longo']} a longo prazo, {contagem['sem']} vazios.")


def erc_real(df, dados):
    """Só o ERC real (sem previsões) de cada linha, arredondado; NaN onde a REN não o tem."""
    if dados is None:
        return np.full(len(df), np.nan)
    idx = pd.DatetimeIndex(instantes_utc(df))
    return np.array(arredondar_erc(dados["reais"].reindex(idx).to_numpy(dtype=float, copy=True)), dtype=float)


def com_posicoes(df):
    df = df.reset_index(drop=True)
    df["_dia"] = pd.to_datetime(df["Data"], format="%m/%d/%Y").dt.date
    df["_pos"] = df.groupby("_dia").cumcount() + 1
    return df


def montar_fechado(publicado, base, dados):
    """
    Um ano fechado volta a ser montado em cada corrida, mas só é reescrito se mudar:
    as colunas fixas vêm da base (se houver — assim uma base corrigida conta) ou do
    próprio ficheiro, o OMIE fica como está (é definitivo) e o ERC é o real da REN,
    que corrige dias com meses de atraso. Onde a REN deixar de ter o valor, fica o
    que lá estava.
    """
    df = com_posicoes((base if base is not None else publicado)[opc.COLUNAS_BASE])
    pub = com_posicoes(publicado[["Data", "OMIE", "ERC"]]).rename(columns={"ERC": "_erc_antes"})
    df = df.merge(pub[["_dia", "_pos", "OMIE", "_erc_antes"]], on=["_dia", "_pos"], how="left")
    reais = erc_real(df, dados)
    df["ERC"] = np.where(~np.isnan(reais), reais, df["_erc_antes"].to_numpy(dtype=float))
    return df.drop(columns="_erc_antes")


def importar_ano(ano, base, dados):
    """
    Um ano passado que ainda não existe (ex.: 2024) entra pela base: as colunas fixas
    da base, o OMIE PT do histórico (data/omie/historico/omie_historico_AAAA.csv, por
    dia e posição no dia) e o ERC real da REN. Fecha logo.
    """
    caminho = opc.caminho_historico(ano)
    if not os.path.exists(caminho):
        raise ValueError(f"há base de {ano} mas não há histórico do OMIE desse ano ({caminho})")
    h = pd.read_csv(caminho, encoding="utf-8-sig", float_precision="round_trip",
                    dtype={"dia": str, "hora": str, "BD": str, "BS": str, "TD": str, "TS": str})
    h["_dia"] = pd.to_datetime(h["dia"], format="%d/%m/%Y").dt.date
    h["_pos"] = h.groupby("_dia").cumcount() + 1
    h = h.rename(columns={"preco_pt": "OMIE"})

    df = com_posicoes(base[opc.COLUNAS_BASE])
    df = df.merge(h[["_dia", "_pos", "OMIE", "BD", "BS", "TD", "TS", "hora"]].add_suffix("_h")
                  .rename(columns={"_dia_h": "_dia", "_pos_h": "_pos"}), on=["_dia", "_pos"], how="left")
    diferentes = int(sum((df[c] != df[c + "_h"]).sum() for c in ["BD", "BS", "TD", "TS"]))
    diferentes += int((df["Hora"] != df["hora_h"]).sum())
    if diferentes:
        print(f"     ⚠️ {ano}: {diferentes} valor(es) de ciclos ou horas da base diferentes do histórico (fica a base).")
    df["OMIE"] = df["OMIE_h"] + 0.0
    df = df.drop(columns=[c for c in df.columns if c.endswith("_h")])
    df["ERC"] = erc_real(df, dados)
    return df


def mesmo_conteudo(novo, publicado):
    """O ano montado é igual ao publicado (valores, não bytes)?"""
    a = novo[opc.COLUNAS_ANO].reset_index(drop=True)
    b = publicado[opc.COLUNAS_ANO].reset_index(drop=True)
    return a.shape == b.shape and a.equals(b.astype(a.dtypes.to_dict()))


def ano_completo(ano, df, ultima_data_omie, ultimo_dia_erc):
    """
    Um ano fecha quando já tem tudo real: o OMIE até ao fim de 31/12 em hora de
    Portugal — a última hora sai do dia de mercado 1/1 seguinte, em hora de Espanha,
    por isso é preciso o MIBEL até lá — e o ERC real da REN até 31/12.
    """
    if pd.Timestamp(ultima_data_omie).date() < date(ano + 1, 1, 1):
        return False
    if ultimo_dia_erc is None or pd.Timestamp(ultimo_dia_erc).date() < date(ano, 12, 31):
        return False
    if df["OMIE"].isna().any() or df["ERC"].isna().any():
        print(f"     ⚠️ {ano} já devia fechar mas tem quartos de hora sem OMIE ou sem ERC: fica aberto.")
        return False
    return True


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


def validar_e_exportar_csvs(anos_novos, estado, fechar):
    """
    Valida as três abas do xlsx e os anos abertos acabados de montar e, só se não
    houver erros, exporta tudo: os CSVs das abas, um ficheiro por ano aberto, o
    ficheiro único da transição (PUBLICAR_COMBINADO) e o manifest.

    anos_novos: {ano: DataFrame} dos ficheiros a escrever (abertos, importados e
    fechados que mudaram); estado: o de opc.estado_dos_anos() no início da corrida;
    fechar: os anos que passam a fechados nesta corrida (incluindo os importados).
    Os outros anos ficam como estão publicados.

    A exportação é tudo ou nada: os ficheiros novos são escritos ao lado (.tmp) e
    só substituem os publicados depois de todos estarem escritos, com o manifest
    em último lugar. Antes, uma aba que falhasse ficava de fora do manifest e as
    outras eram publicadas na mesma.
    """
    anos_novos = {a: df[opc.COLUNAS_ANO] for a, df in anos_novos.items()}
    # Os que fecham nesta corrida já são validados como fechados (ERC completo)
    estado_validacao = dict(estado, fechados=estado["fechados"] | set(fechar))
    try:
        abas = validar_tarifarios_eletricidade.ler_abas(FICHEIRO_EXCEL)
        relatorio = validar_tarifarios_eletricidade.validar(abas, anos=anos_novos, estado=estado_validacao)
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

    def md5(caminho):
        # Hash MD5 do conteúdo para o manifest (o simulador compara-o com o da cache)
        with open(caminho, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()[:8]

    os.makedirs(PASTA_CSV, exist_ok=True)
    trocas = []          # (temporário, definitivo), pela ordem em que vão substituir
    manifest = {}
    try:
        for aba in ABAS_PARA_CSV:
            final = os.path.join(PASTA_CSV, f"{aba}.csv")
            trocas.append((final + ".tmp", final))
            abas[aba].to_csv(final + ".tmp", index=False, encoding='utf-8-sig')
            manifest[aba] = md5(final + ".tmp")
            print(f"   ✅ {aba}.csv ({len(abas[aba])} registos) [{manifest[aba]}]")

        # Os anos: os abertos acabados de montar e os outros tal como estão publicados
        tabelas = {}
        for ano in sorted(estado["publicados"] | set(anos_novos)):
            if ano in anos_novos:
                final = opc.caminho_ano(ano)
                trocas.append((final + ".tmp", final))
                anos_novos[ano].to_csv(final + ".tmp", index=False, encoding='utf-8-sig')
                tabelas[ano] = (anos_novos[ano], md5(final + ".tmp"))
            else:
                tabelas[ano] = (None, md5(opc.caminho_ano(ano)))

        # Transição: o ficheiro único com o ano atual e o anterior, por esta ordem
        # (a do antigo xlsx), para as versões publicadas que ainda o leem
        if PUBLICAR_COMBINADO:
            ano_atual = opc.hoje_pt().year
            partes = []
            for ano in (ano_atual, ano_atual - 1):
                if ano in tabelas:
                    df, _ = tabelas[ano]
                    partes.append(df if df is not None else opc.ler(opc.caminho_ano(ano))[opc.COLUNAS_ANO])
            final = os.path.join(PASTA_CSV, opc.FICHEIRO_COMBINADO)
            trocas.append((final + ".tmp", final))
            pd.concat(partes, ignore_index=True).to_csv(final + ".tmp", index=False, encoding='utf-8-sig')
            manifest[opc.NOME] = md5(final + ".tmp")
            print(f"   ✅ {opc.FICHEIRO_COMBINADO} (transição: {ano_atual} + {ano_atual - 1}) [{manifest[opc.NOME]}]")

        for ano, (df, h) in tabelas.items():
            manifest[f"{opc.NOME}_{ano}"] = h
            estado_ano = opc.FECHADO if (ano in estado["fechados"] or ano in fechar) else opc.ABERTO
            nota = "reescrito" if df is not None else "sem alterações"
            print(f"   ✅ {opc.NOME}_{ano}.csv ({estado_ano}, {nota}) [{h}]")
        manifest["anos"] = {str(ano): (opc.FECHADO if (ano in estado["fechados"] or ano in fechar) else opc.ABERTO)
                            for ano in tabelas}

        final = os.path.join(PASTA_CSV, "manifest.json")
        trocas.append((final + ".tmp", final))
        with open(final + ".tmp", 'w', encoding='utf-8') as f:
            json.dump(manifest, f)
    except Exception as e:
        for tmp, _ in trocas:
            if os.path.exists(tmp):
                os.remove(tmp)
        sinalizar_bd_bloqueada(f"falha ao exportar: {e}")
        return None

    for tmp, final in trocas:
        os.replace(tmp, final)
    print(f"   ✅ manifest.json gerado: {manifest}")
    print("✅ Exportação de CSVs concluída.")
    return manifest


def run_update_process():
    """
    Função principal que encapsula todo o processo de ETL.
    """
    try:
        # ========================================================
        # PASSO 0: Que anos há (abertos, fechados) — ver omie_perdas_ciclos.py
        # ========================================================
        estado = opc.estado_dos_anos()
        anos_abertos = estado["abertos"]
        print(f"\nℹ️ OMIE_PERDAS_CICLOS — anos abertos: {anos_abertos or '—'} · "
              f"fechados: {sorted(estado['fechados']) or '—'}"
              + (f" · a importar do histórico: {estado['importar']}" if estado["importar"] else ""))
        # Os passos 3 e 4 precisam de pelo menos um ano; sem anos abertos os preços
        # calculados simplesmente não são usados
        anos_calculo = anos_abertos or [opc.hoje_pt().year]

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
            # Do 1.º ano aberto até ao fim do ano a seguir ao último (os futuros
            # semanais/mensais/trimestrais propagam-se por estes dias)
            'Data': pd.date_range(start=f'{min(anos_calculo)}-01-01', end=f'{max(anos_calculo) + 1}-12-31', freq='D')
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
        
        # Até 1/1 do ano a seguir ao último ano aberto: a última hora de 31/12 em
        # Portugal é o dia de mercado 1/1 seguinte em Espanha
        datas_futuras = pd.date_range(start=ultima_data_historica + pd.Timedelta(days=1),
                                      end=f'{max(anos_calculo) + 1}-01-01', freq='D')

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

        # Selecionar apenas os anos abertos (os fechados não voltam a ser escritos)
        dados_finais_pt = dados_finais_pt[dados_finais_pt['datetime_pt'].dt.year.isin(anos_calculo)].copy()
        dados_finais_pt = dados_finais_pt[['Data_PT', 'Hora_PT', 'Preco']].rename(
            columns={'Data_PT': 'Data', 'Hora_PT': 'Hora'}
        )
        dados_finais_pt = dados_finais_pt.dropna(subset=['Preco']).reset_index(drop=True)

        print(f"✅ {len(dados_finais_pt)} registos finais (em PT) preparados.")

        # ============================================================
        # PASSO 5: Ficheiros por ano da OMIE_PERDAS_CICLOS + datas no xlsx
        # ============================================================

        print("\n⏳ Passo 5: A montar os ficheiros por ano da OMIE_PERDAS_CICLOS...")

        # Preços calculados (Passo 4), por dia e posição no dia, em hora de Portugal
        precos_pt = dados_finais_pt.copy()
        precos_pt['Data'] = pd.to_datetime(precos_pt['Data']).dt.date
        precos_pt['Hora'] = precos_pt['Hora'].astype(int)
        # A última data OMIE *em hora de Espanha* (antes da conversão)
        ultima_data_omie = pd.read_csv(FICHEIRO_MIBEL_CSV, parse_dates=['Data'])['Data'].max()

        # Uma base ou um ficheiro ilegível não pode parar as fases 2B (dados do
        # site): não se publica a BD do simulador e segue-se, como na validação.
        # anos_novos: os ficheiros a escrever nesta corrida (abertos, importados e
        # fechados que mudaram); fechar: os que passam a fechados agora
        anos_novos, fechar, ultimo_dia_erc, montagem_ok = {}, set(), None, True
        try:
            dados_erc = carregar_erc()
            ultimo_dia_erc = dados_erc["ultimo"] if dados_erc is not None else None

            # Abertos: base + OMIE (real e futuros) + ERC (real e previsão própria)
            abertos = {}
            for ano in anos_abertos:
                base = opc.ler(opc.caminho_base(ano))
                anterior = opc.ler(opc.caminho_ano(ano)) if os.path.exists(opc.caminho_ano(ano)) else None
                abertos[ano] = montar_ano(base, anterior, precos_pt)
                origem = "a partir do ficheiro publicado" if anterior is not None else "ano novo, só a base"
                print(f"   - {ano} (aberto): {len(abertos[ano])} quartos de hora ({origem}), "
                      f"{int(abertos[ano]['OMIE'].isna().sum())} sem OMIE")
            calcular_erc(abertos, dados_erc)
            for ano in anos_abertos:
                if ano_completo(ano, abertos[ano], ultima_data_omie, ultimo_dia_erc):
                    fechar.add(ano)
                    print(f"   🔒 {ano} tem OMIE e ERC reais até 31/12: fica fechado a partir desta corrida.")
            anos_novos.update(abertos)

            # Passados ainda sem ficheiro: base + OMIE do histórico + ERC real; fecham já
            for ano in estado["importar"]:
                anos_novos[ano] = importar_ano(ano, opc.ler(opc.caminho_base(ano)), dados_erc)
                fechar.add(ano)
                print(f"   📥 {ano} (importado do histórico): {len(anos_novos[ano])} quartos de hora, "
                      f"{int(anos_novos[ano]['ERC'].isna().sum())} sem ERC real — fica fechado.")

            # Fechados: OMIE congelado; o ERC real (a REN corrige meses depois) e a
            # base, se tiver sido corrigida. Só se reescreve o que mudou.
            for ano in sorted(estado["fechados"]):
                publicado = opc.ler(opc.caminho_ano(ano))
                base = opc.ler(opc.caminho_base(ano)) if ano in estado["bases"] else None
                novo = montar_fechado(publicado, base, dados_erc)
                if mesmo_conteudo(novo, publicado):
                    print(f"   - {ano} (fechado): sem alterações")
                else:
                    antes, depois = publicado["ERC"].to_numpy(dtype=float), novo["ERC"].to_numpy(dtype=float)
                    erc_mudou = int((~((antes == depois) | (np.isnan(antes) & np.isnan(depois)))).sum())
                    fixas_mudaram = not novo[opc.COLUNAS_BASE].reset_index(drop=True).equals(
                        publicado[opc.COLUNAS_BASE].reset_index(drop=True))
                    motivos = ([f"ERC corrigido pela REN em {erc_mudou} quarto(s) de hora"] if erc_mudou else []) + \
                              (["base corrigida"] if fixas_mudaram else [])
                    print(f"   ✏️ {ano} (fechado): reescrito — {' e '.join(motivos) or 'conteúdo diferente'}")
                    anos_novos[ano] = novo
        except Exception as e:
            import traceback
            traceback.print_exc()
            montagem_ok = False
            sinalizar_bd_bloqueada(f"falha a montar os ficheiros por ano: {e}")

        # Datas de referência na folha Constantes do xlsx (vão para o Constantes.csv)
        print(f"   - A carregar '{FICHEIRO_EXCEL}' para escrever as datas de referência...")
        wb = openpyxl.load_workbook(FICHEIRO_EXCEL)
        sheet_const = wb["Constantes"]
        sheet_const['B90'] = ultima_data_omie.strftime('%m/%d/%Y')
        sheet_const['B91'] = data_relatorio_omip.strftime('%m/%d/%Y')
        # Último dia com ERC real da REN (a coluna ERC é previsão daí em diante); o
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
        if montagem_ok:
            validar_e_exportar_csvs(anos_novos, estado, fechar)

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