#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validar_dados.py
Verifica se o que este repositório publica está de facto bom para publicar.

PARA QUE SERVE
--------------
Os bots correm sozinhos e ficam todos verdes desde que o script não rebente.
Isso não chega: um ficheiro pode parar de ser escrito, uma fonte pode mudar o
nome de uma coluna, uma chave mal escolhida pode apagar linhas em silêncio.
Nada disto levanta excepção — e portanto nada disto apareceu alguma vez no
Actions.

Já aconteceu três vezes:

  * O omip_historico_<ANO>.csv esteve SETE DIAS sem ser actualizado, entre 10 e
    17/09/2026, porque o step que o escrevia ficou órfão ao reorganizar o
    workflow. Todos os workflows verdes, nenhum aviso, e o site a servir a mesma
    sessão OMIP durante uma semana.
  * O mibgas_ttf_spread.csv perdeu 684 cotações (6% do ficheiro) por a chave de
    deduplicação não incluir a entrega: os produtos de estação W e S cotam duas
    estações ao mesmo tempo e colapsavam numa linha só.
  * O preco_ultimo do mibgas_futuros.csv foi documentado como a coluna de
    referência quando está 0% preenchido antes de 2023.

O manifesto já declara a tolerância de atraso de cada dataset — o campo
tolerancia_dias, um por entrada, escolhido à mão para cada cadência. Só que até
hoje ninguém o lia. Este script é quem passa a lê-lo.

O QUE VERIFICA
--------------
  existência   os ficheiros do padrão existem e não são de zero bytes
  frescura     ultima_data + tolerancia_dias >= hoje
  colunas      as colunas de que o site depende continuam lá
  chave        a chave natural não tem duplicados
  calendário   96 quartos por dia, 92 e 100 nos dois dias de mudança de hora
  cobertura    nenhuma coluna deixou de vir preenchida

As duas primeiras aplicam-se a TODOS os datasets, porque saem do manifesto. As
outras quatro só aos que estão declarados em REGRAS: exigem conhecer o ficheiro,
e mais vale não verificar do que verificar mal.

FALSOS ALARMES
--------------
Um validador que grita sem razão é pior do que nenhum — deixa de se ler, e
depois não se lê no dia em que tem razão. Por isso:

  * ERRO só para o que é inequívoco: ficheiro em falta, prazo ultrapassado,
    coluna que desapareceu, chave duplicada, dia com linhas a mais. Sai !=0.
  * AVISO para o que pode ser legítimo: um dia incompleto, uma coluna a
    esvaziar-se. Fica no log e não chumba nada.
  * O último dia de cada ficheiro nunca é verificado ao calendário: está quase
    sempre a meio.
  * A cobertura compara o passado com as últimas 30 sessões, não com 100%.
    Vários índices chegam com um ou dois dias de atraso, e isso é normal.

USO
---
  python validar_dados.py                   # tudo menos os datasets grandes
  python validar_dados.py --rapido          # só existência e frescura
  python validar_dados.py --id gas-futuros  # só um dataset (repetível)
  python validar_dados.py --lentos          # inclui histórico, ENTSO-E e mapas
  python validar_dados.py --so-avisar       # reporta tudo e sai sempre com 0
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

# Reutiliza o registo e a deteção de data do manifesto em vez de os repetir.
# Saber qual é a "última data real" de cada dataset é a parte difícil — o
# omie_dados_atuais.csv vai até 2027, mas 111 desses dias são estimativas dos
# futuros OMIP e não contam para efeitos de frescura — e essa inteligência já
# vive no gerar_manifest.py. Duplicá-la aqui garantiria apenas que um dia as
# duas cópias diriam coisas diferentes.
from gerar_manifest import REGISTO, ficheiros_de, ultima_data  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

TZ_PT = ZoneInfo("Europe/Lisbon")
RE_DIA = re.compile(r"^\d{2}/\d{2}/\d{4}$")


# ============================================================
# Regras — o que não se infere do manifesto
# ============================================================
# colunas     as que o site e os consumidores usam. Não é o cabeçalho todo de
#             propósito: uma coluna nova é bem-vinda, uma coluna a menos não.
# chave       a chave natural. Lista, ou dicionário {ficheiro: chave} quando o
#             dataset é um padrão com ficheiros de grão diferente.
# calendario  coluna de dia em DD/MM/AAAA a que se aplica a regra dos 96 quartos.
# cobertura   coluna de data ISO que define a janela recente.
# tolerante   o ficheiro tem blocos concatenados no fim e precisa do parser lento.

REGRAS = {
    # Os dois "atuais" trazem blocos TABELA_* no fim, com outro número de
    # campos; sem tolerante=True o pandas rebenta a meio.
    "omie-atuais": dict(
        colunas=["dia", "hora", "intervalo", "preco_pt", "preco_es"],
        calendario="dia", tolerante=True),
    "producao-atuais": dict(
        colunas=["dia", "hora", "intervalo", "Consumo"],
        calendario="dia", tolerante=True),
    "producao-bombagem": dict(
        colunas=["dia", "producao_bombagem_gwh"], chave=["dia"]),

    # Os históricos são ficheiros fechados: já não mudam, e por isso não vale a
    # pena relê-los a cada corrida do CI. Mas é neles que a verificação de
    # calendário rende mais numa auditoria pontual — 16 anos de cada vez, com a
    # mudança de hora a cair em dias diferentes todos os anos. Daí o --lentos.
    "omie-historico": dict(
        colunas=["dia", "hora", "intervalo", "preco_pt", "preco_es"],
        calendario="dia"),
    "producao-historico": dict(
        colunas=["dia", "hora", "intervalo", "Hídrica", "Eólica", "Solar"],
        calendario="dia"),
    "omip-historico": dict(
        colunas=["Data", "Zona", "Contrato", "Valor"],
        chave=["Data", "Zona", "Contrato"]),

    "gas-mibgas": dict(
        colunas=["dia", "data_iso", "mibgas_pt", "mibgas_es"],
        chave=["data_iso"], cobertura="data_iso"),
    "gas-futuros": dict(
        colunas=["dia", "data_iso", "produto", "horizonte", "rotulo",
                 "entrega_inicio", "preco_ultimo", "preco_referencia"],
        # Sem entrega_inicio na chave perdiam-se as cotações de estação: os
        # produtos W e S cotam o inverno que vem e o seguinte na mesma sessão.
        chave=["data_iso", "produto", "entrega_inicio"], cobertura="data_iso"),
    "gas-ttf-spread": dict(
        colunas=["data_iso", "produto", "entrega_inicio", "spread"],
        chave=["data_iso", "produto", "entrega_inicio"], cobertura="data_iso"),

    "emissoes-eua": dict(
        colunas=["data_iso", "leilao", "contrato", "tipo", "preco_eur_t", "volume_tco2"],
        # Há dias com dois leilões e preços diferentes: a chave tem de os separar.
        chave=["data_iso", "leilao", "contrato"], cobertura="data_iso"),

    # Padrões cujos ficheiros têm grão diferente: cada um com a sua chave.
    "agregados-omie": dict(
        chave={"omie_diario.csv": ["data_iso"], "omie_mensal.csv": ["mes"],
               "omie_anual.csv": ["ano"]}),
    "agregados-producao": dict(
        chave={"producao_diario.csv": ["data_iso"], "producao_mensal.csv": ["mes"],
               "producao_anual.csv": ["ano"]}),
    "emissoes": dict(
        chave={"intensidade_diaria.csv": ["data_iso"], "intensidade_mensal.csv": ["mes"],
               "intensidade_anual.csv": ["ano"],
               "intensidade_perfil_horario.csv": ["ano", "hora"]}),
}

# Datasets pesados, verificados a fundo só com --lentos. O histórico são 33 MB
# de OMIE e 127 MB de ENTSO-E: relê-los a cada corrida do CI não paga o que dá,
# porque são ficheiros fechados que já não mudam. A frescura desses continua a
# ser verificada sempre — é barata e é a que interessa.
LENTOS = {"omie-historico", "producao-historico", "producao-entsoe",
          "mapas-precos", "mapas-producao"}


class Relatorio:
    """Acumula o que se encontrou. ERRO chumba a corrida, AVISO não."""

    def __init__(self):
        self.erros = []
        self.avisos = []
        self._atual = None

    def dataset(self, ident):
        self._atual = ident

    def erro(self, msg):
        self.erros.append((self._atual, msg))
        print(f"  ERRO   {msg}")

    def aviso(self, msg):
        self.avisos.append((self._atual, msg))
        print(f"  aviso  {msg}")


# ============================================================
# Auxiliares
# ============================================================

def quartos_esperados(d):
    """
    Quartos-de-hora de um dia civil português: 96, ou 92/100 nos dias em que o
    relógio muda. Calculado com o fuso e não fixado a 96 justamente para que um
    dia de 100 linhas em Junho continue a contar como erro.
    """
    ini = datetime(d.year, d.month, d.day, tzinfo=TZ_PT)
    fim = datetime(d.year, d.month, d.day, tzinfo=TZ_PT) + timedelta(days=1)
    horas = (fim.astimezone(timezone.utc) - ini.astimezone(timezone.utc)).total_seconds() / 3600
    return int(horas * 4)


def ler_csv(caminho, colunas=None, tolerante=False):
    kw = dict(encoding="utf-8-sig", dtype=str)
    if colunas:
        kw["usecols"] = colunas
    if tolerante:
        kw.update(engine="python", on_bad_lines="skip")
    return pd.read_csv(caminho, **kw)


def amostra(valores, n=3):
    v = list(valores)
    resto = f" (+{len(v) - n})" if len(v) > n else ""
    return ", ".join(str(x) for x in v[:n]) + resto


def csvs(ficheiros):
    return [f for f in ficheiros if f.endswith(".csv")]


# ============================================================
# Verificações
# ============================================================

def ver_existencia(entrada, ficheiros, rel):
    if not ficheiros:
        rel.erro(f"nenhum ficheiro em {entrada['caminho']}")
        return False
    vazios = [os.path.basename(f) for f in ficheiros if os.path.getsize(f) == 0]
    if vazios:
        rel.erro(f"ficheiro(s) de 0 bytes: {amostra(vazios)}")
        return False
    return True


def ver_frescura(entrada, ficheiros, hoje, rel):
    """O coração do script: é isto que teria apanhado o OMIP parado 7 dias."""
    if entrada["deteccao"] == "estatico":
        return None
    try:
        ud = ultima_data(entrada, ficheiros)
    except Exception as e:
        rel.erro(f"falhou a determinar a última data: {type(e).__name__}: {e}")
        return None
    if not ud:
        rel.erro("não foi possível determinar a última data")
        return None

    atraso = (hoje - date.fromisoformat(ud)).days
    tol = entrada["tolerancia_dias"]
    if atraso > tol:
        rel.erro(f"parado há {atraso} dias — última data {ud}, "
                 f"tolerância {tol} ({entrada['cadencia']})")
    elif atraso == tol:
        rel.aviso(f"no limite: {atraso} dias desde {ud}, tolerância {tol}")
    return ud


def ver_colunas(regra, ficheiros, rel):
    exigidas = regra.get("colunas")
    if not exigidas:
        return
    for f in csvs(ficheiros):
        nome = os.path.basename(f)
        try:
            presentes = list(pd.read_csv(f, encoding="utf-8-sig", nrows=0).columns)
        except Exception as e:
            rel.erro(f"{nome}: cabeçalho ilegível ({type(e).__name__})")
            continue
        em_falta = [c for c in exigidas if c not in presentes]
        if em_falta:
            rel.erro(f"{nome}: colunas em falta: {', '.join(em_falta)}")


def ver_chave(regra, ficheiros, rel):
    chave = regra.get("chave")
    if not chave:
        return
    for f in csvs(ficheiros):
        nome = os.path.basename(f)
        cols = chave.get(nome) if isinstance(chave, dict) else chave
        if not cols:
            continue
        try:
            d = ler_csv(f, colunas=cols, tolerante=regra.get("tolerante", False))
        except Exception as e:
            rel.erro(f"{nome}: não abriu para verificar a chave "
                     f"({type(e).__name__}: {e})")
            continue
        dup = d[d.duplicated(subset=cols, keep=False)]
        if len(dup):
            exemplos = [" | ".join(r) for r in dup[cols].head(3).astype(str).values]
            rel.erro(f"{nome}: {len(dup)} linhas com chave "
                     f"({', '.join(cols)}) repetida — exemplos: {amostra(exemplos)}")


def ver_calendario(regra, ficheiros, ultima, rel):
    """
    96 quartos por dia, 92 e 100 nos dois dias de mudança de hora.

    Não verifica o último dia do ficheiro (está a meio) nem os dias posteriores
    à última data real — no OMIE esses são estimativas dos futuros OMIP, com
    dias completos que nada têm a ver com publicação de mercado.
    """
    col = regra.get("calendario")
    if not col:
        return
    limite = date.fromisoformat(ultima) if ultima else None
    for f in csvs(ficheiros):
        nome = os.path.basename(f)
        try:
            d = ler_csv(f, colunas=[col], tolerante=regra.get("tolerante", False))
        except Exception as e:
            rel.erro(f"{nome}: não abriu para verificar o calendário "
                     f"({type(e).__name__})")
            continue
        # Os blocos TABELA_* trazem lixo nesta coluna ("Contrato", "FPB Wk39-26"):
        # só passam as linhas com uma data DD/MM/AAAA a sério.
        dias = d[d[col].fillna("").str.match(RE_DIA)][col]
        if dias.empty:
            rel.aviso(f"{nome}: nenhuma data válida na coluna '{col}'")
            continue
        contagem = dias.value_counts()
        datas = {s: datetime.strptime(s, "%d/%m/%Y").date() for s in contagem.index}
        ultimo_no_ficheiro = max(datas.values())

        curtos, longos = [], []
        for texto, n in contagem.items():
            dia = datas[texto]
            # Saltar só o dia vivo — o da última data real, que está a meio — e
            # o que vem depois dele. O 31/12 de um ficheiro histórico fechado
            # está completo e tem de ser verificado como qualquer outro; saltar
            # o último dia de CADA ficheiro deixava 32 dias por verificar.
            if limite is not None:
                if dia >= limite:
                    continue
            elif dia == ultimo_no_ficheiro:
                continue
            esperado = quartos_esperados(dia)
            if n < esperado:
                curtos.append(f"{texto} ({n}/{esperado})")
            elif n > esperado:
                longos.append(f"{texto} ({n}/{esperado})")

        # A mais é sempre erro: são linhas duplicadas ou de outro dia.
        if longos:
            rel.erro(f"{nome}: {len(longos)} dia(s) com linhas a mais: "
                     f"{amostra(sorted(longos))}")
        # A menos pode ser uma falha de publicação da fonte, que se recupera
        # sozinha na corrida seguinte. Avisa, não chumba.
        if curtos:
            rel.aviso(f"{nome}: {len(curtos)} dia(s) incompleto(s): "
                      f"{amostra(sorted(curtos))}")


def ver_cobertura(regra, ficheiros, rel, janela=30):
    """
    Uma coluna que estava preenchida e deixou de vir. É o sintoma de a fonte ter
    mudado sem avisar, e não levanta excepção nenhuma: o ficheiro continua a ser
    escrito, só que com uma coluna vazia.

    Compara com as últimas sessões e não com o ficheiro todo porque várias
    destas colunas têm cobertura histórica baixa por razões legítimas — o
    preco_ultimo do MIBGAS só existe a partir de 2023, e isso não é defeito.
    """
    col_data = regra.get("cobertura")
    if not col_data:
        return
    for f in csvs(ficheiros):
        nome = os.path.basename(f)
        try:
            d = ler_csv(f, tolerante=regra.get("tolerante", False))
        except Exception:
            continue  # ver_chave e ver_colunas já terão reportado
        if col_data not in d.columns:
            continue
        sessoes = sorted(x for x in d[col_data].dropna().unique())[-janela:]
        if not sessoes:
            continue
        recente = d[d[col_data].isin(sessoes)]
        mortas = [c for c in d.columns
                  if d[c].notna().any() and not recente[c].notna().any()]
        if mortas:
            rel.aviso(f"{nome}: coluna(s) preenchidas no passado e vazias nas "
                      f"últimas {len(sessoes)} sessões: {', '.join(mortas)}")


# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="Valida os datasets publicados contra o que o manifesto promete.")
    p.add_argument("--rapido", action="store_true",
                   help="só existência e frescura; salta chave, calendário e cobertura")
    p.add_argument("--id", action="append", metavar="ID",
                   help="validar só este dataset (repetível)")
    p.add_argument("--lentos", action="store_true",
                   help="verificar a fundo também os datasets grandes")
    p.add_argument("--so-avisar", dest="so_avisar", action="store_true",
                   help="reportar tudo mas sair sempre com 0")
    args = p.parse_args()

    if args.id:
        desconhecidos = set(args.id) - {e["id"] for e in REGISTO}
        if desconhecidos:
            print(f"id(s) desconhecido(s): {', '.join(sorted(desconhecidos))}",
                  file=sys.stderr)
            return 2

    hoje = datetime.now(timezone.utc).date()
    rel = Relatorio()
    entradas = [e for e in REGISTO if not args.id or e["id"] in args.id]

    print(f"validar_dados — {len(entradas)} datasets · hoje {hoje.isoformat()} (UTC)")
    print()

    for entrada in entradas:
        ident = entrada["id"]
        rel.dataset(ident)
        antes = len(rel.erros) + len(rel.avisos)
        print(ident)

        ficheiros = ficheiros_de(entrada["caminho"])
        if not ver_existencia(entrada, ficheiros, rel):
            continue
        ultima = ver_frescura(entrada, ficheiros, hoje, rel)

        regra = REGRAS.get(ident)
        if regra and not args.rapido and (args.lentos or ident not in LENTOS):
            ver_colunas(regra, ficheiros, rel)
            ver_chave(regra, ficheiros, rel)
            ver_calendario(regra, ficheiros, ultima, rel)
            ver_cobertura(regra, ficheiros, rel)

        if len(rel.erros) + len(rel.avisos) == antes:
            n = len(ficheiros)
            detalhe = f"{n} ficheiros" if n > 1 else os.path.basename(ficheiros[0])
            print(f"  ok     {detalhe}" + (f" · até {ultima}" if ultima else ""))

    print()
    print("=" * 66)
    print(f"{len(entradas)} datasets · {len(rel.erros)} erro(s) · {len(rel.avisos)} aviso(s)")
    if rel.erros:
        print()
        print("ERROS:")
        for ident, msg in rel.erros:
            print(f"  {ident}: {msg}")

    if rel.erros and not args.so_avisar:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
