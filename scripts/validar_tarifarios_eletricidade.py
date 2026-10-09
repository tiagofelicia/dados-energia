#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validar_tarifarios_eletricidade.py
Verifica a BD do simulador de eletricidade (o xlsx mantido à mão) antes de ela
ser publicada.

PARA QUE SERVE
--------------
O xlsx é editado à mão e o simulador lê-o sem rede de segurança. Nada do que se
segue dá erro em lado nenhum — o simulador calcula, só que mal:

  * um preço escrito como texto ("0,15") chega ao browser como 0;
  * uma constante em falta vale 0 (o getConst() devolve 0), como já aconteceu
    com a G9 no atualizar_precos-horarios_csv.py;
  * uma célula copiada de outro tarifário muda o preço só naquela potência e
    naquela opção horária. Caso real, 09/10/2026: o YesEnergy | #SMARTLIVING
    tinha custo_regulacao_kwh 0,0158688 em 35 linhas e 0,0160312 (o da EZU) na
    linha 2869;
  * um nome_pos_promo com uma gralha parte a cadeia de fases do 1.º ano, e o
    simulador mostra "⚠ fase seguinte não encontrada na BD" aos visitantes.

ONDE CORRE
----------
  1. Passo 6 do atualizar_tarifarios_eletricidade.py: com erros, os CSVs do
     simulador NÃO são exportados e ficam os da última versão boa.
  2. Workflow validar_bd_eletricidade.yml, a cada push do xlsx e uma vez por
     dia. É ele o alarme: chumba, e o GitHub manda email.
  3. À mão, antes de enviar o xlsx:
       python scripts/validar_tarifarios_eletricidade.py [caminho.xlsx]

ERROS E AVISOS
--------------
O mesmo critério do validar_dados.py: ERRO para o que estraga o cálculo ou a
lista do simulador, AVISO para o que pode ser legítimo. Só os erros bloqueiam.

  * A coerência dentro de um tarifário só é ERRO nas colunas de custo que não
    podem depender da potência nem da opção (CONSTANTES_NO_TARIFARIO). As
    condições de campanha podem variar, e variam: a EDP Indexada tem o desconto
    de 10 € só nalgumas potências e opções.
  * Os LIMITES têm folga larga sobre os valores reais de hoje. Servem para
    apanhar vírgulas no sítio errado (0,01548 em vez de 0,1548), não para julgar
    preços. Se um valor verdadeiro sair deles, alarga-se o limite aqui.
  * CONSTANTES_USADAS saiu do código do simulador (getConst, erc() e as famílias
    montadas com a potência). Quando o simulador passar a usar uma constante
    nova, acrescenta-se aqui; quando deixar de usar uma, tira-se.

USO
---
  python validar_tarifarios_eletricidade.py               # o xlsx do repositório
  python validar_tarifarios_eletricidade.py outro.xlsx
  python validar_tarifarios_eletricidade.py --so-avisar   # sai sempre com 0
"""

import argparse
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
FICHEIRO_EXCEL = os.path.join(ROOT_DIR, "data", "simuladores", "simulador-tarifarios-eletricidade",
                              "tarifarios_eletricidade_Tiago_Felicia.xlsx")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass


# ============================================================
# Regras
# ============================================================

ABAS = ["Constantes", "Tarifarios_fixos", "Indexados", "OMIE_PERDAS_CICLOS"]
ABAS_TARIFARIOS = ["Tarifarios_fixos", "Indexados"]

# Colunas de que o simulador depende. Não é o cabeçalho todo de propósito: uma
# coluna nova é bem-vinda, uma coluna a menos não.
COLUNAS = {
    "Tarifarios_fixos": [
        "comercializador", "nome", "tipo", "segmento", "faturacao", "pagamento",
        "opcao_horaria_e_ciclo", "potencia_kva", "preco_potencia_dia",
        "preco_energia_simples", "preco_energia_fora_vazio", "preco_energia_vazio_bi",
        "preco_energia_ponta", "preco_energia_cheias", "preco_energia_vazio_tri",
        "tar_incluida_energia", "tar_incluida_potencia", "financiamento_tse_incluido",
        "custo_regulacao_kwh", "regulacao_iva_23", "perdas_autonomas", "100_Verde",
        "desconto_fatura_mes", "desconto_meses_limite", "desconto_amigo_mes",
        "desconto_amigo_meses", "site_adesao", "custo_pack_mes", "meses_promo",
        "nome_pos_promo"],
    "Indexados": [
        "comercializador", "nome", "tipo", "segmento", "faturacao", "pagamento",
        "opcao_horaria_e_ciclo", "potencia_kva", "preco_potencia_dia",
        "tar_incluida_energia", "tar_incluida_potencia", "financiamento_tse_incluido",
        "100_Verde", "site_adesao"],
    "Constantes": ["constante", "valor_unitário"],
    "OMIE_PERDAS_CICLOS": ["Data", "Hora", "BD", "BS", "TD", "TS",
                           "BTN_A", "BTN_B", "BTN_C", "OMIE", "Perdas"],
}

# Uma linha com dados tem de ter estes campos preenchidos
IDENTIFICACAO = ["comercializador", "nome", "tipo", "opcao_horaria_e_ciclo",
                 "potencia_kva", "preco_potencia_dia"]

# Opção horária → preços de energia que um tarifário fixo tem de ter
OPCOES = {
    "Simples": ["preco_energia_simples"],
    "Bi-horário - Ciclo Diário": ["preco_energia_fora_vazio", "preco_energia_vazio_bi"],
    "Bi-horário - Ciclo Semanal": ["preco_energia_fora_vazio", "preco_energia_vazio_bi"],
    "Tri-horário - Ciclo Diário": ["preco_energia_ponta", "preco_energia_cheias", "preco_energia_vazio_tri"],
    "Tri-horário - Ciclo Semanal": ["preco_energia_ponta", "preco_energia_cheias", "preco_energia_vazio_tri"],
    "Tri-horário > 20.7 kVA - Ciclo Diário": ["preco_energia_ponta", "preco_energia_cheias", "preco_energia_vazio_tri"],
    "Tri-horário > 20.7 kVA - Ciclo Semanal": ["preco_energia_ponta", "preco_energia_cheias", "preco_energia_vazio_tri"],
}
PRECOS_ENERGIA = sorted({c for cols in OPCOES.values() for c in cols})

POTENCIAS_KVA = [1.15, 2.3, 3.45, 4.6, 5.75, 6.9, 10.35, 13.8, 17.25, 20.7, 27.6, 34.5, 41.4]

# Valores conhecidos dos filtros do simulador (o filtro procura o texto dentro
# da célula — um valor com gralha faz o tarifário desaparecer quando se filtra)
TIPOS = {"Tarifarios_fixos": {"Fixo"},
         "Indexados": {"Indexado quarto-horário", "Indexado Média"}}
SEGMENTOS = {"Doméstico", "Não Doméstico", "Doméstico e Não Doméstico"}
FATURACAO = {"Fatura eletrónica", "Fatura em papel"}
PAGAMENTO = {"Débito Direto", "Multibanco", "Numerário/Payshop/CTT"}

NUMERICAS = ["potencia_kva", "preco_potencia_dia", *PRECOS_ENERGIA, "custo_regulacao_kwh",
             "desconto_fatura_mes", "desconto_amigo_inicial", "desconto_amigo_mes",
             "custo_pack_mes", "desconto_mes_inicio", "desconto_meses_limite",
             "desconto_amigo_meses", "meses_promo"]
MESES = ["desconto_mes_inicio", "desconto_meses_limite", "desconto_amigo_meses", "meses_promo"]
BOOLEANAS = ["tar_incluida_energia", "tar_incluida_potencia", "financiamento_tse_incluido",
             "100_Verde", "regulacao_iva_23", "perdas_autonomas", "apenas_fase"]

# (mínimo, máximo), inclusive. Valores reais a 09/10/2026 em comentário.
LIMITES = {
    "preco_potencia_dia": (0.02, 6.0),        # €/dia; 0,057–3,58
    **{c: (0.03, 1.0) for c in PRECOS_ENERGIA},  # €/kWh; 0,088–0,49
    "custo_regulacao_kwh": (0.0001, 0.1),     # €/kWh; 0,0159–0,0160
    "desconto_fatura_mes": (0.01, 50),        # €/mês; 4–10
    "desconto_amigo_mes": (0.01, 50),         # €/mês; 1,23–7,5
    "desconto_amigo_inicial": (0.01, 200),    # € (uma vez); vazia
    "custo_pack_mes": (0.01, 100),            # €/mês; 12,18–19,56
}
LIMITES_MESES = (1, 36)                       # 2–20
LIMITES_PERDAS = (1.0, 1.5)                   # 1,118–1,253

# Propriedades do tarifário, que não dependem da potência nem da opção horária.
# Valores diferentes entre linhas do mesmo tarifário são uma cópia mal feita.
CONSTANTES_NO_TARIFARIO = [
    "tar_incluida_energia", "tar_incluida_potencia", "financiamento_tse_incluido",
    "custo_regulacao_kwh", "regulacao_iva_23", "perdas_autonomas",
    "desconto_amigo_inicial", "desconto_amigo_mes", "desconto_amigo_meses", "custo_pack_mes"]
# Estas também costumam ser iguais, mas não mudam o preço: só avisam
QUASE_CONSTANTES_NO_TARIFARIO = ["tipo", "segmento", "100_Verde", "site_adesao"]

# Constantes que o simulador (Tiago_Felicia_JS_v64 + motor_elet.js) vai buscar
# pelo nome. Datas à parte, em DATAS_CONSTANTES.
CONSTANTES_USADAS = [
    "Coop_K", "Coop_CS", "Repsol_FA", "Repsol_Q_Tarifa", "Repsol_Q_Tarifa_Pro",
    "Luzboa_FA", "Luzboa_CGS", "Luzboa_Kp", "Ibelectra_CS", "Ibelectra_Perdas",
    "Ibelectra_K", "Ibelectra_K_a", "Luzigas_CGS", "Luzigas_8_8_K", "Luzigas_K",
    "Plenitude_CGS", "Plenitude_Fee", "Plenitude_GDOs", "EDP_M_Perdas", "EDP_M_K1",
    "EDP_M_K2", "EDP_H_K1", "EDP_H_K2", "Iberdrola_Perdas", "Iberdrola_Media_Q",
    "Iberdrola_Dinamico_Q", "Iberdrola_mFRR", "Galp_Ci", "Alfa_CGS", "Alfa_K",
    "GE_Q_Tarifa", "GE_CG", "Meo_K", "Meo_CG", "EZU_K", "EZU_CGS",
    "Endesa_A_S", "Endesa_A_V", "Endesa_A_FV", "G9_K1", "G9_K2", "G9_K3",
    "TAR_Energia_Simples", "TAR_Energia_Bi_Vazio", "TAR_Energia_Bi_ForaVazio",
    "TAR_Energia_Tri_Vazio", "TAR_Energia_Tri_Cheias", "TAR_Energia_Tri_Ponta",
    "TAR_Energia_Tri_27.6_Vazio", "TAR_Energia_Tri_27.6_Cheias", "TAR_Energia_Tri_27.6_Ponta",
    *[f"TAR_Potencia {k:g}" for k in POTENCIAS_KVA],
    "Desconto TS Energia",
    *[f"Desconto TS Potencia {k:g}" for k in POTENCIAS_KVA if k <= 6.9],
    "Quota_ACP", "Financiamento_TSE", "DGEG", "CAV", "IEC",
]
# Escritas pelo bot (B90–B92), em MM/DD/AAAA. A do ERC só existe no xlsx do bot.
DATAS_CONSTANTES = {"Data_Valores_OMIE": True, "Data_Valores_OMIP": True, "Data_Valores_ERC": False}

# OMIE_PERDAS_CICLOS: abaixo desta fração de linhas com OMIE, avisa
COBERTURA_OMIE_MIN = 0.99


# ============================================================
# Relatório
# ============================================================

class Relatorio:
    """Acumula o que se encontrou. ERRO bloqueia a publicação, AVISO não."""

    def __init__(self):
        self.erros = []
        self.avisos = []

    def erro(self, aba, msg):
        self.erros.append((aba, msg))

    def aviso(self, aba, msg):
        self.avisos.append((aba, msg))

    def texto(self, limite=60):
        linhas = [f"{len(self.erros)} erro(s) · {len(self.avisos)} aviso(s)"]
        for titulo, itens in (("ERROS", self.erros), ("AVISOS", self.avisos)):
            if not itens:
                continue
            linhas += ["", f"{titulo}:"]
            linhas += [f"  [{aba}] {msg}" for aba, msg in itens[:limite]]
            if len(itens) > limite:
                linhas.append(f"  ... e mais {len(itens) - limite}")
        return "\n".join(linhas)

    def markdown(self, titulo, limite=60):
        if self.erros:
            estado = ("❌ **Com erros**: enquanto não forem corrigidos no xlsx, "
                      "o bot não publica os CSVs do simulador (ficam os da última versão boa).")
        else:
            estado = "✅ Sem erros."
        partes = [f"### {titulo}", "", estado, ""]
        for nome, itens in (("Erros", self.erros), ("Avisos", self.avisos)):
            if not itens:
                continue
            partes.append(f"**{nome} ({len(itens)})**")
            partes += [f"- `{aba}` {msg}" for aba, msg in itens[:limite]]
            if len(itens) > limite:
                partes.append(f"- … e mais {len(itens) - limite}")
            partes.append("")
        return "\n".join(partes)

    def escrever_resumo_github(self, titulo):
        """Resumo na página da corrida (só dentro do GitHub Actions)."""
        caminho = os.environ.get("GITHUB_STEP_SUMMARY")
        if caminho:
            with open(caminho, "a", encoding="utf-8") as f:
                f.write(self.markdown(titulo) + "\n")

    def anotacoes_github(self, limite=20):
        """Erros e avisos como anotações do Actions (aparecem no topo da corrida)."""
        for nivel, itens in (("error", self.erros), ("warning", self.avisos)):
            for aba, msg in itens[:limite]:
                print(f"::{nivel}::[{aba}] {msg}")


# ============================================================
# Auxiliares
# ============================================================

def vazio(v):
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v.strip() == ""


def numero(v):
    """float, ou None se não for um número. True/False não contam como número."""
    if vazio(v) or isinstance(v, bool) or type(v).__name__ == "bool_":
        return None
    if isinstance(v, (int, float)) or type(v).__name__.startswith(("int", "float")):
        f = float(v)
        return f if math.isfinite(f) else None
    try:
        # Texto: aceita "0.15" (o browser lê-o bem); "0,15" falha, de propósito
        return float(str(v).strip())
    except ValueError:
        return None


def booleano(v):
    """Como o parseBool() do simulador: True/False, ou None se não reconhecer."""
    if isinstance(v, bool) or type(v).__name__ == "bool_":
        return bool(v)
    n = numero(v)
    if n in (0.0, 1.0):
        return n == 1.0
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("true", "sim", "yes", "1"):
            return True
        if t in ("false", "não", "nao", "no", "0"):
            return False
    return None


def data_mdy(v):
    if isinstance(v, (datetime, pd.Timestamp)):
        return v
    try:
        return datetime.strptime(str(v).strip(), "%m/%d/%Y")
    except ValueError:
        return None


def normalizar(v):
    """Valor comparável entre linhas: números arredondados, booleanos e texto aparado."""
    if vazio(v):
        return None
    b = booleano(v) if (isinstance(v, bool) or type(v).__name__ == "bool_") else None
    if b is not None:
        return b
    n = numero(v)
    if n is not None:
        return round(n, 9)
    return str(v).strip()


def fmt(v):
    if v is None:
        return "(vazio)"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def linhas(nums, n=8):
    nums = sorted(nums)
    resto = f" (+{len(nums) - n})" if len(nums) > n else ""
    return ", ".join(str(x) for x in nums[:n]) + resto


def descr(r):
    return f"{r['nome']} · {r['opcao_horaria_e_ciclo']} · {fmt(numero(r['potencia_kva']))} kVA"


# ============================================================
# Verificações
# ============================================================

def ver_colunas(abas, rel):
    """Folhas e colunas obrigatórias. Devolve as abas que se podem verificar."""
    ok = {}
    for aba in ABAS:
        df = abas.get(aba)
        if df is None:
            rel.erro(aba, "folha em falta no xlsx")
            continue
        faltam = [c for c in COLUNAS[aba] if c not in df.columns]
        if faltam:
            rel.erro(aba, f"coluna(s) em falta: {', '.join(faltam)}")
            continue
        ok[aba] = df
    return ok


def linhas_com_dados(df):
    """[(linha do Excel, registo)] das linhas que não estão totalmente vazias."""
    res = []
    for i, r in enumerate(df.to_dict("records")):
        if all(vazio(v) for v in r.values()):
            continue
        res.append((i + 2, r))  # +1 do cabeçalho, +1 porque o Excel conta de 1
    return res


def ver_linhas(aba, regs, rel):
    """Campos de cada linha: preenchimento, tipos, valores conhecidos e limites."""
    problemas = defaultdict(list)  # mensagem → [linhas]; agrupa os repetidos

    for ln, r in regs:
        em_falta = [c for c in IDENTIFICACAO if vazio(r.get(c))]
        if em_falta:
            problemas[("erro", f"linha sem {', '.join(em_falta)}")].append(ln)

        opcao = str(r.get("opcao_horaria_e_ciclo") or "").strip()
        if opcao and opcao not in OPCOES:
            problemas[("erro", f"opção horária desconhecida: \"{opcao}\"")].append(ln)

        for c in NUMERICAS:
            if c not in r or vazio(r[c]):
                continue
            n = numero(r[c])
            if n is None:
                problemas[("erro", f"{c} não é um número: \"{r[c]}\"")].append(ln)
            elif c in MESES:
                lo, hi = LIMITES_MESES
                if n != int(n) or not lo <= n <= hi:
                    problemas[("erro", f"{c} = {fmt(n)} (tem de ser um n.º inteiro de meses entre {lo} e {hi})")].append(ln)
            elif c in LIMITES:
                lo, hi = LIMITES[c]
                if not lo <= n <= hi:
                    problemas[("erro", f"{c} = {fmt(n)} fora do intervalo plausível [{fmt(lo)}, {fmt(hi)}]")].append(ln)

        for c in BOOLEANAS:
            if c in r and not vazio(r[c]) and booleano(r[c]) is None:
                problemas[("erro", f"{c} não é verdadeiro/falso: \"{r[c]}\"")].append(ln)

        kva = numero(r.get("potencia_kva"))
        if kva is not None and not any(abs(kva - p) < 0.001 for p in POTENCIAS_KVA):
            problemas[("aviso", f"potência {fmt(kva)} kVA fora da lista das 13 potências")].append(ln)

        if aba == "Tarifarios_fixos" and opcao in OPCOES:
            faltam = [c for c in OPCOES[opcao] if vazio(r.get(c))]
            if faltam:
                problemas[("erro", f"{opcao} sem {', '.join(faltam)}")].append(ln)
            a_mais = [c for c in PRECOS_ENERGIA if c not in OPCOES[opcao] and not vazio(r.get(c))]
            if a_mais:
                problemas[("aviso", f"{opcao} com preço noutra coluna ({', '.join(a_mais)}), que é ignorado")].append(ln)

        tipo = str(r.get("tipo") or "").strip()
        if tipo and tipo not in TIPOS[aba]:
            problemas[("aviso", f"tipo desconhecido: \"{tipo}\"")].append(ln)
        seg = str(r.get("segmento") or "").strip()
        if seg and seg not in SEGMENTOS:
            problemas[("aviso", f"segmento desconhecido: \"{seg}\"")].append(ln)
        for c, conhecidos in (("faturacao", FATURACAO), ("pagamento", PAGAMENTO)):
            if vazio(r.get(c)):
                continue
            estranhos = [p.strip() for p in str(r[c]).split(",") if p.strip() not in conhecidos]
            if estranhos:
                problemas[("aviso", f"{c} com valor desconhecido: \"{', '.join(estranhos)}\"")].append(ln)

    for (nivel, msg), lns in problemas.items():
        getattr(rel, nivel)(aba, f"{msg} — linha(s) {linhas(lns)}")


def chave(r):
    kva = numero(r.get("potencia_kva"))
    return (str(r.get("comercializador") or "").strip(),
            str(r.get("nome") or "").strip().lower(),
            str(r.get("opcao_horaria_e_ciclo") or "").strip().lower(),
            round(kva, 2) if kva is not None else None)


def ver_duplicados(regs_por_aba, rel):
    """A mesma (comercializador, nome, opção, potência) duas vezes, na mesma folha ou nas duas."""
    vistos = defaultdict(list)
    for aba, regs in regs_por_aba.items():
        for ln, r in regs:
            k = chave(r)
            if all(k) and k[3] is not None:
                vistos[k].append((aba, ln, r))
    for k, ocorr in vistos.items():
        if len(ocorr) > 1:
            onde = ", ".join(f"{aba} linha {ln}" for aba, ln, _ in ocorr)
            rel.erro(ocorr[0][0], f"linha repetida ({descr(ocorr[0][2])}): {onde}")


def ver_coerencia(aba, regs, rel):
    """Colunas que têm de ser iguais em todas as linhas de um tarifário."""
    por_tarifario = defaultdict(list)
    for ln, r in regs:
        if not vazio(r.get("nome")):
            por_tarifario[(str(r.get("comercializador") or "").strip(), str(r["nome"]).strip())].append((ln, r))

    for nivel, colunas in (("erro", CONSTANTES_NO_TARIFARIO), ("aviso", QUASE_CONSTANTES_NO_TARIFARIO)):
        for (_, nome), lrs in por_tarifario.items():
            for c in colunas:
                if c not in lrs[0][1]:
                    continue
                valores = Counter(normalizar(r[c]) for _, r in lrs)
                if len(valores) < 2:
                    continue
                maioria, n_maioria = valores.most_common(1)[0]
                diferentes = [(ln, normalizar(r[c])) for ln, r in lrs if normalizar(r[c]) != maioria]
                exemplos = "; ".join(f"linha {ln} = {fmt(v)}" for ln, v in diferentes[:6])
                resto = f" (+{len(diferentes) - 6})" if len(diferentes) > 6 else ""
                getattr(rel, nivel)(aba, f"{nome}: {c} = {fmt(maioria)} em {n_maioria} linha(s), "
                                         f"mas {exemplos}{resto}")


def ver_cadeias(regs_por_aba, rel):
    """nome_pos_promo tem de apontar para uma linha que exista (como o encontrarLinhaPorNome)."""
    indice = {chave(r) for regs in regs_por_aba.values() for _, r in regs}
    quebradas = defaultdict(list)
    sem_meses = defaultdict(list)
    for aba, regs in regs_por_aba.items():
        for ln, r in regs:
            prox = r.get("nome_pos_promo")
            if vazio(prox):
                continue
            prox = str(prox).strip()
            k = chave(r)
            if (k[0], prox.lower(), k[2], k[3]) not in indice:
                quebradas[(aba, str(r["nome"]).strip(), prox)].append(ln)
            if numero(r.get("meses_promo")) is None:
                sem_meses[(aba, str(r["nome"]).strip())].append(ln)
    for (aba, nome, prox), lns in quebradas.items():
        rel.erro(aba, f"{nome}: nome_pos_promo \"{prox}\" não existe com a mesma potência e opção "
                      f"— linha(s) {linhas(lns)}")
    for (aba, nome), lns in sem_meses.items():
        rel.erro(aba, f"{nome}: nome_pos_promo preenchido sem meses_promo — linha(s) {linhas(lns)}")


def ver_constantes(df, rel):
    aba = "Constantes"
    valores = {}
    contagem = Counter()
    for i, r in enumerate(df.to_dict("records")):
        nome = r.get("constante")
        if vazio(nome):
            continue
        nome = str(nome).strip()
        contagem[nome] += 1
        valores[nome] = (i + 2, r.get("valor_unitário"))

    for nome, n in contagem.items():
        if n > 1:
            rel.erro(aba, f"constante repetida {n} vezes: {nome}")

    for nome in CONSTANTES_USADAS:
        if nome not in valores:
            rel.erro(aba, f"constante usada pelo simulador em falta: {nome} (valeria 0)")
        elif numero(valores[nome][1]) is None:
            ln, v = valores[nome]
            rel.erro(aba, f"{nome} (linha {ln}) não é um número: \"{fmt(v)}\" (valeria 0)")

    for nome, obrigatoria in DATAS_CONSTANTES.items():
        if nome not in valores or vazio(valores[nome][1]):
            if obrigatoria:
                rel.erro(aba, f"{nome} em falta")
        elif data_mdy(valores[nome][1]) is None:
            ln, v = valores[nome]
            rel.erro(aba, f"{nome} (linha {ln}) não é uma data MM/DD/AAAA: \"{fmt(v)}\"")

    for nome, (ln, v) in valores.items():
        if nome in CONSTANTES_USADAS or nome in DATAS_CONSTANTES:
            continue
        if not vazio(v) and numero(v) is None:
            rel.aviso(aba, f"{nome} (linha {ln}) não é um número: \"{fmt(v)}\"")


def ver_omie(df, rel):
    aba = "OMIE_PERDAS_CICLOS"
    com_data = df[df["Data"].notna()]
    if com_data.empty:
        rel.erro(aba, "folha sem linhas")
        return
    for c in ("OMIE", "Perdas", "BTN_A", "BTN_B", "BTN_C"):
        nums = pd.to_numeric(com_data[c], errors="coerce")
        maus = com_data.index[com_data[c].notna() & nums.isna()]
        if len(maus):
            rel.erro(aba, f"{c} não numérico — linha(s) {linhas([i + 2 for i in maus])}")
    perdas = pd.to_numeric(com_data["Perdas"], errors="coerce")
    lo, hi = LIMITES_PERDAS
    fora = com_data.index[perdas.notna() & ((perdas < lo) | (perdas > hi))]
    if len(fora):
        rel.erro(aba, f"Perdas fora do intervalo plausível [{lo}, {hi}] — linha(s) {linhas([i + 2 for i in fora])}")
    cobertura = pd.to_numeric(com_data["OMIE"], errors="coerce").notna().mean()
    if cobertura < COBERTURA_OMIE_MIN:
        rel.aviso(aba, f"só {cobertura:.1%} das linhas com data têm OMIE")


# ============================================================
# Entrada
# ============================================================

def ler_abas(caminho):
    """{aba: DataFrame} das quatro abas; None nas que não existem.

    Lido exatamente como o Passo 6 sempre leu (pd.read_excel com as opções por
    omissão), para que os CSVs exportados destes DataFrames não mudem.
    """
    with pd.ExcelFile(caminho) as xl:
        return {aba: (xl.parse(aba) if aba in xl.sheet_names else None) for aba in ABAS}


def validar(abas):
    rel = Relatorio()
    ok = ver_colunas(abas, rel)

    regs = {aba: linhas_com_dados(ok[aba]) for aba in ABAS_TARIFARIOS if aba in ok}
    for aba, rs in regs.items():
        ver_linhas(aba, rs, rel)
        ver_coerencia(aba, rs, rel)
    ver_duplicados(regs, rel)
    ver_cadeias(regs, rel)

    if "Constantes" in ok:
        ver_constantes(ok["Constantes"], rel)
    if "OMIE_PERDAS_CICLOS" in ok:
        ver_omie(ok["OMIE_PERDAS_CICLOS"], rel)
    return rel


def main():
    p = argparse.ArgumentParser(description="Valida a BD (xlsx) do simulador de eletricidade.")
    p.add_argument("xlsx", nargs="?", default=FICHEIRO_EXCEL, help="por omissão, o xlsx do repositório")
    p.add_argument("--so-avisar", dest="so_avisar", action="store_true",
                   help="reportar tudo mas sair sempre com 0")
    args = p.parse_args()

    print(f"validar_tarifarios_eletricidade — {os.path.basename(args.xlsx)}")
    rel = validar(ler_abas(args.xlsx))
    print(rel.texto())

    if os.environ.get("GITHUB_ACTIONS") == "true":
        rel.anotacoes_github()
        rel.escrever_resumo_github("Validação da BD do simulador de eletricidade")

    return 1 if rel.erros and not args.so_avisar else 0


if __name__ == "__main__":
    sys.exit(main())
