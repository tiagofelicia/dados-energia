#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_referencia_tecnologias.py
Escreve data/referencia/tecnologias.json — a tabela que traduz entre os três
vocabulários de tecnologias de geração que este repositório publica.

O PROBLEMA
----------
As três fontes nomeiam as mesmas centrais de formas que não coincidem em nada:

    REN (data/producao/)          Energy-Charts (data/mapas/)   ENTSO-E (producao-entsoe/)
    Hídrica                       hydro_run_of_river            Hydro Run-of-River
    Eólica                        wind_onshore                  Wind onshore
    Gás Natural - Ciclo Combinado gas                           Fossil gas
    Carvão                        coal_hard                     Fossil hard coal

Zero nomes em comum. Quem quiser comparar o mix português com o europeu tem de
escrever este mapeamento à mão, adivinhando pelo caminho — e há armadilhas:
a `Hídrica` da REN agrega o que as outras duas separam em três, e a
`Outra Térmica` é um saco de fuelóleo, gasóleo, resíduos e biogás que não
corresponde a nenhuma categoria das outras fontes.

PORQUE É UM SCRIPT E NÃO UM JSON À MÃO
--------------------------------------
O mapeamento em si é estático, mas as fontes mudam: o ENTSO-E acrescenta PSR
types, o Energy-Charts acrescenta chaves. Um JSON escrito à mão envelhece em
silêncio. Este script VALIDA o mapeamento contra as taxonomias realmente
presentes nos ficheiros publicados e avisa quando aparece um nome que não está
mapeado — que é o momento em que alguém tem de decidir onde ele encaixa.

    python gerar_referencia_tecnologias.py
    python gerar_referencia_tecnologias.py --verificar   # não escreve; sai !=0

FATORES DE EMISSÃO
------------------
Onde existem, são as medianas de ciclo de vida do IPCC AR5 (WG3, Annex III,
Tabela A.III.2) — os mesmos que o gerar_intensidade_carbonica.py usa. Quando o
AR5 não publica valor para a tecnologia, o campo fica `null` em vez de um número
inventado: é preferível uma lacuna honesta a um valor que ninguém consegue citar.
"""

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
PASTA_OUT = os.path.join(ROOT_DIR, "data", "referencia")
SAIDA = os.path.join(PASTA_OUT, "tecnologias.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

AR5 = "IPCC AR5 WG3 Annex III, Tabela A.III.2 (mediana, ciclo de vida)"

# categoria: renovavel · fossil · nuclear · armazenamento · fluxo · agregado
#
# 'ren', 'energy_charts' e 'entsoe' listam os nomes EXACTOS de cada fonte.
# Uma lista vazia significa que a fonte não publica essa tecnologia em separado.
# 'ren_agregada' marca os casos em que a coluna da REN cobre várias destas
# tecnologias ao mesmo tempo — não se pode desagregar a partir dos dados dela.
TECNOLOGIAS = {
    # ---------------- Renováveis ----------------
    "hidrica_fio_agua": dict(
        nome="Hídrica de fio de água", categoria="renovavel",
        gco2eq_kwh=24, fator_fonte=AR5 + " — Hydropower",
        ren=["Hídrica"], ren_agregada=True,
        energy_charts=["hydro_run_of_river"], entsoe=["Hydro Run-of-River"]),
    "hidrica_albufeira": dict(
        nome="Hídrica de albufeira", categoria="renovavel",
        gco2eq_kwh=24, fator_fonte=AR5 + " — Hydropower",
        ren=["Hídrica"], ren_agregada=True,
        energy_charts=["hydro_water_reservoir"], entsoe=["Hydro water reservoir"]),
    "eolica_onshore": dict(
        nome="Eólica terrestre", categoria="renovavel",
        gco2eq_kwh=11, fator_fonte=AR5 + " — Wind onshore",
        ren=["Eólica"], energy_charts=["wind_onshore"], entsoe=["Wind onshore"]),
    "eolica_offshore": dict(
        nome="Eólica offshore", categoria="renovavel",
        gco2eq_kwh=12, fator_fonte=AR5 + " — Wind offshore",
        ren=[], energy_charts=["wind_offshore"], entsoe=["Wind offshore"],
        nota="A REN não a separa; em Portugal a produção offshore é residual."),
    "solar": dict(
        nome="Solar fotovoltaica", categoria="renovavel",
        gco2eq_kwh=48, fator_fonte=AR5 + " — Solar PV, utility scale",
        ren=["Solar"], energy_charts=["solar"], entsoe=["Solar"],
        nota="O AR5 dá 41 para telhado e 48 para central; usa-se 48 por ser o "
             "que domina a injeção em rede."),
    "biomassa": dict(
        nome="Biomassa", categoria="renovavel",
        gco2eq_kwh=230, fator_fonte=AR5 + " — Biomass, dedicated",
        ren=["Biomassa"], energy_charts=["biomass"], entsoe=["Biomass"]),
    "ondas": dict(
        nome="Ondas e marés", categoria="renovavel",
        gco2eq_kwh=17, fator_fonte=AR5 + " — Ocean (tidal and wave)",
        ren=["Ondas"], energy_charts=[], entsoe=["Marine"]),
    "geotermica": dict(
        nome="Geotérmica", categoria="renovavel",
        gco2eq_kwh=38, fator_fonte=AR5 + " — Geothermal",
        ren=[], energy_charts=["geothermal"], entsoe=["Geothermal"],
        nota="A REN não a reporta em separado; nos Açores existe produção "
             "geotérmica fora do sistema continental."),
    "outras_renovaveis": dict(
        nome="Outras renováveis", categoria="renovavel",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=["other_renewables"], entsoe=["Other renewables"]),

    # ---------------- Fósseis ----------------
    "gas_natural": dict(
        nome="Gás natural", categoria="fossil",
        gco2eq_kwh=490, fator_fonte=AR5 + " — Gas, combined cycle",
        ren=["Gás Natural - Ciclo Combinado", "Gás natural - Cogeração"],
        energy_charts=["gas"], entsoe=["Fossil gas"],
        nota="A REN separa ciclo combinado de cogeração; as outras fontes não. "
             "A cogeração aloca parte das emissões ao calor útil, pelo que 490 "
             "sobrestima a sua parcela eléctrica."),
    "carvao": dict(
        nome="Carvão", categoria="fossil",
        gco2eq_kwh=820, fator_fonte=AR5 + " — Coal, PC",
        ren=["Carvão"], energy_charts=["coal_hard"], entsoe=["Fossil hard coal"],
        nota="Em Portugal a zero desde 2022: Sines fechou em janeiro de 2021 e "
             "Pego em novembro do mesmo ano."),
    "linhite": dict(
        nome="Linhite", categoria="fossil",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=["coal_lignite"],
        entsoe=["Fossil brown coal / lignite"],
        nota="Não existe em Portugal. O AR5 não publica mediana separada da do "
             "carvão; para estimativas, é mais intensivo que os 820 do carvão."),
    "petroleo": dict(
        nome="Produtos petrolíferos", categoria="fossil",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=["oil"], entsoe=["Fossil oil", "Fossil oil shale"],
        nota="Em Portugal está dentro de 'Outra Térmica', sem forma de separar."),
    "gas_de_carvao": dict(
        nome="Gás de carvão", categoria="fossil",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=["gas_coal_derived"],
        entsoe=["Fossil coal-derived gas"]),
    "turfa": dict(
        nome="Turfa", categoria="fossil",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=[], entsoe=["Fossil peat"]),
    "residuos": dict(
        nome="Resíduos", categoria="fossil",
        gco2eq_kwh=None, fator_fonte=None,
        ren=[], energy_charts=["waste"], entsoe=["Waste"],
        nota="Parcialmente biogénico; a classificação como fóssil é a convenção "
             "do ENTSO-E. Em Portugal está dentro de 'Outra Térmica'."),

    # ---------------- Nuclear ----------------
    "nuclear": dict(
        nome="Nuclear", categoria="nuclear",
        gco2eq_kwh=12, fator_fonte=AR5 + " — Nuclear",
        ren=[], energy_charts=["nuclear"], entsoe=["Nuclear"],
        nota="Não existe em Portugal. Relevante para o mix importado de Espanha."),

    # ---------------- Armazenamento ----------------
    "hidrica_bombagem": dict(
        nome="Hidroelétrica reversível (turbinagem)", categoria="armazenamento",
        gco2eq_kwh=24, fator_fonte=AR5 + " — Hydropower",
        ren=["Hídrica"], ren_agregada=True,
        energy_charts=["hydro_pumped_storage"], entsoe=["Hydro pumped storage"],
        nota="Não é geração primária: devolve à rede energia que antes consumiu. "
             "A REN publica a parcela turbinada em producao_bombagem_diaria.csv, "
             "à escala diária, e o consumo de bombagem na coluna 'Bombagem'."),
    "baterias": dict(
        nome="Baterias", categoria="armazenamento",
        gco2eq_kwh=0, fator_fonte="Emissão contabilizada no momento da carga",
        ren=["Injeção de Baterias"], energy_charts=["battery"],
        entsoe=["Energy storage"],
        nota="Contar emissões na descarga seria dupla contagem: já foram "
             "atribuídas à eletricidade usada para carregar."),

    # ---------------- Não é geração ----------------
    "importacao_exportacao": dict(
        nome="Saldo de trocas transfronteiriças", categoria="fluxo",
        gco2eq_kwh=None, fator_fonte=None,
        ren=["Importação", "Exportação"], energy_charts=["cross_border"],
        entsoe=["Cross border electricity trading"],
        nota="A REN publica importação e exportação em colunas separadas; as "
             "outras fontes publicam o saldo líquido."),
    "bombagem_consumo": dict(
        nome="Consumo de bombagem", categoria="consumo",
        gco2eq_kwh=None, fator_fonte=None,
        ren=["Bombagem"], energy_charts=["pumped_storage_consumption"],
        entsoe=["Hydro pumped storage consumption"],
        nota="Energia consumida a bombear água para a albufeira superior. É "
             "carga, não geração — entra no consumo do sistema e sai depois "
             "como 'hidrica_bombagem'."),
    "baterias_consumo": dict(
        nome="Carga de baterias", categoria="consumo",
        gco2eq_kwh=None, fator_fonte=None,
        ren=["Consumo Baterias"], energy_charts=["battery_consumption"],
        entsoe=["Energy storage consumption"],
        nota="Energia consumida a carregar baterias. É onde as emissões da "
             "descarga ficam contabilizadas."),
    "consumo": dict(
        nome="Consumo total", categoria="agregado",
        gco2eq_kwh=None, fator_fonte=None,
        ren=["Consumo"], energy_charts=["load"], entsoe=["Load"]),
    "outros": dict(
        nome="Outros / não especificado", categoria="agregado",
        gco2eq_kwh=None, fator_fonte=None,
        ren=["Outra Térmica"], energy_charts=["other"], entsoe=["Others"],
        nota="A 'Outra Térmica' da REN agrega fuelóleo, gasóleo, resíduos e "
             "biogás — não corresponde ao 'other' das outras fontes, que é "
             "residual. Pesa menos de 1 % da produção nacional."),
}

# Nomes que aparecem nas fontes e que NÃO são tecnologias a mapear.
IGNORAR_EC = {"residual_load"}

# O ENTSO-E publica uma variante " consumption" para quase todas as tecnologias,
# mesmo onde não faz sentido (uma central solar não consome da rede). Só duas
# correspondem a carga real de armazenamento e são mapeadas; as restantes são
# artefacto da forma como o A75 codifica a direção e ficam de fora.
ENTSOE_CONSUMO_REAL = {"Hydro pumped storage consumption", "Energy storage consumption"}


# ============================================================
# Recolha das taxonomias reais
# ============================================================

def taxonomia_ren():
    p = os.path.join(ROOT_DIR, "data", "producao", "producao_dados_atuais.csv")
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8-sig") as f:
        # as 3 primeiras colunas são dia/hora/intervalo
        return set(f.readline().strip().split(",")[3:])


def taxonomia_energy_charts():
    nomes = set()
    for f in sorted(glob.glob(os.path.join(ROOT_DIR, "data", "mapas", "producao", "2*.json")))[-3:]:
        try:
            for dia in json.load(open(f, encoding="utf-8")).values():
                for pais in dia.values():
                    if isinstance(pais, dict):
                        nomes |= set(pais.get("production", {}))
        except (OSError, ValueError):
            continue
    return nomes


def taxonomia_entsoe():
    nomes = set()
    padrao = os.path.join(ROOT_DIR, "data", "producao", "producao-entsoe", "*", "*.json")
    for f in glob.glob(padrao)[:400]:          # amostra: os nomes repetem-se
        try:
            for s in json.load(open(f, encoding="utf-8")).get("production_types", []):
                nomes.add(s["name"])
        except (OSError, ValueError, KeyError):
            continue
    # O cliente também declara a lista canónica; juntá-la cobre tecnologias que
    # nenhum país publicou ainda no período recolhido.
    cli = os.path.join(SCRIPT_DIR, "entsoe_client.py")
    if os.path.exists(cli):
        s = open(cli, encoding="utf-8").read()
        i = s.find("PSR_TYPES = {")
        if i > 0:
            nomes |= set(re.findall(r"'B\d\d':\s*'([^']+)'", s[i:s.find("}", i)]))
    return nomes


def validar():
    """Confronta o mapeamento com o que está mesmo nos ficheiros."""
    reais = {
        "REN": taxonomia_ren(),
        "Energy-Charts": taxonomia_energy_charts() - IGNORAR_EC,
        "ENTSO-E": {n for n in taxonomia_entsoe()
                    if not n.endswith(" consumption") or n in ENTSOE_CONSUMO_REAL},
    }
    mapeados = {"REN": set(), "Energy-Charts": set(), "ENTSO-E": set()}
    for t in TECNOLOGIAS.values():
        mapeados["REN"] |= set(t["ren"])
        mapeados["Energy-Charts"] |= set(t["energy_charts"])
        mapeados["ENTSO-E"] |= set(t["entsoe"])

    problemas = []
    for fonte in reais:
        for nome in sorted(reais[fonte] - mapeados[fonte]):
            problemas.append(f"{fonte}: '{nome}' existe nos dados e não está mapeado")
        for nome in sorted(mapeados[fonte] - reais[fonte]):
            problemas.append(f"{fonte}: '{nome}' está mapeado e não existe nos dados")
    return reais, mapeados, problemas


def main():
    p = argparse.ArgumentParser(
        description="Gera data/referencia/tecnologias.json.")
    p.add_argument("--verificar", action="store_true",
                   help="não escreve; sai !=0 se o mapeamento divergir das fontes")
    args = p.parse_args()

    print("🔤 Referência de tecnologias")
    reais, mapeados, problemas = validar()
    for fonte in ("REN", "Energy-Charts", "ENTSO-E"):
        print(f"  {fonte:14s} {len(reais[fonte]):3d} nomes nos dados · "
              f"{len(mapeados[fonte]):3d} mapeados")

    if problemas:
        print(f"\n⚠️  {len(problemas)} divergência(s):")
        for x in problemas:
            print(f"     {x}")
    else:
        print("\n  ✓ mapeamento cobre exactamente o que está nos dados")

    if args.verificar:
        print("\n(--verificar: nada foi escrito)")
        sys.exit(1 if problemas else 0)

    saida = {
        "descricao": "Tabela de tradução entre os vocabulários de tecnologias de "
                     "geração usados pelas três fontes deste repositório: REN "
                     "(data/producao/), Energy-Charts (data/mapas/producao/) e "
                     "ENTSO-E (data/producao/producao-entsoe/).",
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unidade_fatores": "gCO2eq/kWh",
        "nota_fatores": "Medianas de ciclo de vida do IPCC AR5. Onde o AR5 não "
                        "publica valor, o campo é null — não há estimativa "
                        "inventada. Não são emissões directas de combustão.",
        "nota_ren_agregada": "ren_agregada=true significa que a coluna da REN "
                             "cobre várias destas tecnologias ao mesmo tempo e "
                             "não é possível desagregá-las a partir dos dados "
                             "dela. É o caso da 'Hídrica', que soma fio de água, "
                             "albufeira e turbinagem de bombagem.",
        "tecnologias": TECNOLOGIAS,
    }

    os.makedirs(PASTA_OUT, exist_ok=True)
    novo = json.dumps(saida, ensure_ascii=False, indent=1, sort_keys=True)
    antigo = None
    if os.path.exists(SAIDA):
        try:
            a = json.load(open(SAIDA, encoding="utf-8"))
            b = json.loads(novo)
            a.pop("gerado_em", None)
            b.pop("gerado_em", None)
            antigo = (a == b)
        except (OSError, ValueError):
            antigo = False
    if antigo is True:
        print(f"\n= data/referencia/tecnologias.json (sem alterações)")
    else:
        with open(SAIDA, "w", encoding="utf-8", newline="") as f:
            f.write(novo + "\n")
        kb = len(novo.encode("utf-8")) / 1024
        print(f"\n✓ data/referencia/tecnologias.json — "
              f"{len(TECNOLOGIAS)} tecnologias · {kb:.1f} KB")

    if problemas:
        sys.exit(1)


if __name__ == "__main__":
    main()
