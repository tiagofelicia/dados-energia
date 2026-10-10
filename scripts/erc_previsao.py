#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
erc_previsao.py
ERC da REN (Encargos de Regulação imputados ao Consumo) por quarto de hora: o
valor real onde a REN já publicou e uma previsão no resto. É usado por:

  atualizar_precos-horarios_csv.py      Preços Horários (hoje e amanhã)
  atualizar_tarifarios_eletricidade.py  coluna ERC dos ficheiros por ano da
                                        OMIE_PERDAS_CICLOS, que os simuladores leem

Os dois chamam as mesmas funções, por isso dão o mesmo número para o mesmo
quarto de hora.

FONTE
-----
data/erc/isp/erc_isp_AAAA-MM.csv (recolhidos pelo atualizar_erc_ren.py), coluna
erc_total_eur_mwh. Os ficheiros estão em dia de MERCADO (o período 1 começa às
23:00 do dia anterior); aqui tudo passa para hora de Portugal a partir do
data_utc.

TRÊS TROÇOS
-----------
  1. Real — até ao último quarto de hora publicado. Sem corte nem médias: é o
     valor que o comercializador fatura.
  2. Curto prazo — até CURTO_PRAZO_DIAS depois do último dia publicado:
         ERC(s) = A(s) + [B_T(s) − C(s)]
       A   média do slot s nos últimos 7 dias publicados (o nível recente);
       B_T média de s nos dias do tipo T (útil, sábado, domingo) das últimas
           8 semanas;
       C   média de s em todos os dias das últimas 8 semanas.
     A janela de 7 dias mistura 5 dias úteis com um sábado e um domingo; o
     termo B−C repõe a diferença própria do tipo de dia (ao domingo de manhã
     chega a +12 €/MWh: o consumo cai e boa parte do ERC é custo fixo por hora).
  3. Longo prazo — daí em diante:
         ERC(s) = N + [P_T(s) − média(P_T)]
       N   média dos últimos 365 dias publicados (o nível);
       P_T média de s nos dias do tipo T à volta da mesma data do ano anterior
           (±21 dias; −364 dias para alinhar o dia da semana).
     O perfil das últimas 8 semanas serve a 2–4 semanas de distância, mas a 60
     dias erra a estação: no verão o ERC é alto ao meio-dia e baixo à noite, no
     inverno é quase o contrário.

Testado com este módulo, simulando em cada dia os dados que haveria disponíveis
(erro médio por quarto de hora, €/MWh):
  curto prazo, 3 dias de antecedência (out/2025–set/2026)   5,0  (constante: 8,8)
  longo prazo, 60 dias de antecedência (abr/2025–set/2026)  7,2  (valor plano: ~8,1)
e no que pesa numa fatura bi-horária, a diferença fora de vazio − vazio de cada
mês, a 60 dias: 2,9 contra ~4,3 do valor plano. Os feriados NÃO se comportam
como domingos, por isso não há calendário de feriados.

Nas previsões cada quarto de hora entra cortado a LIMITES_MWH antes das médias:
há valores isolados entre −629 e +1328 €/MWh (desvios, o apagão de 28/04/2025)
que, sem corte, puxavam um slot durante dias (a previsão chegou a 302 €/MWh).

Se o último dia publicado tiver mais de ATRASO_MAX_DIAS, não há previsões: quem
chama volta às constantes da BD. Os valores reais continuam a valer.
"""

import glob
import os
from datetime import datetime

import numpy as np
import pandas as pd

LIMITES_MWH = (-10.0, 60.0)
JANELA_NIVEL_DIAS = 7
JANELA_TIPO_DIAS = 56
CURTO_PRAZO_DIAS = 14
JANELA_LONGO_DIAS = 365
MEIA_JANELA_ANO_ANTERIOR_DIAS = 21
ATRASO_MAX_DIAS = 30
FUSO = "Europe/Lisbon"


def tipo_de_dia(data):
    """'util', 'sab' ou 'dom' (feriados contam como o dia da semana em que caem)."""
    dia_semana = pd.Timestamp(data).weekday()
    return "sab" if dia_semana == 5 else ("dom" if dia_semana == 6 else "util")


def ler_ficheiros(pasta_isp):
    """Os ERC publicados (data_utc, u em €/MWh), ou None se não houver."""
    ficheiros = sorted(glob.glob(os.path.join(pasta_isp, "erc_isp_????-??.csv")))
    if not ficheiros:
        print(f"⚠️ ERC: sem ficheiros em '{pasta_isp}'.")
        return None
    erc = pd.concat([pd.read_csv(f, encoding="utf-8-sig", usecols=["data_utc", "erc_total_eur_mwh"])
                     for f in ficheiros], ignore_index=True)
    erc["u"] = pd.to_numeric(erc["erc_total_eur_mwh"], errors="coerce")
    return erc.dropna(subset=["u"])[["data_utc", "u"]].reset_index(drop=True)


def carregar(pasta_isp, hoje=None, ate=None):
    """Lê os ficheiros e prepara-os (ver preparar)."""
    erc = ler_ficheiros(pasta_isp)
    return None if erc is None else preparar(erc, hoje, ate)


def preparar(erc, hoje=None, ate=None):
    """
    Prepara o que as previsões precisam, a partir de ler_ficheiros().

    hoje  data de referência para o atraso (por omissão, o dia de hoje);
    ate   corta os dados nesse dia (hora de Portugal) — só para testes, para
          simular o que haveria disponível numa data passada.

    Devolve None se não houver valores; senão um dicionário com:
      reais       Series €/MWh indexada pelo início do quarto de hora em UTC
      tabela      DataFrame dia × 'HH:MM' (hora de Portugal) com o ERC cortado
      tipos       Series com o tipo de cada dia da tabela
      primeiro, ultimo   primeiro e último dia publicados (hora de Portugal)
      atraso      dias entre o último publicado e 'hoje'
      desatualizado      atraso > ATRASO_MAX_DIAS (sem previsões)
      nivel_longo média dos últimos 365 dias (o N do troço 3)
    """
    utc = pd.to_datetime(erc["data_utc"], format="%Y-%m-%d %H:%M", utc=True)
    local = utc.dt.tz_convert(FUSO)
    dia = local.dt.tz_localize(None).dt.normalize()
    if ate is not None:
        manter = (dia <= pd.Timestamp(ate).normalize()).values
        erc, utc, local, dia = erc[manter], utc[manter], local[manter], dia[manter]
    if erc.empty:
        print("⚠️ ERC: os ficheiros não têm valores.")
        return None

    reais = pd.Series(erc["u"].values, index=pd.DatetimeIndex(utc.values, tz="UTC"))
    reais = reais[~reais.index.duplicated(keep="last")].sort_index()

    # dia × slot, com o corte; o recuo da hora junta os dois 01:xx no mesmo slot
    cortado = erc["u"].clip(*LIMITES_MWH).values
    tabela = (pd.DataFrame({"dia": dia.values, "hhmm": local.dt.strftime("%H:%M").values, "u": cortado})
              .groupby(["dia", "hhmm"])["u"].mean().unstack())
    ultimo = tabela.index.max()
    hoje = pd.Timestamp(hoje if hoje is not None else datetime.now()).normalize()
    atraso = (hoje - ultimo).days
    ultimo_ano = tabela[tabela.index > ultimo - pd.Timedelta(days=JANELA_LONGO_DIAS)]
    return {
        "reais": reais,
        "tabela": tabela,
        "tipos": pd.Series([tipo_de_dia(d) for d in tabela.index], index=tabela.index),
        "primeiro": tabela.index.min(),
        "ultimo": ultimo,
        "atraso": atraso,
        "desatualizado": atraso > ATRASO_MAX_DIAS,
        "nivel_longo": float(np.nanmean(ultimo_ano.values)),
    }


def prever_dia(dados, dia):
    """
    Previsão do ERC (€/MWh) de cada quarto de hora de um dia, por 'HH:MM' em
    hora de Portugal. Devolve (Series, troço) com troço 'curto' ou 'longo', ou
    (None, None) se não houver dados, se estiverem desatualizados ou se o dia
    for anterior ao primeiro publicado.
    """
    if dados is None or dados["desatualizado"]:
        return None, None
    dia = pd.Timestamp(dia).normalize()
    if dia < dados["primeiro"]:
        return None, None
    tab, tipos, ultimo = dados["tabela"], dados["tipos"], dados["ultimo"]
    tipo = tipo_de_dia(dia)

    if (dia - ultimo).days <= CURTO_PRAZO_DIAS:
        nivel = tab[tab.index > ultimo - pd.Timedelta(days=JANELA_NIVEL_DIAS)].mean()
        janela = tab[tab.index > ultimo - pd.Timedelta(days=JANELA_TIPO_DIAS)]
        do_tipo = janela[(tipos.loc[janela.index] == tipo).values].mean()
        # Sem dados do tipo num slot, fica só o nível
        prev = (nivel + (do_tipo - janela.mean())).fillna(nivel)
        return prev.dropna(), "curto"

    # Longo prazo: perfil à volta da mesma data do ano anterior; se esse ano
    # ainda não tiver dados (o horizonte do simulador vai até ao fim do ano
    # seguinte), o ano antes desse
    perfil = None
    for anos in (1, 2, 3):
        centro = dia - pd.Timedelta(days=364 * anos)
        meia = pd.Timedelta(days=MEIA_JANELA_ANO_ANTERIOR_DIAS)
        janela = tab[(tab.index >= centro - meia) & (tab.index <= centro + meia)]
        janela = janela[(tipos.loc[janela.index] == tipo).values]
        if len(janela) >= 3:
            perfil = janela.mean()
            break
    nivel = dados["nivel_longo"]
    if perfil is None or perfil.isna().all():
        return pd.Series(nivel, index=tab.columns), "longo"
    return (nivel + (perfil - perfil.mean())).dropna(), "longo"


def erc_instantes(dados, inicios_utc):
    """
    ERC (€/MWh) de cada quarto de hora pedido, pelo instante de início em UTC:
    o real se a REN já o publicou, senão a previsão do dia (hora de Portugal).
    NaN onde não há nem uma nem outra.

    Devolve (array, contagem) com a contagem por troço: real, curto, longo, sem.
    """
    idx = pd.DatetimeIndex(inicios_utc)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    contagem = {"real": 0, "curto": 0, "longo": 0, "sem": 0}
    if dados is None:
        contagem["sem"] = len(idx)
        return np.full(len(idx), np.nan), contagem

    valores = dados["reais"].reindex(idx).to_numpy(dtype=float, copy=True)
    contagem["real"] = int((~np.isnan(valores)).sum())
    falta = np.isnan(valores)
    if falta.any():
        local = idx[falta].tz_convert(FUSO)
        dias = local.tz_localize(None).normalize()
        hhmm = local.strftime("%H:%M")
        previsoes = {d: prever_dia(dados, d) for d in pd.unique(dias)}
        preenchidos = np.full(len(dias), np.nan)
        for k, (d, h) in enumerate(zip(dias, hhmm)):
            prev, troco = previsoes[d]
            if prev is not None and h in prev.index:
                preenchidos[k] = prev[h]
                contagem[troco] += 1
            else:
                contagem["sem"] += 1
        valores[falta] = preenchidos
    return valores, contagem
