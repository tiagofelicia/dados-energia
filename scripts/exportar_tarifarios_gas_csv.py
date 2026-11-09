#!/usr/bin/env python3
"""
Exporta a BD do simulador de gás natural para CSVs, um por folha do xlsx, mais um
manifest.json — como os do simulador de eletricidade (…/simulador-tarifarios-eletricidade/csv/).

Corre no workflow «Atualizar Tarifários Gás Natural (MIBGAS)», DEPOIS de o
atualizar_tarifarios_gas.py escrever no xlsx o MIBGAS e o Info do dia. O xlsx continua a
ser a base mantida à mão (não é committed com o MIBGAS fresco); o que se publica são os CSVs.

Os valores saem como o xlsx os guarda (openpyxl, sem os tipos do pandas, que passava o texto
"TRUE" a True e o 12 a 12.0): o texto tal e qual (o "TRUE", o "-" do preço dos indexados), os
números sem casas a mais e as datas em AAAA-MM-DD (com a hora, se a tiverem). Os simuladores
leem-nos e só convertem em número o que é número — como a leitura do xlsx.

Tudo ou nada: os ficheiros novos são escritos ao lado (.tmp) e só substituem os publicados
depois de todos estarem escritos, com o manifest em último lugar. Se faltar uma folha, uma
coluna obrigatória ou o MIBGAS, não se publica nada (ficam os anteriores) e o script sai com
erro — a corrida fica vermelha (email) e o deploy no Hugging Face não corre.

Uso: python scripts/exportar_tarifarios_gas_csv.py [caminho.xlsx] [--saida pasta]
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import sys

import openpyxl

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASTA_GAS = os.path.join(RAIZ, "data", "simuladores", "simulador-tarifarios-gas")
FICHEIRO_EXCEL = os.path.join(PASTA_GAS, "tarifarios_gas_natural_Tiago_Felicia.xlsx")
PASTA_CSV = os.path.join(PASTA_GAS, "csv")

# Folha → colunas que os simuladores (gás e Dual) não dispensam
FOLHAS = {
    "Tarifas_Gas_Master": ["Comercializador", "Nome_Tarifa_G", "escalao", "Termo_Fixo_eur_dia", "Termo_Energia_eur_kwh"],
    "MIBGAS": ["Data", "Preço"],
    "Info": ["Descricao", "Data"],
    "TOS": ["Município"],
    "Constantes": ["constante", "valor_unitário"],
}


def texto(v):
    """O valor da célula como texto do CSV (vazio = célula vazia)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)          # volta ao mesmo número (round-trip)
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d") if v.time() == datetime.time(0) else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.isoformat()
    return str(v)


def ler_folha(ws):
    """(cabeçalho, linhas) de uma folha: só as colunas com nome e as linhas com algum valor."""
    linhas = list(ws.iter_rows(values_only=True))
    if not linhas:
        return [], []
    cab = linhas[0]
    cols = [i for i, c in enumerate(cab) if c is not None and str(c).strip() != ""]
    nomes = [str(cab[i]).strip() for i in cols]
    dados = []
    for r in linhas[1:]:
        valores = [r[i] if i < len(r) else None for i in cols]
        if any(v is not None and str(v).strip() != "" for v in valores):
            dados.append(valores)
    return nomes, dados


def md5(caminho):
    # Hash MD5 do conteúdo para o manifest (os simuladores comparam-no com o da cache)
    with open(caminho, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]


def exportar(caminho_xlsx, pasta_csv):
    wb = openpyxl.load_workbook(caminho_xlsx, read_only=True, data_only=True)
    folhas = {}
    erros = []
    for folha, obrigatorias in FOLHAS.items():
        if folha not in wb.sheetnames:
            erros.append(f"falta a folha «{folha}»")
            continue
        nomes, dados = ler_folha(wb[folha])
        faltam = [c for c in obrigatorias if c not in nomes]
        if faltam:
            erros.append(f"{folha}: faltam as colunas {', '.join(faltam)}")
        if len(set(nomes)) != len(nomes):
            erros.append(f"{folha}: colunas com o mesmo nome ({', '.join(sorted({n for n in nomes if nomes.count(n) > 1}))})")
        if not dados:
            erros.append(f"{folha}: sem linhas")
        folhas[folha] = (nomes, dados)
    wb.close()

    # O MIBGAS tem de ter datas e preços (sem ele os indexados e o MIBGAS por omissão ficam a zero)
    if "MIBGAS" in folhas and not erros:
        nomes, dados = folhas["MIBGAS"]
        iD, iP = nomes.index("Data"), nomes.index("Preço")
        bons = [r for r in dados if isinstance(r[iD], (datetime.date, datetime.datetime))
                and isinstance(r[iP], (int, float)) and not isinstance(r[iP], bool) and r[iP] > 0]
        if not bons:
            erros.append("MIBGAS: nenhuma linha com data e preço")

    if erros:
        print("❌ CSVs do gás NÃO exportados (ficam os anteriores):")
        for e in erros:
            print(f"   - {e}")
        return None

    os.makedirs(pasta_csv, exist_ok=True)
    trocas, manifest = [], {}
    try:
        for folha, (nomes, dados) in folhas.items():
            final = os.path.join(pasta_csv, f"{folha}.csv")
            trocas.append((final + ".tmp", final))
            with open(final + ".tmp", "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                w.writerow(nomes)
                w.writerows([texto(v) for v in r] for r in dados)
            manifest[folha] = md5(final + ".tmp")
            print(f"   ✅ {folha}.csv ({len(dados)} registos) [{manifest[folha]}]")
        final = os.path.join(pasta_csv, "manifest.json")
        trocas.append((final + ".tmp", final))
        with open(final + ".tmp", "w", encoding="utf-8") as f:
            json.dump(manifest, f)
    except Exception:
        for tmp, _ in trocas:
            if os.path.exists(tmp):
                os.remove(tmp)
        raise

    for tmp, final in trocas:
        os.replace(tmp, final)
    print(f"   ✅ manifest.json gerado: {manifest}")
    return manifest


def main():
    p = argparse.ArgumentParser(description="Exporta a BD do simulador de gás (xlsx) para CSVs + manifest.json.")
    p.add_argument("xlsx", nargs="?", default=FICHEIRO_EXCEL)
    p.add_argument("--saida", default=PASTA_CSV, help="pasta dos CSVs (por omissão, a do repositório)")
    a = p.parse_args()
    print(f"Exportar a BD do gás: {a.xlsx} → {a.saida}")
    if exportar(a.xlsx, a.saida) is None:
        sys.exit(1)
    print("✅ Exportação dos CSVs do gás concluída.")


if __name__ == "__main__":
    main()
