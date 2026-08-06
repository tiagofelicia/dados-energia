# --- Carregar as bibliotecas necessárias ---
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter
from datetime import datetime

print("✅ Bibliotecas carregadas para [Atualizar OMIP histórico]")

# ===================================================================
# ---- CONTEXTO ----
# ===================================================================
# O omip.pt publica apenas o ficheiro da sessão em curso
# (sites/default/files/dados/eod/omipdaily.xlsx) e deixou de disponibilizar
# histórico — não existem ficheiros datados. O snapshot que o
# atualizar_omie_dados_atuais.py escreve é sobrescrito a cada execução, pelo
# que cada sessão não guardada se perde definitivamente.
#
# Este script acumula esse snapshot num histórico anual:
#
#   data/omie/futuros_omip.csv  →  data/omie/historico/omip_historico_<ANO>.csv
#
# Formato (uma linha por sessão × zona × contrato):
#
#   Data,Zona,Contrato,Valor
#   05/08/2026,PT,FPB M Sep-26,103.55
#
#   Data  — data da sessão OMIP (Data_Valores_OMIP do snapshot), NÃO a data de
#           execução: é o que identifica a cotação.
#   Zona  — PT (contratos FPB) ou ES (contratos FTB).
#   Valor — preço de fecho em €/MWh.
#
# Descricao e Variacao não são guardadas por serem redundantes: a primeira é
# derivável do código do contrato, a segunda é a diferença entre duas sessões
# consecutivas — que o histórico passa a permitir calcular para qualquer par.
#
# A escrita é um upsert com chave (Data, Zona, Contrato), o que torna o script
# idempotente: pode correr as 4 execuções diárias do workflow, aos fins de
# semana e feriados (em que a data OMIP não avança) ou duas vezes seguidas,
# sem nunca duplicar linhas. Se o OMIP corrigir uma sessão já guardada, os
# valores novos substituem os antigos.
# ===================================================================

# ===================================================================
# ---- CONFIGURAÇÕES ----
# ===================================================================
# Caminhos ancorados no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
FICHEIRO_SNAPSHOT = os.path.join(ROOT_DIR, "data", "omie", "futuros_omip.csv")   # input
PASTA_HISTORICO = os.path.join(ROOT_DIR, "data", "omie", "historico")           # output
PADRAO_HISTORICO = "omip_historico_{ano}.csv"

COLUNAS = ["Data", "Zona", "Contrato", "Valor"]
# Secção do snapshot → zona do histórico
SECCOES = {"FUTUROS_PT": "PT", "FUTUROS_ES": "ES"}
# Prefixo esperado do código de contrato em cada zona (validação)
PREFIXO_ZONA = {"PT": "FPB", "ES": "FTB"}
# ===================================================================


def _seccoes_tabelas(texto):
    """
    Divide um CSV multi-tabela nas suas secções TABELA_<TAG>.

    Devolve {TAG: corpo}. Aceita o TABELA_ no início do ficheiro (como no
    futuros_omip.csv) ou precedido de outras linhas (como no
    omie_dados_atuais.csv, onde as tabelas vêm depois dos dados quarto-horários),
    tolera BOM e terminações \\r\\n.
    """
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    resultado = {}
    for bloco in re.split(r"(?:^|\n)TABELA_", texto)[1:]:
        linhas = bloco.split("\n")
        tag = linhas[0].strip()
        if tag:
            resultado[tag] = "\n".join(linhas[1:])
    return resultado


def _linhas_csv(corpo):
    """Lê um corpo de secção como lista de dicionários (usa o cabeçalho)."""
    linhas = [l for l in corpo.split("\n") if l.strip()]
    if not linhas:
        return []
    leitor = csv.reader(io.StringIO("\n".join(linhas)))
    filas = list(leitor)
    if not filas:
        return []
    cabecalho = [c.strip() for c in filas[0]]
    registos = []
    for fila in filas[1:]:
        registos.append({cabecalho[i]: fila[i].strip() for i in range(min(len(cabecalho), len(fila)))})
    return registos


def ler_snapshot(texto):
    """
    Extrai de um snapshot (futuros_omip.csv ou omie_dados_atuais.csv) a data da
    sessão OMIP e as cotações de todos os contratos.

    Devolve (data_sessao, [(zona, contrato, valor_float), ...]) com as linhas na
    ordem do ficheiro (PT primeiro, depois ES; dentro de cada zona pela ordem do
    OMIP: diários, semanais, mensais, trimestrais, anuais).

    Levanta ValueError se a data da sessão não for legível — sem ela não é
    possível saber a que sessão pertencem os valores, e guardá-los seria pior
    do que não os guardar.
    """
    seccoes = _seccoes_tabelas(texto)

    data_sessao = None
    for registo in _linhas_csv(seccoes.get("ATUALIZACOES", "")):
        if registo.get("chave") == "Data_Valores_OMIP":
            data_sessao = (registo.get("valor") or "").strip()
            break
    if not data_sessao:
        raise ValueError("Data_Valores_OMIP não encontrada na TABELA_ATUALIZACOES.")
    try:
        datetime.strptime(data_sessao, "%d/%m/%Y")
    except ValueError:
        raise ValueError(f"Data_Valores_OMIP inválida: '{data_sessao}' (esperado DD/MM/AAAA).")

    linhas = []
    for tag, zona in SECCOES.items():
        for registo in _linhas_csv(seccoes.get(tag, "")):
            contrato = (registo.get("Contrato") or "").strip()
            valor_str = (registo.get("Valor") or "").strip()
            if not contrato or not valor_str:
                continue
            try:
                valor = float(valor_str)
            except ValueError:
                print(f"   ⚠️ Valor não numérico ignorado: {zona} {contrato} = '{valor_str}'")
                continue
            prefixo = PREFIXO_ZONA[zona]
            if not contrato.startswith(prefixo):
                print(f"   ⚠️ Contrato '{contrato}' na secção {tag} não começa por {prefixo} — ignorado.")
                continue
            linhas.append((zona, contrato, valor))

    return data_sessao, linhas


def ler_historico(caminho):
    """
    Lê um histórico existente para {data_sessao: [(zona, contrato, valor_str)]},
    preservando a ordem das linhas dentro de cada sessão.
    """
    sessoes = {}
    if not os.path.exists(caminho):
        return sessoes
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        for registo in csv.DictReader(f):
            data = (registo.get("Data") or "").strip()
            zona = (registo.get("Zona") or "").strip()
            contrato = (registo.get("Contrato") or "").strip()
            valor = (registo.get("Valor") or "").strip()
            if data and zona and contrato and valor:
                sessoes.setdefault(data, []).append((zona, contrato, valor))
    return sessoes


def escrever_historico(caminho, sessoes):
    """Escreve o histórico com as sessões por ordem cronológica."""
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.writer(f)
        escritor.writerow(COLUNAS)
        for data in sorted(sessoes, key=lambda d: datetime.strptime(d, "%d/%m/%Y")):
            for zona, contrato, valor in sessoes[data]:
                escritor.writerow([data, zona, contrato, valor])


def caminho_historico(data_sessao, pasta=PASTA_HISTORICO):
    """Histórico anual a que uma sessão pertence (partição pelo ano da sessão)."""
    ano = datetime.strptime(data_sessao, "%d/%m/%Y").year
    return os.path.join(pasta, PADRAO_HISTORICO.format(ano=ano))


def _aplicar(sessoes, data_sessao, linhas):
    """Upsert em memória. Devolve 'nova', 'corrigida' ou 'inalterada'."""
    anterior = sessoes.get(data_sessao)
    nova = [(zona, contrato, f"{valor:.2f}") for zona, contrato, valor in linhas]
    if anterior == nova:
        return "inalterada"
    estado = "corrigida" if anterior is not None else "nova"
    sessoes[data_sessao] = nova
    return estado


def acumular_varias(novas, pasta=PASTA_HISTORICO, verboso=True):
    """
    Faz o upsert de várias sessões, lendo e escrevendo cada histórico anual uma
    única vez — e não uma reescrita do ficheiro por cada sessão, que tornava o
    backfill quadrático no número de sessões.

    `novas` é {data_sessao: [(zona, contrato, valor_float), ...]}.
    Devolve {caminho: {'estados': Counter, 'sessoes': int, 'linhas': int}}.
    Se nenhuma sessão de um ficheiro mudar, o ficheiro não é reescrito.
    """
    por_ficheiro = {}
    for data_sessao, linhas in novas.items():
        por_ficheiro.setdefault(caminho_historico(data_sessao, pasta), {})[data_sessao] = linhas

    resultado = {}
    for caminho, sessoes_novas in por_ficheiro.items():
        sessoes = ler_historico(caminho)
        estados = Counter()
        for data_sessao in sorted(sessoes_novas, key=lambda d: datetime.strptime(d, "%d/%m/%Y")):
            estados[_aplicar(sessoes, data_sessao, sessoes_novas[data_sessao])] += 1
        if estados["nova"] or estados["corrigida"]:
            escrever_historico(caminho, sessoes)
        resultado[caminho] = {
            "estados": estados,
            "sessoes": len(sessoes),
            "linhas": sum(len(v) for v in sessoes.values()),
        }
        if verboso:
            print(f"   📊 '{os.path.basename(caminho)}': {estados['nova']} novas, "
                  f"{estados['corrigida']} corrigidas, {estados['inalterada']} já iguais "
                  f"→ {len(sessoes)} sessões, {resultado[caminho]['linhas']} linhas.")
    return resultado


def acumular(data_sessao, linhas, pasta=PASTA_HISTORICO, verboso=True):
    """
    Faz o upsert de uma sessão no histórico anual respetivo.

    Devolve (caminho, estado) com estado em {'nova', 'corrigida', 'inalterada'}.
    """
    resultado = acumular_varias({data_sessao: linhas}, pasta=pasta, verboso=False)
    caminho, info = next(iter(resultado.items()))
    estados = info["estados"]
    estado = ("nova" if estados["nova"] else
              "corrigida" if estados["corrigida"] else "inalterada")

    if verboso:
        n_linhas = len(linhas)
        if estado == "inalterada":
            print(f"   ℹ️ Sessão {data_sessao} já registada com os mesmos valores — nada a fazer.")
        elif estado == "corrigida":
            print(f"   ♻️ Sessão {data_sessao} já existia com valores diferentes — substituída ({n_linhas} linhas).")
        else:
            print(f"   ✅ Sessão {data_sessao} adicionada ({n_linhas} linhas).")
        print(f"   📊 '{os.path.basename(caminho)}': {info['sessoes']} sessões, {info['linhas']} linhas.")

    return caminho, estado


def main():
    parser = argparse.ArgumentParser(
        description="Acumula o snapshot dos futuros OMIP no histórico anual."
    )
    parser.add_argument(
        "--snapshot", default=FICHEIRO_SNAPSHOT,
        help="Snapshot a processar (futuros_omip.csv ou omie_dados_atuais.csv). "
             "Por omissão, data/omie/futuros_omip.csv."
    )
    parser.add_argument(
        "--historico", default=PASTA_HISTORICO,
        help="Pasta dos históricos anuais. Por omissão, data/omie/historico/."
    )
    args = parser.parse_args()

    print(f"ℹ️ Snapshot: '{args.snapshot}'")
    print(f"ℹ️ Histórico: '{args.historico}'")

    try:
        with open(args.snapshot, "r", encoding="utf-8-sig") as f:
            texto = f.read()
    except FileNotFoundError:
        print(f"❌ Snapshot não encontrado: '{args.snapshot}'")
        return 1

    try:
        data_sessao, linhas = ler_snapshot(texto)
    except ValueError as e:
        print(f"❌ Snapshot inválido: {e}")
        return 1

    if not linhas:
        print("❌ Snapshot sem contratos — nada para acumular.")
        return 1

    pt = sum(1 for zona, _, _ in linhas if zona == "PT")
    es = len(linhas) - pt
    # Sem contratos de uma das zonas o snapshot está incompleto, mas guarda-se
    # o que existe: uma sessão parcial é melhor do que uma sessão perdida.
    if not pt or not es:
        print(f"   ⚠️ Snapshot incompleto (PT: {pt}, ES: {es}) — a guardar o que existe.")

    print(f"\n⏳ A acumular a sessão OMIP de {data_sessao} ({pt} contratos PT + {es} ES)...")
    acumular(data_sessao, linhas, pasta=args.historico)
    print("\n✅ Histórico OMIP atualizado.")
    return 0


# PONTO DE ENTRADA DO SCRIPT
if __name__ == "__main__":
    sys.exit(main())
