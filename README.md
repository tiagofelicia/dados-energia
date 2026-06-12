# dados-energia

Dados do mercado de energia em Portugal — preços OMIE/OMIP, produção REN e mapas europeus (ENTSO-E) — **atualizados automaticamente** várias vezes por dia via GitHub Actions.

Este repositório alimenta os simuladores e dashboards de **[www.tiagofelicia.pt](https://www.tiagofelicia.pt)** e está aberto a qualquer pessoa que queira reutilizar os dados (ver [Licença](#licença-e-atribuição)).

## Como aceder

Todos os ficheiros são públicos, sem registo nem chave de API. Página de apresentação: **[dados.tiagofelicia.pt](https://dados.tiagofelicia.pt/)**.

```
https://dados.tiagofelicia.pt/data/<caminho>
```

Exemplos:

```bash
# curl
curl -s https://dados.tiagofelicia.pt/data/omie/omie_dados_atuais.csv
```

```python
# Python / pandas
import pandas as pd
df = pd.read_csv("https://dados.tiagofelicia.pt/data/omie/omie_dados_atuais.csv")
```

```js
// JavaScript (CORS aberto: Access-Control-Allow-Origin: *)
const r = await fetch("https://dados.tiagofelicia.pt/data/omie/records_omie.json");
const recordes = await r.json();
```

Espelho alternativo (mesmos caminhos): `https://raw.githubusercontent.com/tiagofelicia/dados-energia/main/data/<caminho>`

## Estrutura

```
data/
├── omie/        Preços do mercado diário OMIE (e futuros OMIP)
├── producao/    Produção elétrica em Portugal (REN)
├── mapas/       Preços e mix de produção por país/zona europeia
└── regulado/    Dados regulados ERSE/E-Redes (atualização anual)
```

## Datasets

### `data/omie/` — Preços de mercado

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `omie_dados_atuais.csv` | Preços quarto-horários do ano corrente, incluindo datas futuras estimadas a partir dos futuros OMIP | ~5×/dia útil |
| `historico/omie_historico_AAAA.csv` | Séries históricas anuais (2010–presente), mesmo schema | Fecho de ano |
| `precos-horarios.csv` | Preço quarto-horário final por tarifário indexado e opção horária (inclui bloco `TABELA_CONSTANTES` no fim) | ~5×/dia útil |
| `records_omie.json` | Recordes históricos OMIE (dia/hora mais caro e mais barato, etc.) | Diária (incremental) |
| `MIBEL_ano_atual_ACUM.csv` | ⚙️ Intermédio de pipeline — preços horários PT/ES acumulados dos últimos 12 meses. Não recomendado para consumo direto | ~5×/dia útil |

Schema de `omie_dados_atuais.csv` e dos históricos:

```
dia,hora,intervalo,Simples,BD,BS,TD,TS,preco_pt,preco_es
01/01/2026,00:15,[00:00-00:15[,S,V,V,V,V,104.70,104.70
```

- `dia` em `DD/MM/AAAA`; `intervalo` de 15 minutos; preços em **€/MWh**.
- `Simples`, `BD` (bi-horário diário), `BS` (bi-horário semanal), `TD` (tri-horário diário), `TS` (tri-horário semanal): classificação do período horário BTN no intervalo (V = Vazio, C = Cheias, P = Ponta, F = Fora de Vazio, S = Simples). Ciclos oficiais em [tiagofelicia.pt/periodos-horarios](https://www.tiagofelicia.pt/periodos-horarios).
- Antes da entrada em vigor da negociação quarto-horária, os valores horários são replicados pelos 4 intervalos.
- As fórmulas dos tarifários indexados de `precos-horarios.csv` estão documentadas em [tiagofelicia.pt/formulas-tarifarios-indexados](https://www.tiagofelicia.pt/formulas-tarifarios-indexados).

### `data/producao/` — Produção elétrica (Portugal)

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `producao_dados_atuais.csv` | Produção por fonte no ano corrente, potência média (MW) por intervalo de 15 min | A cada 4 h |
| `historico/producao_historico_AAAA.csv` | Séries históricas anuais (2010–presente), mesmo schema | Fecho de ano |
| `producao_bombagem_diaria.csv` | Consumo diário em bombagem hidroelétrica (GWh), desde 2010 | A cada 4 h |
| `records_producao.json` | Recordes (dia mais renovável, maior consumo, pico, etc.) + agregados mensais pré-calculados | A cada 4 h (incremental) |

Schema dos CSV de produção:

```
dia,hora,intervalo,Hídrica,Eólica,Solar,Biomassa,Ondas,Gás Natural - Ciclo Combinado,
Gás natural - Cogeração,Carvão,Outra Térmica,Importação,Exportação,Bombagem,
Injeção de Baterias,Consumo Baterias,Consumo
```

### `data/mapas/` — Europa

| Pasta | Conteúdo | Atualização |
|---|---|---|
| `precos_qh/AAAA-MM.json` | Preços day-ahead por zona de mercado europeia (ENTSO-E), um ficheiro por mês desde 2018-01. Por dia e zona: `avg`, `min`, `max` (+ horas) e `values` quarto-horários em €/MWh | 2×/dia |
| `precos_qh/metadata.json` | `{"ultima_data": "AAAA-MM-DD"}` | 2×/dia |
| `producao/AAAA-MM.json` | Mix de produção diário por país europeu (GWh/dia por tecnologia + consumo), desde 2026-01 | 3×/dia |
| `producao/metadata.json` | `{"ultima_data": "AAAA-MM-DD"}` | 3×/dia |

### `data/regulado/` — Dados regulados (ERSE / E-Redes)

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `perfis_erse_9.json` | Perfis de consumo BTN publicados pela ERSE | Anual |
| `Perdas_calculadas_2026_TF.csv` | Fatores de perdas (1+perdas) calculados para BT e MT, a partir dos perfis de perdas da E-Redes | Anual |
| `tos_municipios.json` | Taxa de Ocupação do Subsolo (TOS) por município + ORD/CUR de gás natural respetivo | Quando há alterações |

## Fontes originais

Os dados são recolhidos e processados a partir de fontes oficiais e públicas:

- **[OMIE](https://www.omie.es)** — preços do mercado diário (MIBEL)
- **[OMIP](https://www.omip.pt)** — mercado a prazo / futuros
- **[REN](https://datahub.ren.pt)** — produção, consumo e bombagem em Portugal
- **[ENTSO-E Transparency Platform](https://transparency.entsoe.eu)** — preços day-ahead europeus
- **[Energy-Charts](https://www.energy-charts.info)** (Fraunhofer ISE) — mix de produção europeu
- **[ERSE](https://www.erse.pt)** e **[E-Redes](https://www.e-redes.pt)** — perfis, perdas e tarifas reguladas

Os dados originais pertencem às respetivas entidades e podem estar sujeitos aos seus próprios termos de utilização. Este repositório disponibiliza **compilações e séries derivadas** desses dados, organizadas para consumo direto.

## Licença e atribuição

As compilações e os datasets derivados deste repositório estão licenciados sob **[CC BY 4.0](LICENSE)** (Creative Commons Atribuição 4.0 Internacional).

Pode copiar, redistribuir e adaptar os dados, inclusive para fins comerciais, desde que dê o devido crédito. Forma de citação sugerida:

> Dados: Tiago Felícia — [www.tiagofelicia.pt](https://www.tiagofelicia.pt) (fontes originais: OMIE, REN, ENTSO-E), via [dados.tiagofelicia.pt](https://dados.tiagofelicia.pt/)

## Avisos

- Os dados são disponibilizados "tal como estão", sem garantias. Podem existir falhas, atrasos ou correções retroativas nas fontes originais.
- Os ficheiros marcados com ⚙️ são intermédios do pipeline e o seu formato pode mudar sem aviso.
- Os nomes e caminhos dos restantes ficheiros são **estáveis**: alterações que partam URLs serão evitadas e, quando inevitáveis, anunciadas neste README.

## Contacto

Sugestões, erros ou dúvidas: [www.tiagofelicia.pt/contacto](https://www.tiagofelicia.pt/contacto)
