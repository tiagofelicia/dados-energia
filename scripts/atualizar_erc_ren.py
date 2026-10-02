#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
atualizar_erc_ren.py
Publica os Encargos de Regulação Imputados ao Consumo (ERC) da REN, por
período de 15 minutos, nas três vistas que o SIME mostra:

  isp   total, componentes e consumo de mercado, por período
        https://mercado.ren.pt/PT/Electr/InfoMercado/InfSistema/ERC/Paginas/ERC-ISP.aspx
  brp   o total repartido por agente (BRP / unidade de liquidação)
        https://mercado.ren.pt/PT/Electr/InfoMercado/InfSistema/ERC/Paginas/ERC-BRP.aspx
  tipo  o total repartido por tipo de encargo (desvios, aFRR, mFRR, restrições…)
        https://mercado.ren.pt/PT/Electr/InfoMercado/InfSistema/ERC/Paginas/ERC-Tipo.aspx

  Série: 14/03/2024 → último dia publicado (a REN publica com 1 a 2 dias de
  atraso). Antes de 14/03/2024 a API devolve vazio.

DE ONDE VÊM OS DADOS
--------------------
As três páginas são web parts SharePoint que leem uma API JSON em
mercadoservices.ren.pt, com um pedido por dia de mercado. O botão "Exportar
Dados" usa a mesma API (Exports/GetExports) e devolve um xlsx para qualquer
intervalo de datas, mas o xlsx do ISP NÃO traz o consumo de mercado, que o
JSON traz. Por isso o script usa o JSON nas três vistas.

A API pede um cabeçalho X-ApiKey. A chave é a que as próprias páginas enviam:
vem no JavaScript que qualquer visitante recebe, não é um segredo. Se a REN a
mudar, o script pára com "API key is invalid." A nova chave está no ficheiro
mfrrr-val-web-part_*.js da página ERC-ISP (procurar btoa("mercado_). Pode ser
passada na variável de ambiente REN_API_KEY sem mexer no código.

AS TRÊS VISTAS SOMAM O MESMO (QUASE SEMPRE)
-------------------------------------------
O isp é o resumo; os outros dois decompõem-no por quem paga e por porquê. Em
cada período, a soma do brp dá o erc_total_eur do isp, e a soma do tipo dá a
soma das componentes do isp (cada componente bate com o grupo de códigos
respetivo). As duas coincidem — exceto em 137 períodos de fevereiro de 2026 e
de 09/05/2026, em que a REN publica um total abaixo da soma das componentes
(241 371 EUR no conjunto, verificado na série de 14/03/2024 a 30/09/2026). Os
valores ficam como a fonte os dá.

DIA DE MERCADO, NÃO DIA CIVIL
-----------------------------
O dia de mercado é o de Espanha (CET): o período 1 de 30/09 é das 23:00 às
23:15 de 29/09 em hora de Portugal. Os ficheiros mensais repartem-se pelo dia
de mercado, tal como na REN. Um dia tem 96 períodos, 92 no último domingo de
março e 100 no último domingo de outubro. A coluna data_utc do isp (início do
período, em UTC) é a referência sem ambiguidades nesses dois dias.

O BRP FOI HORÁRIO ATÉ 30/09/2025
--------------------------------
O isp e o tipo são de 15 minutos desde o início. O brp não: até 30/09/2025 a
REN reparte o ERC por agente à HORA (24 períodos por dia, 23/25 nos dias de
mudança de hora), e só a partir de 01/10/2025 — a data em que o MIBEL passou a
15 minutos — ao quarto de hora. O período 1 do brp de 2024 é a soma dos
períodos 1 a 4 do isp, não o período 1. Daí a coluna 'minutos' (60 ou 15) no
brp: sem ela, um join com o isp por (data_iso, periodo) dava resultados
errados sem erro nenhum.

FICHEIROS
---------
  data/erc/isp/erc_isp_AAAA-MM.csv     um período por linha (~3 000 linhas/mês)
  data/erc/brp/erc_brp_AAAA-MM.csv     período × agente
  data/erc/tipo/erc_tipo_AAAA-MM.csv   período × código de encargo
  data/erc/erc_tipo_codigos.csv        o que significa cada código do tipo

O brp e o tipo são ficheiros longos e por isso magros: só data_iso e periodo
como tempo (o intervalo e o data_utc estão no isp, que serve de tabela de
períodos), e só as linhas com algum valor. Num dia típico, o tipo tem 87
códigos × 96 períodos e menos de 20 % das células têm valor.

Convenção de formato da casa: UTF-8 com BOM, separador ',', ponto decimal. Os
valores são escritos tal como a API os devolve (texto), sem passar por float:
assim não há arredondamentos nem uma corrida que reescreva o ficheiro com os
mesmos dados em texto diferente.

INCREMENTAL E IDEMPOTENTE
-------------------------
Por omissão, volta a pedir os últimos REVISAO_DIAS dias já guardados (para
apanhar correções da REN) e todos os que faltam até ao último publicado. Cada
dia pedido substitui o que lá estava. Um ficheiro só é reescrito se o texto
mudar, por isso uma corrida sem novidades não produz commit.

USO
---
  python atualizar_erc_ren.py                    # incremental, as três vistas
  python atualizar_erc_ren.py --backfill         # desde 14/03/2024
  python atualizar_erc_ren.py --desde 2025-04-01 --ate 2025-04-30
  python atualizar_erc_ren.py --vistas isp       # só uma (ou duas) das vistas
"""

import argparse
import base64
import csv
import glob
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA = os.path.join(ROOT_DIR, "data", "erc")
FICHEIRO_CODIGOS = os.path.join(PASTA, "erc_tipo_codigos.csv")

BASE_API = "https://mercadoservices.ren.pt/api/"
# A chave das páginas da REN, tal como está no JavaScript delas (antes do btoa).
CHAVE_API = os.environ.get("REN_API_KEY") or "mercado_mL273BtiLeRcqfqBqImWBf5uvPTmdW4VHxb4EeD6"

DATA_INICIAL = date(2024, 3, 14)
REVISAO_DIAS = 7
TIMEOUT = 60
TENTATIVAS = 3
PARALELO = 4

COLUNAS_ISP = ["dia", "data_iso", "periodo", "intervalo", "data_utc", "consumo_mwh",
               "erc_total_eur", "erc_total_eur_mwh",
               "rt_pdbf_eur", "rt_pdbf_eur_mwh",
               "rt_pdvd_eur", "rt_pdvd_eur_mwh",
               "rt_phf_eur", "rt_phf_eur_mwh",
               "banda_afrr_eur", "banda_afrr_eur_mwh",
               "banda_mfrr_eur", "banda_mfrr_eur_mwh",
               "outros_eur", "outros_eur_mwh"]
# Coluna do CSV → campo do JSON, para as colunas numéricas do isp.
CAMPOS_ISP = {
    "consumo_mwh": "CONSUMO_MERCADO",
    "erc_total_eur": "ERC_TOT_VALOR", "erc_total_eur_mwh": "ERC_TOT_UNIT",
    "rt_pdbf_eur": "RTPDBF_VALOR", "rt_pdbf_eur_mwh": "RTPDBF_UNIT",
    "rt_pdvd_eur": "RTPDVD_VALOR", "rt_pdvd_eur_mwh": "RTPDVD_UNIT",
    "rt_phf_eur": "RTPHF_VALOR", "rt_phf_eur_mwh": "RTPHF_UNIT",
    "banda_afrr_eur": "BAFRR_VALOR", "banda_afrr_eur_mwh": "BAFRR_UNIT",
    "banda_mfrr_eur": "BMFRR_VALOR", "banda_mfrr_eur_mwh": "BMFRR_UNIT",
    "outros_eur": "OUTROS_VALOR", "outros_eur_mwh": "OUTROS_UNIT",
}
COLUNAS_BRP = ["data_iso", "periodo", "minutos", "brp", "unidade_liquidacao", "erc_eur"]
COLUNAS_TIPO = ["data_iso", "periodo", "codigo", "quantidade", "valor_eur"]
COLUNAS_CODIGOS = ["codigo", "tipo_id", "tipo", "subtipo", "tipo_en", "subtipo_en", "unidade"]

VISTAS = {
    "isp": dict(controlador="ERCPeriodo", ultimo="GetERCLatest", dia="GetERCByDay",
                colunas=COLUNAS_ISP),
    "brp": dict(controlador="ERCBRP", ultimo="GetERCBRPLatest", dia="GetERCBRPByDay",
                colunas=COLUNAS_BRP),
    "tipo": dict(controlador="ERCTipo", ultimo="GetERCTipoLatest", dia="GetERCTipoByDay",
                 colunas=COLUNAS_TIPO),
}


class ChaveInvalida(Exception):
    pass


# ============================================================
# API
# ============================================================

_local = threading.local()


def _sessao():
    # Uma sessão por thread: o requests.Session não é garantidamente
    # thread-safe e assim cada thread mantém a sua ligação keep-alive.
    if not hasattr(_local, "sessao"):
        s = requests.Session()
        s.headers.update({
            "X-ApiKey": base64.b64encode(CHAVE_API.encode()).decode(),
            "Accept": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        })
        _local.sessao = s
    return _local.sessao


def pedir(controlador, metodo, **params):
    """GET à API. Devolve o JSON já descodificado.

    A API embrulha a resposta duas vezes: o corpo é uma string JSON cujo
    conteúdo é, por sua vez, o JSON dos dados.
    """
    url = f"{BASE_API}{controlador}/{metodo}"
    params = {"language": "PT", **params}
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resp = _sessao().get(url, params=params, timeout=TIMEOUT)
            if "API key is invalid" in resp.text[:100]:
                raise ChaveInvalida(
                    "a REN rejeitou a chave da API (\"API key is invalid.\"). "
                    "A chave nova está no JavaScript da página ERC-ISP — ver o "
                    "cabeçalho deste script — e pode ser passada em REN_API_KEY.")
            resp.raise_for_status()
            dados = resp.json()
            if isinstance(dados, str):
                dados = json.loads(dados)
            if isinstance(dados, dict) and "Message" in dados:
                raise RuntimeError(dados["Message"])
            return dados
        except ChaveInvalida:
            raise
        except Exception as e:
            ultimo_erro = e
            if tentativa < TENTATIVAS:
                time.sleep(2 ** tentativa)
    raise RuntimeError(f"{metodo} {params}: {ultimo_erro}")


def ultimo_publicado(vista):
    v = VISTAS[vista]
    return date.fromisoformat(pedir(v["controlador"], v["ultimo"])["latestDate"])


def pedir_dia(vista, d):
    v = VISTAS[vista]
    return pedir(v["controlador"], v["dia"],
                 dayQuery=d.day, monthQuery=d.month, yearQuery=d.year)


# ============================================================
# Conversão
# ============================================================

_NUMERO = re.compile(r"^-?\d+(\.\d+)?$")


def num(v):
    """Texto numérico da API → texto do CSV. Vazio se não houver valor.

    Mantém o texto original; só acrescenta o zero a '.5' / '-.5', a forma
    que o Oracle usa por omissão, para o CSV ser legível por qualquer parser.
    """
    if v is None:
        return ""
    s = str(v).strip()
    if s == "":
        return ""
    s = re.sub(r"^(-?)\.", r"\g<1>0.", s)
    if not _NUMERO.match(s):
        float(s)    # aceita notação científica; rebenta se não for número
    return s


def _dia_pt(iso):
    a, m, d = iso.split("-")
    return f"{d}/{m}/{a}"


def _intervalo(hora_ini_fim):
    """'23:00 - 23:15' → '[23:00-23:15[' (o formato dos outros datasets)."""
    ini, fim = [p.strip() for p in hora_ini_fim.split("-")]
    return f"[{ini}-{fim}["


def _minutos(hora_ini_fim):
    """'23:00 - 00:00' → 60; '23:00 - 23:15' → 15."""
    ini, fim = [p.strip() for p in hora_ini_fim.split("-")]
    para_min = lambda s: int(s[:2]) * 60 + int(s[3:5])
    return (para_min(fim) - para_min(ini)) % 1440


def linhas_isp(dados):
    return [[_dia_pt(r["DIA_MERC"]), r["DIA_MERC"], str(r["PERIODO"]),
             _intervalo(r["HORA_INI_FIM"]), r["DATA_UTC"]]
            + [num(r[CAMPOS_ISP[c]]) for c in COLUNAS_ISP[5:]]
            for r in dados]


def linhas_brp(dados):
    # O campo chama-se BSP na API, mas a página e o xlsx chamam-lhe BRP.
    return [[r["DIA_MERC"], str(r["PERIODO"]), str(_minutos(r["HORA_INI_FIM"])),
             r["BSP"], r["UNI_LIQ"], num(r["VALOR"])]
            for r in dados if num(r["VALOR"]) != ""]


def linhas_tipo(dados):
    out = []
    for r in dados:
        q, v = num(r["QTY"]), num(r["VALOR"])
        if q != "" or v != "":
            out.append([r["DIA_MERC"], str(r["PERIODO"]), r["INF_ID"], q, v])
    return out


def _limpar_rotulo(s):
    # A API devolve "Generation Curtailment ¿ Non-compliance": um travessão
    # que se perdeu na conversão de carateres do lado da REN.
    return (s or "").replace(" ¿ ", " – ").strip()


def codigos_tipo(dados):
    """Dicionário codigo → [tipo_id, tipo, subtipo, tipo_en, subtipo_en, unidade]."""
    cod = {}
    for r in dados:
        unidade = (r.get("UNID_QTY") or "").replace("MWH", "MWh")
        cod[r["INF_ID"]] = [r.get("TIPOID") or "", _limpar_rotulo(r.get("TIPO_PT")),
                            _limpar_rotulo(r.get("SUBTIPO_PT")),
                            _limpar_rotulo(r.get("TIPO_EN")),
                            _limpar_rotulo(r.get("SUBTIPO_EN")), unidade]
    return cod


CONVERSORES = {"isp": linhas_isp, "brp": linhas_brp, "tipo": linhas_tipo}


def periodos_esperados(d, minutos=15):
    """Períodos de um dia de mercado: 96 a 15 min (24 à hora), menos uma hora
    no último domingo de março e mais uma no último domingo de outubro."""
    por_hora = 60 // minutos
    if d.month in (3, 10) and d.weekday() == 6 and (d + timedelta(days=7)).month != d.month:
        return (23 if d.month == 3 else 25) * por_hora
    return 24 * por_hora


# ============================================================
# Ficheiros
# ============================================================

def caminho_mes(vista, ano_mes):
    return os.path.join(PASTA, vista, f"erc_{vista}_{ano_mes}.csv")


def ficheiros_vista(vista):
    return sorted(glob.glob(os.path.join(PASTA, vista, f"erc_{vista}_????-??.csv")))


def ler_csv(caminho):
    """(texto, cabeçalho, linhas) ou (None, None, []) se não existir."""
    if not os.path.exists(caminho):
        return None, None, []
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        texto = f.read()
    tabela = list(csv.reader(io.StringIO(texto)))
    return texto, (tabela[0] if tabela else None), tabela[1:]


def escrever_csv(caminho, colunas, linhas, texto_antigo):
    """Escreve só se o texto mudar. Devolve True se escreveu."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(colunas)
    w.writerows(linhas)
    novo = buf.getvalue()
    if novo == texto_antigo:
        return False
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        f.write(novo)
    return True


def ultima_data_guardada(vista):
    ficheiros = ficheiros_vista(vista)
    if not ficheiros:
        return None
    _, cab, linhas = ler_csv(ficheiros[-1])
    if not linhas:
        return None
    i = cab.index("data_iso")
    return date.fromisoformat(max(l[i] for l in linhas))


def gravar_mes(vista, ano_mes, por_dia):
    """Junta os dias recolhidos ao ficheiro do mês. Devolve (linhas, escreveu).

    Os dias recolhidos substituem por inteiro os que lá estavam. A ordenação é
    estável por (data_iso, periodo): dentro de cada período fica a ordem da
    REN, que é a mesma das páginas.
    """
    colunas = VISTAS[vista]["colunas"]
    caminho = caminho_mes(vista, ano_mes)
    texto, cab, antigas = ler_csv(caminho)
    if cab is not None and cab != colunas:
        raise RuntimeError(f"{caminho}: cabeçalho inesperado {cab}")
    i_data = colunas.index("data_iso")
    i_per = colunas.index("periodo")
    linhas = [l for l in antigas if l[i_data] not in por_dia]
    for d in sorted(por_dia):
        linhas.extend(por_dia[d])
    linhas.sort(key=lambda l: (l[i_data], int(l[i_per])))
    return len(linhas), escrever_csv(caminho, colunas, linhas, texto)


def gravar_codigos(novos):
    """Upsert no dicionário: os códigos novos vão para o fim, os rótulos
    existentes são atualizados com os mais recentes."""
    texto, cab, linhas = ler_csv(FICHEIRO_CODIGOS)
    atuais = {l[0]: l[1:] for l in linhas}
    ordem = [l[0] for l in linhas] + [c for c in novos if c not in atuais]
    atuais.update(novos)
    escreveu = escrever_csv(FICHEIRO_CODIGOS, COLUNAS_CODIGOS,
                            [[c] + atuais[c] for c in ordem], texto)
    return len(ordem), len(ordem) - len(linhas), escreveu


# ============================================================
# Recolha
# ============================================================

def recolher_dia(vista, d):
    """Pede um dia e converte-o logo, para não guardar o JSON em memória."""
    dados = pedir_dia(vista, d)
    if not dados:
        return d, None, None, "sem dados"
    linhas = CONVERSORES[vista](dados)
    codigos = codigos_tipo(dados) if vista == "tipo" else None
    periodos = {int(r["PERIODO"]) for r in dados}
    # A resolução lê-se dos próprios dados (o brp foi horário até 30/09/2025)
    # em vez de vir de uma data fixa no código.
    duracoes = sorted({_minutos(r["HORA_INI_FIM"]) for r in dados})
    aviso = None
    if duracoes not in ([15], [60]):
        aviso = f"durações de período inesperadas: {duracoes} min"
    else:
        esperado = periodos_esperados(d, duracoes[0])
        if periodos != set(range(1, esperado + 1)):
            aviso = f"{len(periodos)} períodos (esperados {esperado})"
    return d, linhas, codigos, aviso


def processar_vista(vista, inicio, fim, paralelo):
    """Recolhe [inicio, fim] mês a mês e grava cada mês logo a seguir.

    Mês a mês para a memória não crescer com o backfill: um mês do tipo são
    ~250 000 registos JSON, a série inteira passaria dos 7 milhões.
    """
    dias = [inicio + timedelta(days=i) for i in range((fim - inicio).days + 1)]
    meses = {}
    for d in dias:
        meses.setdefault(d.strftime("%Y-%m"), []).append(d)

    total_dias, sem_dados, falhados, avisos = 0, [], [], []
    codigos = {}
    with ThreadPoolExecutor(max_workers=paralelo) as ex:
        for ano_mes, dias_mes in meses.items():
            futuros = [(d, ex.submit(recolher_dia, vista, d)) for d in dias_mes]
            por_dia = {}
            for d, fut in futuros:
                try:
                    _, linhas, cods, aviso = fut.result()
                except ChaveInvalida:
                    raise
                except Exception as e:
                    falhados.append((d, e))
                    print(f"  ❌ {d}: {e}")
                    continue
                if linhas is None:
                    sem_dados.append(d)
                    continue
                if aviso:
                    avisos.append((d, aviso))
                    print(f"  ⚠️  {d}: {aviso}")
                por_dia[d.isoformat()] = linhas
                if cods:
                    codigos.update(cods)
            if not por_dia:
                continue
            n, escreveu = gravar_mes(vista, ano_mes, por_dia)
            total_dias += len(por_dia)
            print(f"  {ano_mes}: {len(por_dia):2d} dia(s) recolhido(s) · "
                  f"{n:,} linhas no ficheiro · "
                  f"{'gravado' if escreveu else 'sem alterações'}")

    if codigos:
        n, novos, escreveu = gravar_codigos(codigos)
        print(f"  dicionário de códigos: {n} códigos"
              f"{f' ({novos} novos)' if novos else ''} · "
              f"{'gravado' if escreveu else 'sem alterações'}")
    return total_dias, sem_dados, falhados, avisos


# ============================================================
# Principal
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="Publica os ERC da REN (isp, brp, tipo) em CSV mensais.")
    p.add_argument("--vistas", nargs="+", choices=list(VISTAS), default=list(VISTAS))
    p.add_argument("--backfill", action="store_true",
                   help=f"recolher desde {DATA_INICIAL.isoformat()}")
    p.add_argument("--desde", type=date.fromisoformat, metavar="AAAA-MM-DD")
    p.add_argument("--ate", type=date.fromisoformat, metavar="AAAA-MM-DD")
    p.add_argument("--revisao", type=int, default=REVISAO_DIAS, metavar="N",
                   help=f"dias já guardados a voltar a pedir (omissão: {REVISAO_DIAS})")
    p.add_argument("--paralelo", type=int, default=PARALELO, metavar="N",
                   help=f"pedidos em simultâneo (omissão: {PARALELO})")
    args = p.parse_args()

    print(f"⚡ ERC da REN — vistas: {', '.join(args.vistas)}")
    algum_ok, houve_falhas = False, False
    for vista in args.vistas:
        try:
            publicado = ultimo_publicado(vista)
        except ChaveInvalida as e:
            print(f"❌ {e}")
            sys.exit(1)
        except Exception as e:
            print(f"\n❌ {vista}: não foi possível saber o último dia publicado: {e}")
            houve_falhas = True
            continue

        guardado = ultima_data_guardada(vista)
        if args.desde:
            inicio = max(args.desde, DATA_INICIAL)
        elif args.backfill or guardado is None:
            if guardado is None and not args.backfill:
                print(f"ℹ️  {vista}: ainda sem ficheiros — a fazer backfill completo.")
            inicio = DATA_INICIAL
        else:
            inicio = max(DATA_INICIAL, guardado - timedelta(days=args.revisao))
        fim = min(args.ate, publicado) if args.ate else publicado

        print(f"\n📥 {vista}: {inicio} → {fim}  "
              f"(último publicado: {publicado}; último guardado: {guardado or '—'})")
        if inicio > fim:
            print("   nada a recolher.")
            continue

        t0 = time.time()
        try:
            n, sem_dados, falhados, avisos = processar_vista(vista, inicio, fim, args.paralelo)
        except ChaveInvalida as e:
            print(f"❌ {e}")
            sys.exit(1)
        algum_ok = algum_ok or n > 0
        houve_falhas = houve_falhas or bool(falhados)
        print(f"   {n} dia(s) em {time.time() - t0:.0f}s"
              + (f" · {len(sem_dados)} sem dados: {sem_dados[0]} → {sem_dados[-1]}"
                 if sem_dados else "")
              + (f" · {len(avisos)} com períodos fora do esperado" if avisos else ""))
        if falhados:
            print(f"   ⚠️  {len(falhados)} dia(s) falharam: "
                  + ", ".join(d.isoformat() for d, _ in falhados)
                  + " — repetir com --desde/--ate.")

    print(f"\n{'✅' if not houve_falhas else '⚠️ '} Concluído "
          f"({datetime.now():%Y-%m-%d %H:%M}).")
    # Como no atualizar_eua_co2.py: só falha o workflow se nada se aproveitou,
    # para uma falha pontual não impedir o commit do que correu bem.
    if houve_falhas and not algum_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
