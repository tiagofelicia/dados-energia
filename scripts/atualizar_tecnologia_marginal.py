#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_tecnologia_marginal.py
Que tecnologias estão ao preço do mercado diário do MIBEL (OMIE), período a período, em Portugal e
em Espanha, desde 2018.

FONTES (OMIE, públicas, sem registo)
------------------------------------
  curva_pbc_uof_AAAAMM.zip  As curvas de oferta e de procura do mercado diário com o código de cada
                            unidade. A OMIE só as publica 90 dias depois: o zip de um mês vai
                            crescendo dia a dia (a 18/11/2026 o de agosto ia até 19/08).
  cab_AAAAMM.zip            Os cabeçalhos das ofertas, com o nome de cada unidade («C.T. SINES 4»,
                            «C.C. TURBOGÁS 1»): identificam as unidades que já não estão na lista.
  LISTA_UNIDADES.XLS        A lista das unidades vigentes, com o tipo e a tecnologia.
  Os preços vêm dos ficheiros deste repositório (data/omie/historico/omie_historico_AAAA.csv e
  data/omie/omie_dados_atuais.csv), pela coluna data_utc.

MÉTODO
------
Em cada período do mercado diário (uma hora até 30/09/2025, um quarto de hora desde 01/10/2025),
contam-se as ofertas CASADAS (de venda e de compra) cujo preço é igual ao preço marginal de
Portugal, e as que são iguais ao de Espanha (a menos de 0,005 €/MWh). Cada unidade é arrumada num
grupo de tecnologia: pela tecnologia da lista da OMIE; se já lá não está, pelo nome nas ofertas; se
nem isso, pelo padrão do código (as unidades de venda dos representantes do regime especial acabam
em VDnn ou REnn, as de compra dos comercializadores em RMnn ou Cnn). O grupo de cada oferta depende
também do lado (GRUPOS: V venda, C compra): uma bateria a carregar, uma interligação a exportar ou a
bombagem a bombear são ofertas de compra; as unidades MIE e MIP (as ligações entre as zonas de
Portugal e de Espanha) aparecem dos dois lados.

Estar ao preço não é o mesmo que marcar o preço: num período podem estar várias tecnologias ao
preço, e com o acoplamento europeu o preço pode vir de fora do MIBEL (fica «SEM»).

SAÍDAS (data/omie/tecnologia-marginal/)
---------------------------------------
  tecnologia_marginal_AAAA-MM.csv  Um período por linha (um ficheiro por mês do dia de mercado: cada corrida só
                                reescreve o mês que está a crescer, e não o ano inteiro): dia_mercado,
                                periodo, minutos, preco_pt, preco_es, tecnologias_pt, tecnologias_es
                                (grupos separados por +), unidades_pt, unidades_es (códigos OMIE
                                separados por espaço; /C = oferta de compra) e data_utc (início do
                                período).
  unidades.csv                  As unidades que já estiveram ao preço, com o grupo e a origem da
                                classificação.
  resumo.json                   Os totais por ano, por mês e nos últimos 12 meses que a página
                                /quem-marca-o-preco lê, com Portugal pelo dia e pela hora de Portugal
                                (até à véspera do último dia de mercado: a última hora do dia português,
                                23h–24h, pertence ao dia de mercado seguinte).
  resumo_cet.json               O mesmo com Portugal pelo dia de mercado, em hora CET.
                                Nos dois, Espanha conta sempre pelo dia e pela hora de Espanha (CET).
  metadata.json                 O primeiro e o último dia, os grupos e a parte por classificar.

Os grupos dos CSV são recalculados em cada corrida a partir das unidades: uma classificação nova
(uma unidade que entra na lista, por exemplo) chega a todo o histórico sem voltar a descarregar.

USO
---
  python atualizar_tecnologia_marginal.py                  # os meses desde o último dia + o seguinte
  python atualizar_tecnologia_marginal.py --desde 2018-01  # refaz tudo desde esse mês
  python atualizar_tecnologia_marginal.py --reagregar      # só os grupos e o resumo (sem descarregar)
Para testes: --cache PASTA guarda (e reutiliza) os zips; --meses 2018-01,2025-10 só esses meses.
"""

import argparse
import csv
import glob
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA = os.path.join(ROOT_DIR, "data", "omie", "tecnologia-marginal")
PASTA_OMIE = os.path.join(ROOT_DIR, "data", "omie")

URL_FICHEIRO = "https://www.omie.es/es/file-download?parents={pai}&filename={nome}"
URL_LISTA = "https://www.omie.es/sites/default/files/dados/listados/LISTA_UNIDADES.XLS"
UA = {"User-Agent": "Mozilla/5.0 (dados-energia; +https://dados.tiagofelicia.pt)"}

INICIO = (2018, 1)          # o primeiro mês que a OMIE tem no repositório público
TOLERANCIA = 0.005          # €/MWh: os preços vêm com 2 casas decimais
MAD = ZoneInfo("Europe/Madrid")
LIS = ZoneInfo("Europe/Lisbon")
UTC = timezone.utc

# Os grupos, pela ordem em que aparecem nos ficheiros e na página. SEM não é uma tecnologia: é o
# período em que nenhuma oferta casada tem exatamente o preço (o preço veio do acoplamento com
# o resto da Europa, de uma oferta complexa ou ficou entre dois blocos).
# Primeiro os de venda (V), depois os de compra (C): é esta a ordem das barras na página, com a linha
# que separa os dois lados.
GRUPOS = [
    ("HID", "Hídrica", "V", "Albufeiras e fios de água (inclui as reversíveis quando oferecem como hídricas)"),
    ("BOMP", "Bombagem a produzir", "V", "Centrais de bombagem pura a turbinar"),
    ("GAS", "Gás natural", "V", "Centrais de ciclo combinado"),
    ("CAR", "Carvão", "V", "Centrais térmicas convencionais (carvão)"),
    ("NUC", "Nuclear", "V", "Centrais nucleares (só em Espanha)"),
    ("SOL", "Solar", "V", "Fotovoltaica e solar térmica"),
    ("EOL", "Eólica", "V", "Eólica em terra e no mar"),
    ("TER", "Cogeração e outras térmicas", "V", "Cogeração e outras térmicas não renováveis"),
    ("REN", "Biomassa e outras renováveis", "V", "Biomassa, resíduos renováveis, geotérmica e híbridas renováveis"),
    ("ARM", "Baterias a descarregar", "V", "Armazenamento e híbridas com armazenamento, a vender"),
    ("REP", "Produção sem tecnologia identificada", "V", "Vendas de representantes do regime especial e de carteiras de produção"),
    ("INT", "Interligações (venda)", "V", "Importação de agentes externos e ligações entre zonas, do lado da venda"),
    ("OUT", "Outras ofertas de venda", "V", "Vendas de comercializadores, unidades genéricas e unidades por classificar"),
    ("BOMC", "Bombagem a consumir", "C", "Ofertas de compra para bombear água"),
    ("ARMC", "Baterias a carregar", "C", "Armazenamento a comprar"),
    ("PRO", "Comercializadores e consumidores", "C", "Ofertas de compra de comercializadores e consumidores"),
    ("INTC", "Interligações (compra)", "C", "Exportação de agentes externos e ligações entre zonas, do lado da compra"),
    ("OUTC", "Outras ofertas de compra", "C", "Compras de outras unidades e de unidades por classificar"),
    ("SEM", "Nenhuma oferta ao preço exato", "", "Nenhuma oferta casada com o preço do período"),
]
ORDEM = [g[0] for g in GRUPOS]
NOMES = {g[0]: g[1] for g in GRUPOS}
LADO = {g[0]: g[2] for g in GRUPOS}
# O grupo de cada OFERTA: o grupo da unidade acertado pelo lado da oferta
_DE_COMPRA = {"BOMP": "BOMC", "BOMC": "BOMC", "ARM": "ARMC", "INT": "INTC", "PRO": "PRO"}
_DE_VENDA = {"BOMC": "BOMP", "PRO": "OUT"}


def grupo_oferta(grupo_unidade, lado):
    if lado == "C":
        return _DE_COMPRA.get(grupo_unidade, "OUTC")
    return _DE_VENDA.get(grupo_unidade, grupo_unidade)

# Tecnologia da LISTA_UNIDADES (coluna TECNOLOGÍA) -> grupo
TEC_GRUPO = {
    "RE Mercado Solar Fotovoltáica": "SOL", "RE Mercado Solar Térmica": "SOL",
    "RE Tar. CUR Solar Fotovoltáica": "SOL", "RE Tar. CUR Solar Térmica": "SOL",
    "RE Mercado Eólica": "EOL", "RE Mercado Eólica Marina": "EOL", "RE Tar. CUR Eólica": "EOL",
    "RE Mercado Hidráulica": "HID", "Hidráulica Generación": "HID", "RE Tar. CUR Hidráulica": "HID",
    "Hidráulica de Bombeo Puro": "BOMP",
    "Consumo de bombeo": "BOMC", "Consumo Bombeo Mixto": "BOMC", "Consumo Bombeo Puro": "BOMC",
    "Ciclo Combinado": "GAS", "Gas": "GAS",
    "Nuclear": "NUC",
    "Hulla Antracita": "CAR", "Carbón de Importación": "CAR", "Hulla+Antracita": "CAR",
    "Lignito Negro": "CAR", "Lignito Pardo": "CAR",
    "RE Mercado Térmica no Renovab.": "TER", "RE Tar. CUR Térmica no Renov.": "TER",
    "Híbrida Renov.-Térmica": "TER", "Híbrida Renov-Térmica-Almac.": "TER", "Fuel-Gas": "TER", "Fuel": "TER",
    "RE Mercado Térmica Renovable": "REN", "RE Tar. CUR Térmica Renovable": "REN",
    "RE Mercado Geotérmica": "REN", "Híbrida Renovable": "REN",
    "Almacenamiento Venta": "ARM", "Almacenamiento Compra": "ARM", "Híbrida Renov.-Almacenamiento": "ARM",
    "RE Tarifa CUR (uof)": "REP", "Agente vendedor Reg. Especial": "REP", "Porfolio Produccion Venta": "REP",
    "Import. de agentes externos": "INT", "Import. de comercializadoras": "INT",
    "Comercializador": "PRO", "Compras Comercialización": "PRO", "Compras Consumo Directo": "PRO",
    "Consumidor directo": "PRO", "Compra Comercializador Balance": "PRO", "Comercializador no residente": "PRO",
    "Consumo de productores": "PRO", "Porfolio Produccion Compra": "PRO", "Porfolio Comerc. Compra": "PRO",
    "Compras Consumos Auxiliares": "PRO", "Compras Cons. Directo Balance": "PRO",
    "Rep. de consumidores directos": "PRO", "Rep. de comercializadores": "PRO", "Rep. Consumos Auxiliares": "PRO",
    "Comercializador ultimo recurso": "PRO",
}
TIPO_GRUPO = {"COMERCIALIZADOR": "PRO", "CONSUMIDOR DIRECTO": "PRO", "COMERCIALIZADOR ULTIMO RECURSO": "PRO",
              "DISTRIBUIDOR": "PRO", "CONTRATO INTERNACIONAL": "INT", "BOMBEO": "BOMC"}

COLUNAS = ["dia_mercado", "periodo", "minutos", "preco_pt", "preco_es",
           "tecnologias_pt", "tecnologias_es", "unidades_pt", "unidades_es", "data_utc"]
COLUNAS_UNIDADES = ["codigo", "nome", "lado", "tipo_unidade", "tecnologia_omie", "zona", "grupo", "origem",
                    "primeira", "ultima"]


# ============================================================
# Descargas
# ============================================================
def descarregar(url, destino, tentativas=4):
    """Grava url em destino; devolve False se o ficheiro ainda não existir (a OMIE responde 404, ou 200 com
    uma página em vez do ficheiro)."""
    for i in range(tentativas):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r, open(destino, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            with open(destino, "rb") as f:
                inicio = f.read(8)
            # zip (PK), xls (D0 CF 11 E0); a OMIE responde 200 com HTML aos ficheiros que não tem
            return inicio[:2] == b"PK" or inicio[:4] == b"\xd0\xcf\x11\xe0"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            print(f"  ⚠ {url}: {e} (tentativa {i + 1})")
            time.sleep(5 * (i + 1))
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ {url}: {e} (tentativa {i + 1})")
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"Falhou a descarga de {url}")


def ficheiro_mes(pai, ano, mes, pasta_tmp, cache):
    nome = f"{pai}_{ano}{mes:02d}.zip"
    if cache:
        alvo = os.path.join(cache, nome)
        if os.path.exists(alvo) and zipfile.is_zipfile(alvo):
            return alvo
    else:
        alvo = os.path.join(pasta_tmp, nome)
    ok = descarregar(URL_FICHEIRO.format(pai=pai, nome=nome), alvo)
    if not ok:
        if os.path.exists(alvo):
            os.remove(alvo)
        return None
    return alvo


# ============================================================
# Preços (data_utc -> (PT, ES))
# ============================================================
def carregar_precos():
    precos = {}
    fich = sorted(glob.glob(os.path.join(PASTA_OMIE, "historico", "omie_historico_*.csv")))
    fich.append(os.path.join(PASTA_OMIE, "omie_dados_atuais.csv"))
    for f in fich:
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                k, pt, es = r.get("data_utc"), r.get("preco_pt"), r.get("preco_es")
                if not k or not pt or not es:
                    continue   # a tabela de atualizações no fim do omie_dados_atuais.csv
                try:
                    precos[k] = (float(pt), float(es))
                except ValueError:
                    continue
    return precos


# ============================================================
# Unidades: lista da OMIE, nomes das ofertas e classificação
# ============================================================
def ler_lista(caminho):
    import xlrd   # só aqui: o --reagregar não precisa dele
    folha = xlrd.open_workbook(caminho, logfile=open(os.devnull, "w")).sheet_by_index(0)
    cab = None
    for r in range(min(15, folha.nrows)):
        if str(folha.cell_value(r, 0)).strip().upper() == "CODIGO":
            cab = r
            break
    if cab is None:
        raise RuntimeError("LISTA_UNIDADES.XLS sem a linha de cabeçalho CODIGO")
    nomes = [str(x).strip().upper() for x in folha.row_values(cab)]
    col = lambda p: next(i for i, n in enumerate(nomes) if n.startswith(p))  # noqa: E731
    ic, idsc, it, iz, itec = col("CODIGO"), col("DESCRIP"), col("TIPO UNIDAD"), col("ZONA"), col("TECNOLOG")
    lista = {}
    for r in range(cab + 1, folha.nrows):
        v = folha.row_values(r)
        cod = str(v[ic]).strip()
        if cod and cod not in lista:
            lista[cod] = dict(nome=str(v[idsc]).strip(), tipo_unidade=str(v[it]).strip(),
                              zona=str(v[iz]).strip(), tecnologia_omie=str(v[itec]).strip())
    if len(lista) < 1000:
        raise RuntimeError(f"LISTA_UNIDADES.XLS com só {len(lista)} unidades")
    return lista


def ler_cab(caminho):
    """Nome e lado (V/C) de cada unidade nos cabeçalhos das ofertas de um mês (largura fixa)."""
    nomes = {}
    with zipfile.ZipFile(caminho) as z:
        for n in z.namelist():
            for linha in z.read(n).decode("latin-1").splitlines():
                if len(linha) < 48:
                    continue
                u, d, lado = linha[10:17].strip(), linha[17:47].strip(), linha[47]
                if u:
                    nomes[u] = (d, lado)
    return nomes


def grupo_pelo_nome(nome, lado):
    n = nome.upper()
    if n.startswith("C.N.") or "NUCLEAR" in n:
        return "NUC"
    if n.startswith("C.C.") or "CICLO COMB" in n:
        return "GAS"
    if "BOMBEO" in n or "BOMBAG" in n or n.startswith("C.H.B"):
        return "BOMC" if lado == "C" else "BOMP"     # só compra = a bombear
    if n.startswith("C.T."):
        return "CAR"
    if "COGENER" in n:
        return "TER"
    if n.startswith("C.H.") or "HIDR" in n:
        return "HID"
    if "EOLIC" in n or "EÓLIC" in n:
        return "EOL"
    if "FOTOV" in n or "SOLAR" in n:
        return "SOL"
    if "BIOMAS" in n:
        return "REN"
    return None


def grupo_pelo_codigo(cod):
    if re.search(r"VD\d+$", cod) or re.search(r"RE\d+$", cod) or cod.startswith("GSLRE"):
        return "REP"
    if re.search(r"RM\d+$", cod) or re.search(r"C\d\d$", cod):
        return "PRO"
    if re.fullmatch(r"MI[EP][A-Z]?", cod):     # MIE, MIP, MIEU: as unidades do operador nas ligações entre zonas
        return "INT"
    return None


def classificar(cod, info, nomes_cab, interligacoes):
    """(grupo, origem) de uma unidade. info = linha guardada (com os dados da lista, se já lá esteve)."""
    if cod in interligacoes:
        return "INT", "interligacao"
    tec, tipo, zona = info.get("tecnologia_omie", ""), info.get("tipo_unidade", ""), info.get("zona", "")
    if tec or tipo:
        if zona.upper().startswith("FRONTERA"):
            return "INT", "lista"
        g = TEC_GRUPO.get(tec) or TIPO_GRUPO.get(tipo.upper())
        if g:
            return g, "lista"
        if tipo.upper() in ("GENERICA", "PORFOLIO"):
            return "OUT", "lista"
    nome, lado = nomes_cab.get(cod, (info.get("nome", ""), ""))
    lado = info.get("lado") or lado     # o das curvas (V, C ou CV) manda sobre o do cabeçalho
    g = grupo_pelo_nome(nome, lado) if nome else None
    if g:
        return g, "nome"
    g = grupo_pelo_codigo(cod)
    if g:
        return g, "codigo"
    if lado == "C":
        return "PRO", "lado"
    return "OUT", "sem"


# ============================================================
# Curvas: as unidades casadas ao preço, período a período
# ============================================================
def num(x):
    return float(x.replace(".", "").replace(",", "."))


def ler_dia(fh, dia, precos):
    """Linhas de um dia de mercado (AAAAMMDD) do curva_pbc_uof. Devolve (linhas, unidades vistas
    com tipologia de interligação, lado V/C de cada unidade casada) ou None se o dia estiver incompleto."""
    leitor = io.TextIOWrapper(fh, encoding="latin-1")
    cab = None
    casadas = defaultdict(list)
    interl, lados = set(), {}
    for linha in leitor:
        r = linha.rstrip("\r\n").split(";")
        if cab is None:
            if r and r[0] in ("Hora", "Periodo"):
                cab = r
                col = lambda p: next((i for i, n in enumerate(cab) if n.startswith(p)), None)  # noqa: E731
                iu, ip, ioc, ito, itp = col("Unidad"), col("Precio"), col("Ofertada"), col("Tipo Oferta"), col("Tipolog")
                if None in (iu, ip, ioc, ito):
                    # Já aconteceu (12/08/2026): o ficheiro da OMIE vem sem o preço nem a coluna O/C
                    print(f"  ⚠ {dia}: ficheiro da OMIE sem as colunas do preço ou O/C — dia ignorado")
                    return None
            continue
        if len(r) <= ioc or r[ioc] != "C":
            continue
        casadas[r[0]].append((r[iu].strip(), num(r[ip]), r[ito]))
        lados.setdefault(r[iu].strip(), set()).add(r[ito])
        if itp is not None and len(r) > itp and r[itp][:3] in ("Imp", "Exp"):
            interl.add(r[iu].strip())
    if cab is None or not casadas:
        return None
    d0 = datetime.strptime(dia, "%Y%m%d").replace(tzinfo=MAD).astimezone(UTC)
    qh = any("Q" in p for p in casadas)
    esperado = {False: (23, 24, 25), True: (92, 96, 100)}[qh]
    if len(casadas) not in esperado:
        print(f"  ⚠ {dia}: {len(casadas)} períodos — dia ignorado")
        return None
    linhas = []
    sem_preco = 0
    for p, ofertas in casadas.items():
        if qh:
            h, q = p[1:].split("Q")
            n, minutos = (int(h) - 1) * 4 + int(q), 15
        else:
            n, minutos = int(p), 60
        t = d0 + timedelta(minutes=(n - 1) * minutos)
        k = t.strftime("%Y-%m-%d %H:%M")
        if k not in precos:
            sem_preco += 1
            continue
        ppt, pes = precos[k]
        # /C = oferta de compra (a mesma unidade pode estar dos dois lados: MIE, MIP)
        tok = lambda u, lado: u + "/C" if lado == "C" else u  # noqa: E731
        u_pt = sorted({tok(u, ld) for u, pr, ld in ofertas if abs(pr - ppt) < TOLERANCIA})
        u_es = sorted({tok(u, ld) for u, pr, ld in ofertas if abs(pr - pes) < TOLERANCIA})
        linhas.append(dict(dia_mercado=f"{dia[:4]}-{dia[4:6]}-{dia[6:]}", periodo=n, minutos=minutos,
                           preco_pt=ppt, preco_es=pes, unidades_pt=" ".join(u_pt),
                           unidades_es=" ".join(u_es), data_utc=k))
    if sem_preco:
        print(f"  ⚠ {dia}: {sem_preco} períodos sem preço nos ficheiros do OMIE — dia ignorado")
        return None
    linhas.sort(key=lambda x: x["periodo"])
    return linhas, interl, lados


def processar_mes(caminho, precos):
    dias, interl, lados = {}, set(), {}
    with zipfile.ZipFile(caminho) as z:
        for n in sorted(z.namelist()):
            m = re.search(r"(\d{8})\.\d+$", n)
            if not m:
                continue
            with z.open(n) as fh:
                res = ler_dia(fh, m.group(1), precos)
            if res:
                dias[m.group(1)], i, ld = res
                interl |= i
                for u, v in ld.items():
                    lados.setdefault(u, set()).update(v)
    return dias, interl, lados


# ============================================================
# Ficheiros
# ============================================================
def ler_csv_ano():
    linhas = {}
    for f in sorted(glob.glob(os.path.join(PASTA, "tecnologia_marginal_*.csv"))):
        with open(f, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                r["periodo"], r["minutos"] = int(r["periodo"]), int(r["minutos"])
                r["preco_pt"], r["preco_es"] = float(r["preco_pt"]), float(r["preco_es"])
                linhas[(r["dia_mercado"], r["periodo"])] = r
    return linhas


def ler_unidades():
    f = os.path.join(PASTA, "unidades.csv")
    if not os.path.exists(f):
        return {}
    with open(f, encoding="utf-8", newline="") as fh:
        return {r["codigo"]: r for r in csv.DictReader(fh)}


def escrever_se_mudou(caminho, conteudo):
    if os.path.exists(caminho):
        with open(caminho, encoding="utf-8", newline="") as fh:
            if fh.read() == conteudo:
                return False
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(conteudo)
    os.replace(tmp, caminho)
    return True


def csv_texto(colunas, linhas):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=colunas, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for r in linhas:
        w.writerow(r)
    return buf.getvalue()


def fmt_preco(x):
    return f"{x:.2f}"


# ============================================================
# Resumo para a página
# ============================================================
def novo_bloco():
    return dict(h=0.0, ps=0.0, pv=0.0, pc=0.0, pres=defaultdict(float), rep=defaultdict(float),
                pp=defaultdict(float), lst=[])


def acumular(b, w, preco, grupos):
    b["h"] += w
    b["ps"] += preco * w
    # horas com pelo menos uma oferta de venda / de compra ao preço
    if any(LADO.get(g) == "V" for g in grupos):
        b["pv"] += w
    if any(LADO.get(g) == "C" for g in grupos):
        b["pc"] += w
    for g in grupos:
        b["pres"][g] += w
        b["rep"][g] += w / len(grupos)
        b["pp"][g] += preco * w
    b["lst"].append((preco, w, grupos))


def _r(x, c=2):
    return round(x + 0.0, c)


def fechar_bloco(b, horas=None):
    lst = sorted(b["lst"], key=lambda x: x[0])
    if not lst:
        return dict(h=0.0, preco=None, pv=0.0, pc=0.0, pres={}, rep={}, pm={}, q=dict(lim=[None, None]))
    total = sum(w for _, w, _ in lst) or 1.0
    # Limites dos 25 % mais baratos e mais caros, pesados pela duração dos períodos
    acc, q25, q75 = 0.0, None, None
    for p, w, _ in lst:
        acc += w
        if q25 is None and acc >= 0.25 * total:
            q25 = p
        if q75 is None and acc >= 0.75 * total:
            q75 = p
    faixa = {}
    for nome, f in (("baratas", lambda p: p <= q25), ("caras", lambda p: p >= q75)):
        h, pres, rep = 0.0, defaultdict(float), defaultdict(float)
        for p, w, gs in lst:
            if f(p):
                h += w
                for g in gs:
                    pres[g] += w
                    rep[g] += w / len(gs)
        faixa[nome] = dict(h=_r(h), pres={g: _r(pres[g]) for g in ORDEM if pres[g]},
                           rep={g: _r(rep[g]) for g in ORDEM if rep[g]})
    out = dict(h=_r(b["h"]), preco=_r(b["ps"] / b["h"]) if b["h"] else None, pv=_r(b["pv"]), pc=_r(b["pc"]),
               pres={g: _r(b["pres"][g]) for g in ORDEM if b["pres"][g]},
               rep={g: _r(b["rep"][g]) for g in ORDEM if b["rep"][g]},
               pm={g: _r(b["pp"][g] / b["pres"][g]) for g in ORDEM if b["pres"][g]},
               q=dict(lim=[_r(q25), _r(q75)], **faixa))
    if horas is not None:
        out["horas"] = dict(h=[_r(x) for x in horas["h"]],
                            pres={g: [_r(x) for x in v] for g, v in horas["pres"].items()},
                            rep={g: [_r(x) for x in v] for g, v in horas["rep"].items()})
    return out


def resumo(linhas, primeiro_dia, ultimo_dia, fuso):
    """Por ano, por mês e nos últimos 12 meses (os 365 dias até ao último dia, chave «u12»).
    fuso «pt»: Portugal pelo dia e pela hora de Portugal; «cet»: pelo dia de mercado e pela hora CET. Espanha
    conta sempre pelo dia e pela hora de Espanha (o dia de mercado), como nas outras páginas do site."""
    desde_u12 = (date.fromisoformat(ultimo_dia) - timedelta(days=364)).isoformat()
    anos = defaultdict(lambda: {"PT": novo_bloco(), "ES": novo_bloco()})
    meses = defaultdict(lambda: {"PT": novo_bloco(), "ES": novo_bloco()})
    horas = defaultdict(lambda: {c: dict(h=[0.0] * 24, pres=defaultdict(lambda: [0.0] * 24),
                                         rep=defaultdict(lambda: [0.0] * 24)) for c in ("PT", "ES")})
    for r in linhas:
        w = r["minutos"] / 60
        t = datetime.strptime(r["data_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        t_es = t.astimezone(MAD)                       # o dia de Madrid é o dia de mercado
        t_pt = t.astimezone(LIS) if fuso == "pt" else t_es
        for c, campo, pc, tl in (("PT", "tecnologias_pt", "preco_pt", t_pt), ("ES", "tecnologias_es", "preco_es", t_es)):
            dia = tl.strftime("%Y-%m-%d")
            if dia < primeiro_dia or dia > ultimo_dia:
                continue
            ano, mes, hl = dia[:4], dia[:7], tl.hour
            chaves_ano = (ano, "u12") if dia >= desde_u12 else (ano,)
            gs = r[campo].split("+") if r[campo] else ["SEM"]
            acumular(meses[mes][c], w, r[pc], gs)
            for k in chaves_ano:
                acumular(anos[k][c], w, r[pc], gs)
                hh = horas[k][c]
                hh["h"][hl] += w
                for g in gs:
                    hh["pres"][g][hl] += w
                    hh["rep"][g][hl] += w / len(gs)
    u12 = {c: fechar_bloco(v, horas["u12"][c]) for c, v in anos.pop("u12").items()}
    u12.update(de=desde_u12, ate=ultimo_dia)
    return ({a: {c: fechar_bloco(v[c], horas[a][c]) for c in v} for a, v in sorted(anos.items())},
            {m: {c: fechar_bloco(v[c]) for c in v} for m, v in sorted(meses.items())}, u12)


# ============================================================
# Principal
# ============================================================
def dias_em_falta(linhas):
    dias = sorted({k[0] for k in linhas})
    if not dias:
        return []
    d, fim, tem, falta = date.fromisoformat(dias[0]), date.fromisoformat(dias[-1]), set(dias), []
    while d <= fim:
        if d.isoformat() not in tem:
            falta.append(d.isoformat())
        d += timedelta(days=1)
    return falta


def meses_entre(a, b):
    y, m = a
    while (y, m) <= b:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--desde", help="AAAA-MM: refazer desde esse mês")
    ap.add_argument("--reagregar", action="store_true", help="só os grupos e o resumo")
    ap.add_argument("--cache", help="pasta onde guardar e reutilizar os zips (testes)")
    ap.add_argument("--meses", help="só estes meses, AAAA-MM separados por vírgula (testes)")
    a = ap.parse_args()

    os.makedirs(PASTA, exist_ok=True)
    linhas = ler_csv_ano()
    unidades = ler_unidades()
    interligacoes = {c for c, u in unidades.items() if u.get("origem") == "interligacao"}
    nomes_cab, lados = {}, {}
    hoje = date.today()

    if not a.reagregar:
        if a.meses:
            meses = [tuple(int(x) for x in m.split("-")) for m in a.meses.split(",")]
        else:
            if a.desde:
                if not re.fullmatch(r"\d{4}-\d{2}", a.desde):
                    sys.exit("--desde tem de ser AAAA-MM")
                inicio = max(INICIO, tuple(int(x) for x in a.desde.split("-")))
            elif linhas:
                ultimo = max(k[0] for k in linhas)
                inicio = (int(ultimo[:4]), int(ultimo[5:7]))
                # os dias que faltam no último mês de dados (ficheiro defeituoso na OMIE) voltam a ser pedidos;
                # mais para trás não, para não descarregar o mesmo zip duas vezes por dia para sempre
                limite = (date.fromisoformat(ultimo) - timedelta(days=35)).isoformat()
                falta = [d for d in dias_em_falta(linhas) if d >= limite]
                if falta:
                    inicio = min(inicio, (int(falta[0][:4]), int(falta[0][5:7])))
                    print(f"Dias em falta a tentar outra vez: {', '.join(falta)}")
            else:
                inicio = INICIO
                print("Sem ficheiros: recolha completa desde 2018-01")
            meses = list(meses_entre(inicio, (hoje.year, hoje.month)))

        precos = carregar_precos()
        print(f"Preços: {len(precos)} quartos de hora")
        pasta_tmp = tempfile.mkdtemp(prefix="tecmarg_")
        try:
            lista_xls = os.path.join(a.cache or pasta_tmp, "LISTA_UNIDADES.XLS")
            if not (a.cache and os.path.exists(lista_xls)):
                if not descarregar(URL_LISTA, lista_xls):
                    raise RuntimeError("LISTA_UNIDADES.XLS indisponível")
            lista = ler_lista(lista_xls)
            print(f"Lista da OMIE: {len(lista)} unidades vigentes")
            novos = 0
            for ano, mes in meses:
                uof = ficheiro_mes("curva_pbc_uof", ano, mes, pasta_tmp, a.cache)
                if not uof:
                    print(f"{ano}-{mes:02d}: a OMIE ainda não publicou — fim")
                    break
                cab = ficheiro_mes("cab", ano, mes, pasta_tmp, a.cache)
                if cab:
                    nomes_cab.update(ler_cab(cab))
                dias, interl, ld = processar_mes(uof, precos)
                interligacoes |= interl
                for u, v in ld.items():
                    lados.setdefault(u, set()).update(v)
                for dia, ls in dias.items():
                    for r in ls:
                        linhas[(r["dia_mercado"], r["periodo"])] = r
                novos += sum(len(v) for v in dias.values())
                print(f"{ano}-{mes:02d}: {len(dias)} dias, {sum(len(v) for v in dias.values())} períodos")
                if not a.cache:
                    for f in (uof, cab):
                        if f and os.path.exists(f):
                            os.remove(f)
        finally:
            if not a.cache:
                shutil.rmtree(pasta_tmp, ignore_errors=True)
    else:
        lista = {}

    if not linhas:
        sys.exit("Sem dados — nada escrito")

    # ---- unidades: as que estiveram ao preço, com a melhor informação que houver ----
    vistos = defaultdict(lambda: [None, None])
    for (dia, _), r in linhas.items():
        for u in (r["unidades_pt"] + " " + r["unidades_es"]).split():
            u = u.split("/")[0]
            v = vistos[u]
            v[0] = dia if v[0] is None or dia < v[0] else v[0]
            v[1] = dia if v[1] is None or dia > v[1] else v[1]
    novas = {}
    for u, (prim, ult) in vistos.items():
        ant = unidades.get(u, {})
        # lado: V (venda), C (compra) ou CV, juntando o que já se tinha visto
        lado = "".join(sorted(set(ant.get("lado", "")) | lados.get(u, set())))
        info = dict(codigo=u, nome=ant.get("nome", ""), lado=lado,
                    tipo_unidade=ant.get("tipo_unidade", ""), tecnologia_omie=ant.get("tecnologia_omie", ""),
                    zona=ant.get("zona", ""))
        if u in lista:                       # na lista vigente: manda a lista
            info.update(lista[u])
        elif u in nomes_cab and not info["nome"]:
            info["nome"] = nomes_cab[u][0]
        g, origem = classificar(u, info, nomes_cab, interligacoes)
        info.update(grupo=g, origem=origem, primeira=prim, ultima=ult)
        novas[u] = info
    grupo_de = {u: v["grupo"] for u, v in novas.items()}
    sem_mapa = sorted({v["tecnologia_omie"] for v in novas.values()
                       if v["tecnologia_omie"] and v["tecnologia_omie"] not in TEC_GRUPO})
    if sem_mapa:
        print("⚠ Tecnologias da lista sem grupo (acrescentar a TEC_GRUPO):", sem_mapa)

    # ---- grupos de cada período ----
    ordem = {g: i for i, g in enumerate(ORDEM)}
    sem_classe = {u for u, v in novas.items() if v["origem"] == "sem"}
    por_classificar = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in linhas.values():
        w = r["minutos"] / 60
        for c in ("pt", "es"):
            toks = [t.split("/") for t in r["unidades_" + c].split()]
            gs = {grupo_oferta(grupo_de.get(t[0], "OUT"), "C" if len(t) > 1 else "V") for t in toks}
            r["tecnologias_" + c] = "+".join(sorted(gs, key=ordem.get))
            acc = por_classificar[r["dia_mercado"][:4]][c.upper()]
            acc[0] += w
            if any(t[0] in sem_classe for t in toks):
                acc[1] += w

    # ---- verificações antes de escrever ----
    por_mes = defaultdict(lambda: [0, 0])
    for r in linhas.values():
        m = por_mes[r["dia_mercado"][:7]]
        m[0] += 1
        m[1] += 1 if r["tecnologias_pt"] else 0
    maus = [m for m, (n, c) in por_mes.items() if c < 0.5 * n]
    if maus:
        sys.exit(f"ERRO: meses com menos de metade dos períodos com ofertas ao preço: {maus} — nada escrito")

    # ---- escrever ----
    mudou = []
    por_mes = defaultdict(list)
    for k in sorted(linhas):
        r = dict(linhas[k])
        r["preco_pt"], r["preco_es"] = fmt_preco(r["preco_pt"]), fmt_preco(r["preco_es"])
        por_mes[k[0][:7]].append(r)
    for mes, ls in por_mes.items():
        f = os.path.join(PASTA, f"tecnologia_marginal_{mes}.csv")
        if escrever_se_mudou(f, csv_texto(COLUNAS, ls)):
            mudou.append(os.path.basename(f))
    uni = sorted(novas.values(), key=lambda x: x["codigo"])
    if escrever_se_mudou(os.path.join(PASTA, "unidades.csv"), csv_texto(COLUNAS_UNIDADES, uni)):
        mudou.append("unidades.csv")

    dias_todos = sorted({k[0] for k in linhas})
    # Em hora de Portugal, o último dia completo é a véspera do último dia de mercado (falta-lhe 23h–24h)
    ultimo_pt = (date.fromisoformat(dias_todos[-1]) - timedelta(days=1)).isoformat()
    for fuso, nome, ultimo in (("pt", "resumo.json", ultimo_pt), ("cet", "resumo_cet.json", dias_todos[-1])):
        anos, meses_r, u12 = resumo(linhas.values(), dias_todos[0], ultimo, fuso)
        res = dict(fuso=fuso, primeiro_dia=dias_todos[0], ultimo_dia=ultimo, grupos=ORDEM, nomes=NOMES, lados=LADO,
                   u12=u12, anos=anos, meses=meses_r)
        txt = json.dumps(res, ensure_ascii=False, separators=(",", ":"))
        if escrever_se_mudou(os.path.join(PASTA, nome), txt):
            mudou.append(nome)

    # parte das horas com alguma unidade por classificar ao preço, por ano e país
    por_classificar = {a: {c: _r(100 * x[1] / x[0], 1) if x[0] else 0.0 for c, x in sorted(v.items())}
                       for a, v in sorted(por_classificar.items())}
    origens = defaultdict(int)
    for v in novas.values():
        origens[v["origem"]] += 1
    meta = dict(ultima_data=dias_todos[-1], ultima_data_pt=ultimo_pt, primeira_data=dias_todos[0],
                dias_em_falta=dias_em_falta(linhas),
                resumos={"resumo.json": "Portugal pelo dia e pela hora de Portugal (até ultima_data_pt); Espanha "
                                        "pelo dia e pela hora de Espanha",
                         "resumo_cet.json": "Portugal e Espanha pelo dia de mercado, em hora CET (até ultima_data)"},
                grupos=[dict(id=g, nome=n, lado=ld, descricao=d) for g, n, ld, d in GRUPOS],
                unidades=len(novas), unidades_por_origem=dict(sorted(origens.items())),
                por_classificar_pct=por_classificar,
                fonte="OMIE: curva_pbc_uof (curvas do mercado diário com as unidades, publicadas 90 dias "
                      "depois), cab (nomes das unidades nas ofertas) e LISTA_UNIDADES.XLS; preços dos "
                      "ficheiros data/omie/ deste repositório",
                metodo="Ofertas casadas (venda e compra) com preço igual ao preço marginal de Portugal ou "
                       "de Espanha (±0,005 €/MWh), período a período. Estar ao preço não é o mesmo que "
                       "marcar o preço.")
    # o metadata só muda quando os dados mudam (sem a hora da corrida, que o faria mudar sempre)
    if escrever_se_mudou(os.path.join(PASTA, "metadata.json"),
                         json.dumps(meta, ensure_ascii=False, indent=1) + "\n"):
        mudou.append("metadata.json")

    print(f"Períodos: {len(linhas)} ({dias_todos[0]} a {dias_todos[-1]}); unidades ao preço: {len(novas)} "
          f"{dict(origens)}")
    print("Por classificar (% das horas com alguma unidade por classificar ao preço):", json.dumps(por_classificar))
    print("Ficheiros alterados:", ", ".join(mudou) if mudou else "nenhum")


if __name__ == "__main__":
    main()
