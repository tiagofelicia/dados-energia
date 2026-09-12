# dados-energia

Dados do mercado de energia em Portugal — preços OMIE/OMIP, gás MIBGAS, produção REN, mapas europeus (ENTSO-E) e intensidade carbónica — **atualizados automaticamente** várias vezes por dia via GitHub Actions.

Este repositório alimenta os simuladores e dashboards de **[www.tiagofelicia.pt](https://www.tiagofelicia.pt)** e está aberto a qualquer pessoa que queira reutilizar os dados (ver [Licença](#licença-e-atribuição)).

## Como aceder

Todos os ficheiros são públicos, sem registo nem chave de API. Página de apresentação: **[dados.tiagofelicia.pt](https://dados.tiagofelicia.pt/)**.

```
https://dados.tiagofelicia.pt/data/<caminho>
```

Exemplos:

```bash
# curl
curl -s https://dados.tiagofelicia.pt/data/agregados/omie_anual.csv
```

```python
# Python / pandas
import pandas as pd
df = pd.read_csv("https://dados.tiagofelicia.pt/data/agregados/omie_mensal.csv")
```

```js
// JavaScript (CORS aberto: Access-Control-Allow-Origin: *)
const r = await fetch("https://dados.tiagofelicia.pt/data/omie/hoje.json");
const hoje = await r.json();
console.log(hoje.hoje.pt.medio);   // preço médio de hoje, €/MWh
```

Espelho alternativo (mesmos caminhos): `https://raw.githubusercontent.com/tiagofelicia/dados-energia/main/data/<caminho>`

### Catálogo e estado

- **[`data/manifest.json`](https://dados.tiagofelicia.pt/data/manifest.json)** — catálogo legível por máquina: para cada dataset, o caminho, o schema, a cadência de atualização, a última data disponível e os avisos relevantes.
- **[Estado dos dados](https://dados.tiagofelicia.pt/status.html)** — mostra se algum dataset está atrasado face à cadência prometida. O cálculo é feito no navegador, pelo que a página continua a reportar atrasos mesmo que os processos de recolha parem.

### Por onde começar

Se quer médias e totais, use **[`data/agregados/`](#dataagregados--séries-pré-calculadas)** em vez das séries quarto-horárias: 2 KB em vez de 32 MB. Se quer mostrar o dia corrente numa página, use os **`hoje.json`**.

## Estrutura

```
data/
├── omie/         Preços do mercado diário OMIE e futuros OMIP
├── producao/     Produção elétrica em Portugal (REN) e na Europa (ENTSO-E)
├── gas/          Mercado ibérico de gás natural (MIBGAS)
├── mapas/        Preços e mix de produção por país/zona europeia
├── agregados/    Médias e totais pré-calculados (diário, mensal, anual)
├── emissoes/     Intensidade carbónica da produção elétrica
├── referencia/   Tabelas de referência (tecnologias, vocabulários)
├── regulado/     Dados regulados ERSE/E-Redes (atualização anual)
└── manifest.json Catálogo de tudo o que está acima
```

## Datasets

### `data/omie/` — Preços de mercado

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `omie_dados_atuais.csv` | Preços quarto-horários do ano corrente, incluindo datas futuras estimadas a partir dos futuros OMIP | ~5×/dia útil |
| `historico/omie_historico_AAAA.csv` | Séries históricas anuais (2010–presente), mesmo schema | Fecho de ano |
| `hoje.json` | Instantâneo do dia: preços de hoje e de amanhã, médias por período do ciclo horário (~8 KB) | A cada atualização |
| `precos-horarios.csv` | Preço quarto-horário final por tarifário indexado e opção horária | ~5×/dia útil |
| `futuros_omip.csv` | Versão leve só com as tabelas de metadados e futuros OMIP da sessão em curso (~3 KB) | ~5×/dia útil |
| `historico/omip_historico_AAAA.csv` | Histórico de sessões OMIP: uma linha por sessão × zona × contrato | Diária |
| `records_omie.json` | Recordes históricos OMIE (dia/hora mais caro e mais barato, etc.) + agregados mensais e anuais | Diária (incremental) |
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

⚠️ **Dois avisos de formato**, importantes antes de escrever código sobre estes ficheiros:

- **`omie_dados_atuais.csv` tem blocos extra no fim** (`TABELA_ATUALIZACOES`, `TABELA_FUTUROS_PT`, `TABELA_FUTUROS_ES`), a partir da linha ~35 135. Um `pd.read_csv` simples traz essas linhas como se fossem dados. Corte no primeiro `TABELA_`, ou use os [agregados](#dataagregados--séries-pré-calculadas) se só precisar de médias.
- **`precos-horarios.csv` não abre com `pd.read_csv` sem argumentos.** Tem duas tabelas concatenadas *horizontalmente* — `TABELA_HORARIA` a partir da coluna 11 e `TABELA_CONSTANTES` a partir da 19 — o que produz linhas de 8, 11, 16, 19 e 20 campos e um `ParserError`.

Além disso, `omie_dados_atuais.csv` contém **datas futuras estimadas a partir dos futuros OMIP**, não preços de mercado. O último dia real está declarado em `futuros_omip.csv`, na chave `Data_Valores_OMIE` — as datas posteriores são estimativas. Os agregados e a intensidade carbónica já as excluem.

### `data/producao/` — Produção elétrica

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `producao_dados_atuais.csv` | Produção por fonte no ano corrente, potência média (MW) por intervalo de 15 min | A cada 4 h |
| `historico/producao_historico_AAAA.csv` | Séries históricas anuais (2010–presente), mesmo schema | Fecho de ano |
| `hoje.json` | Instantâneo do dia: mix quarto-horário, resumo diário e estado no último intervalo (~15 KB) | A cada atualização |
| `producao_bombagem_diaria.csv` | Consumo diário em bombagem hidroelétrica (GWh), desde 2010 | A cada 4 h |
| `records_producao.json` | Recordes (dia mais renovável, maior consumo, pico, etc.) + agregados mensais pré-calculados | A cada 4 h (incremental) |
| `producao-entsoe/{cc}/AAAA-WSS.json` | **Produção europeia intra-diária** para 35 países: geração por tecnologia, consumo, previsões day-ahead e intradiárias, preços e fluxos transfronteiriços. Um ficheiro por país e semana ISO | A cada 4 h (PT/ES) · 2×/dia (todos) |
| `producao-entsoe/metadata.json` | Última atualização e código EIC por país | idem |

Schema dos CSV de produção:

```
dia,hora,intervalo,Hídrica,Eólica,Solar,Biomassa,Ondas,Gás Natural - Ciclo Combinado,
Gás natural - Cogeração,Carvão,Outra Térmica,Importação,Exportação,Bombagem,
Injeção de Baterias,Consumo Baterias,Consumo
```

Valores em **MW** (potência média no intervalo). Para energia: `MW × 0,25 = MWh`.

Os ficheiros de `producao-entsoe/` têm um schema próprio e mais rico, com `timezone`, `interval_minutes` e `unix_seconds` explícitos:

```json
{
  "country": "pt", "iso_year": 2026, "iso_week": 35,
  "timezone": "Europe/Lisbon", "interval_minutes": 60,
  "production_types": [ { "name": "Wind onshore", "data": [ … ] } ],
  "forecast": { … }, "price": { … }, "exchanges_forecast": { … }
}
```

### `data/gas/` — Mercado de gás natural (MIBGAS)

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `mibgas_spot.csv` | Índices diários do mercado ibérico de gás, desde 17/12/2015 | 2×/dia |
| `mibgas_futuros.csv` | Curva forward do gás ibérico em preço absoluto, do intradiário ao ano Y+2, desde 16/12/2015 | 2×/dia |
| `mibgas_ttf_spread.csv` | Prémio do gás ibérico face ao benchmark europeu TTF, 15 produtos de D+1 a Y+2, desde 02/01/2024 | 2×/dia |
| `metadata.json` | Última data, primeira data e cobertura | 2×/dia |

Schema de `mibgas_spot.csv`:

```
dia,data_iso,mibgas_pt,mibgas_es,vtp_last,vtp_avg,pvb_last,pvb_avg,lng_es,avb_es
11/09/2026,2026-09-11,81.59,81.92,83.01,81.65,83.46,82.50,81.34,81.00
12/09/2026,2026-09-12,,,80.60,81.30,81.31,81.55,,
```

| Coluna | Índice | Disponível desde |
|---|---|---|
| `mibgas_pt` | Índice de referência MIBGAS-PT | 17/03/2021 |
| `mibgas_es` | Índice de referência MIBGAS-ES | 17/12/2015 |
| `vtp_last` / `vtp_avg` | VTP — hub português, última transação / média ponderada | 01/01/2023 |
| `pvb_last` / `pvb_avg` | PVB — hub espanhol, última transação / média ponderada | 01/01/2023 |
| `lng_es` | GNL Espanha | 2018 (esparso) |
| `avb_es` | Armazenamento Espanha | 2021 |

- Preços em **€/MWh** (PCS). **Um campo vazio significa "não publicado nesse dia", nunca zero.**
- Só preços reais de mercado: sem futuros, sem preenchimento de lacunas. Os índices de referência PT/ES saem com cerca de três dias de atraso e preenchem-se depois — é o que mostra a segunda linha do exemplo, com os hubs já publicados e `mibgas_pt`/`mibgas_es` ainda vazios.
**Que coluna usar.** Há duas famílias de índices e é fácil confundi-las:

- **Hubs** — `vtp_last` / `vtp_avg` (VTP, Portugal) e `pvb_last` / `pvb_avg` (PVB, Espanha). São o preço negociado em cada ponto virtual. A correspondência natural é `vtp_last` para Portugal e `pvb_last` para Espanha.
- **Índices de referência** — `mibgas_pt` e `mibgas_es`. Calculados por outra metodologia, e o `mibgas_es` é o único com série desde 2015.

Os dois hubs andam praticamente colados: entre 2023 e 2026, o `vtp_last` afastou-se do `pvb_last` mais de 1 €/MWh em apenas **5 % dos dias** (média de +0,05 €/MWh). Já a diferença entre o índice de referência e o hub do mesmo país é maior — 30 % dos dias acima de 1 €/MWh. Ou seja: **misturar famílias engana mais do que comparar países.**

Se estiver a replicar a fórmula de um comercializador, confirme qual das quatro colunas ela refere.

#### `mibgas_futuros.csv`

```
dia,data_iso,produto,area,horizonte,rotulo,entrega_inicio,entrega_fim,dias_entrega,preco_ultimo,preco_referencia,volume_mwh
04/09/2026,2026-09-04,GYES_Y+1,ES,ano,2027,2027-01-01,2027-12-31,365,52.43,52.43,0.0
```

Os produtos a prazo negociados no MIBGAS, em **€/MWh**. Filtre por `horizonte`, que
vai do mais curto ao mais longo: `intradiario`, `dia`, `fim-de-semana`,
`resto-do-mes`, `mes`, `trimestre`, `estacao`, `ano`. O horizonte é lido do código do
produto, não deduzido das datas.

Atenção a `intradiario` *vs* `dia`: o intradiário (`GWD*`) entrega **no próprio dia da
sessão** e o day-ahead (`GDA*`) entrega de D+1 a D+3. Os dois têm `dias_entrega = 1`,
pelo que filtrar só por duração os confunde.

| Coluna | |
|---|---|
| `dia` | Dia de **negociação** (a cotação), não de entrega |
| `rotulo` | O período de entrega por extenso: `Maio 2025`, `3.º Trimestre 2025`, `Inverno 2025/26`, `2027` |
| `preco_ultimo` | *Last Price* — sinal de fecho da sessão; estima o valor quando não houve liquidez |
| `preco_referencia` | *Reference Price* — média ponderada das transações da sessão |

**O `rotulo` é a razão de ser deste ficheiro.** O MIBGAS identifica os produtos por
posição relativa — `GMES_M+2` é "o segundo mês a contar de agora", e o que isso
significa muda todos os meses. Sem o rótulo, uma série histórica de `GMES_M+2` mistura
entregas de meses diferentes. A coluna é derivada das datas de entrega, não de uma
tabela, por isso não precisa de manutenção anual.

Os produtos de médio e longo prazo (`GMES`, `GQES`, `GYES`, `GSES`, `GBoMES`) existem
só para **ES** — a liquidez forward está no hub espanhol. Portugal só tem o curto prazo:
`GWDPT` (intradiário), `GDAPT_D+1` a `D+3` (day-ahead) e `GWEPT` (fim de semana).
O horizonte mais distante cotado é o ano **Y+2** (em 2026, o ano de 2028).

A curva de 04/09/2026 em `preco_ultimo`, para dar uma ideia do que se lê aqui. Nos
produtos de entrega mais próxima, onde houve transações, o `preco_referencia` difere;
nos mais distantes não houve liquidez e as duas colunas coincidem — daí ser o
`preco_ultimo` o que dá uma curva completa:

```
Outubro 2026    71,13     4.º Trimestre 2026   71,18     Inverno 2026/27   69,08
Dezembro 2026   71,41     2.º Trimestre 2027   50,07     Inverno 2027/28   45,08
                          2027                 52,43     2028              34,58
```

#### `mibgas_ttf_spread.csv`

Schema:

```
dia,data_iso,produto,entrega_inicio,entrega_fim,spread,ttf_derivado
04/09/2026,2026-09-04,D+1,2026-09-05,2026-09-05,1.29,71.45
```

- `spread` em €/MWh: positivo = gás ibérico mais caro que o TTF europeu.
- `dia` é o dia de **negociação**; só há linhas em dias de sessão (~255/ano).
- **A chave é `(data_iso, produto, entrega_inicio)`, não `(data_iso, produto)`.** Os produtos
  de estação `W` e `S` são cotados para duas estações ao mesmo tempo — o inverno que vem e o
  seguinte — e aparecem **duas vezes na mesma sessão**, distinguidos só pela entrega. Em
  11/09/2026, `W` vale -0,625 para o inverno 2026/27 e -0,586 para o de 2027/28. Filtrar por
  `produto == "W"` devolve duas linhas: use também `entrega_inicio`.
- `ttf_derivado` (só para `D+1`) é uma **estimativa** com incerteza de cerca de 1 €/MWh, não o índice oficial da ICE. Validada contra as médias trimestrais da DG ENER.

### `data/mapas/` — Europa

| Pasta | Conteúdo | Atualização |
|---|---|---|
| `precos_qh/AAAA-MM.json` | Preços day-ahead por zona de mercado europeia (ENTSO-E, 48 zonas), um ficheiro por mês desde 2018-01. Por dia e zona: `avg`, `min`, `max` (+ horas) e `values` quarto-horários em €/MWh | 1×/dia |
| `precos_qh/metadata.json` | `{"ultima_data": "AAAA-MM-DD"}` | 1×/dia |
| `producao/AAAA-MM.json` | Mix de produção diário por país europeu (GWh/dia por tecnologia + consumo), desde 2026-01 | 2×/dia |
| `producao/metadata.json` | `{"ultima_data": "AAAA-MM-DD"}` | 2×/dia |

- O campo `resolution` diz sempre `PT15M`, mesmo no histórico horário: os valores horários são replicados pelos 4 quartos, para uniformidade.
- `values` pode conter `null` em dias parcialmente publicados.

### `data/agregados/` — Séries pré-calculadas

Derivados de `data/omie/` e `data/producao/`, para quem quer médias e totais sem descarregar as séries quarto-horárias completas (~94 MB).

| Ficheiro | Conteúdo | Tamanho |
|---|---|---|
| `omie_anual.csv` | Preços OMIE por ano, 2010–presente | 2 KB |
| `omie_mensal.csv` | Por mês | 22 KB |
| `omie_diario.csv` | Por dia | 712 KB |
| `producao_anual.csv` | Produção, consumo e quota renovável por ano | 2 KB |
| `producao_mensal.csv` | Por mês | 24 KB |
| `producao_diario.csv` | Por dia | 672 KB |

Cada linha traz `dias` (e `quartos`, no OMIE) com quantos entraram no cálculo — é assim que se vê que o mês ou ano corrente ainda está incompleto.

Os agregados OMIE incluem o **preço médio por período do ciclo horário BTN** (`bd_v`, `bd_f`, `bs_v`, `bs_f`, `td_v`, `td_c`, `td_p`, `ts_v`, `ts_c`, `ts_p`), o que permite comparar tarifários bi e tri-horários sem reprocessar as séries.

**Os dias futuros estimados a partir dos futuros OMIP estão excluídos** — estes ficheiros contêm apenas dias com preço real de mercado.

### `data/emissoes/` — Intensidade carbónica

Emissões da produção elétrica nacional, calculadas a 15 minutos desde 2010 a partir de `data/producao/`.

| Ficheiro | Conteúdo |
|---|---|
| `intensidade_diaria.csv` | Por dia, 2010–presente |
| `intensidade_mensal.csv` | Por mês |
| `intensidade_anual.csv` | Por ano |
| `intensidade_perfil_horario.csv` | Média por hora do dia × ano |
| `fatores_emissao.csv` | Os fatores usados e a sua proveniência |

Unidade: **gCO₂eq/kWh**. Fatores: **IPCC AR5**, WG3 Annex III, Tabela A.III.2 (medianas de ciclo de vida).

⚠️ **Âmbito — ler antes de usar:**

- São emissões de **ciclo de vida** (construção, fabrico, operação, desmantelamento), não emissões diretas de combustão. **Não são comparáveis** com o Inventário Nacional da APA nem com o indicador da EEA, que contabilizam só emissões diretas. Os valores aqui são, por construção, mais altos.
- É a intensidade da **produção nacional**, não do consumo. Portugal importa de Espanha — em 2024, 25,5 % do consumo. As colunas `saldo_importador_gwh` e `importacao_perc_consumo` dizem quando a diferença é material.
- No perfil horário, as horas de maior sol **não** são as mais limpas: em ciclo de vida o solar (48) é cerca do dobro da hídrica (24), pelo que ao meio-dia a intensidade sobe face às horas de predomínio hídrico. Em emissões diretas o resultado inverteria-se.

### `data/referencia/` — Tabelas de referência

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `tecnologias.json` | Traduz entre os três vocabulários de tecnologias de geração usados neste repositório | Quando as fontes mudam |

As três fontes nomeiam as mesmas centrais de formas que não coincidem:

| REN (`data/producao/`) | Energy-Charts (`data/mapas/`) | ENTSO-E (`producao-entsoe/`) |
|---|---|---|
| `Hídrica` | `hydro_run_of_river` | `Hydro Run-of-River` |
| `Eólica` | `wind_onshore` | `Wind onshore` |
| `Gás Natural - Ciclo Combinado` | `gas` | `Fossil gas` |
| `Carvão` | `coal_hard` | `Fossil hard coal` |

O `tecnologias.json` tem uma entrada por tecnologia canónica, com os nomes exatos de cada fonte, a categoria (renovável, fóssil, nuclear, armazenamento, consumo, fluxo, agregado), o fator de emissão de ciclo de vida quando o IPCC AR5 o publica, e notas:

```json
"eolica_onshore": {
  "nome": "Eólica terrestre", "categoria": "renovavel",
  "gco2eq_kwh": 11, "fator_fonte": "IPCC AR5 … — Wind onshore",
  "ren": ["Eólica"], "energy_charts": ["wind_onshore"], "entsoe": ["Wind onshore"]
}
```

Duas armadilhas que o ficheiro sinaliza explicitamente:

- **`ren_agregada: true`** — a coluna `Hídrica` da REN soma fio de água, albufeira e turbinagem de bombagem, que as outras fontes separam em três. Não é possível desagregá-la a partir dos dados da REN, e um mapeamento um-para-um daria um resultado errado.
- **`Outra Térmica`** agrega fuelóleo, gasóleo, resíduos e biogás; não corresponde ao `other` das outras fontes, que é residual.

Onde o AR5 não publica valor (linhite, petróleo, resíduos, turfa), `gco2eq_kwh` é `null` — uma lacuna honesta em vez de um número que ninguém consegue citar.

### `data/regulado/` — Dados regulados (ERSE / E-Redes)

| Ficheiro | Conteúdo | Atualização |
|---|---|---|
| `perfis_erse_9.json` | Perfis de consumo BTN publicados pela ERSE | Anual |
| `Perdas_calculadas_2026_TF.csv` | Fatores de perdas (1+perdas) calculados para BT e MT, a partir dos perfis de perdas da E-Redes | Anual |
| `tos_municipios.json` | Taxa de Ocupação do Subsolo (TOS) por município + ORD/CUR de gás natural respetivo | Quando há alterações |

⚠️ `Perdas_calculadas_2026_TF.csv` está fora da convenção do resto do repositório: separador `;`, vírgula decimal, data no formato `1/jan/2026` e três linhas decorativas antes do cabeçalho real.

## Fontes originais

Os dados são recolhidos e processados a partir de fontes oficiais e públicas:

- **[OMIE](https://www.omie.es)** — preços do mercado diário (MIBEL)
- **[OMIP](https://www.omip.pt)** — mercado a prazo / futuros
- **[MIBGAS](https://www.mibgas.es)** — mercado ibérico de gás natural
- **[REN](https://datahub.ren.pt)** — produção, consumo e bombagem em Portugal
- **[ENTSO-E Transparency Platform](https://transparency.entsoe.eu)** — preços day-ahead e produção europeia
- **[Energy-Charts](https://www.energy-charts.info)** (Fraunhofer ISE) — mix de produção europeu
- **[ERSE](https://www.erse.pt)** e **[E-Redes](https://www.e-redes.pt)** — perfis, perdas e tarifas reguladas
- **[IPCC AR5](https://www.ipcc.ch/site/assets/uploads/2018/02/ipcc_wg3_ar5_annex-iii.pdf)** (WG3, Annex III) — fatores de emissão de ciclo de vida

Os dados originais pertencem às respetivas entidades e podem estar sujeitos aos seus próprios termos de utilização. Este repositório disponibiliza **compilações e séries derivadas** desses dados, organizadas para consumo direto.

## Licença e atribuição

As compilações e os datasets derivados deste repositório estão licenciados sob **[CC BY 4.0](LICENSE)** (Creative Commons Atribuição 4.0 Internacional), **com a exceção indicada abaixo**.

### Exceção: `data/simuladores/`

Os ficheiros em `data/simuladores/` — comparativos de tarifários de eletricidade e gás do mercado português — **não estão abrangidos pela licença CC BY 4.0** e são reservados: © Tiago Felícia, todos os direitos reservados.

Ao contrário do resto do repositório, não são recolha automática de uma fonte pública: resultam de pesquisa, verificação e agregação manual, oferta a oferta, a partir das condições publicadas por cada comercializador. Estão acessíveis porque alimentam os simuladores de [tiagofelicia.pt](https://www.tiagofelicia.pt), não como dado aberto.

Para reutilizar estes ficheiros, [contacte o autor](https://www.tiagofelicia.pt/contacto).

Pode copiar, redistribuir e adaptar os dados, inclusive para fins comerciais, desde que dê o devido crédito. Forma de citação sugerida:

> Dados: Tiago Felícia — [www.tiagofelicia.pt](https://www.tiagofelicia.pt) (fontes originais: OMIE, REN, ENTSO-E, MIBGAS), via [dados.tiagofelicia.pt](https://dados.tiagofelicia.pt/)

## Avisos

- Os dados são disponibilizados "tal como estão", sem garantias. Podem existir falhas, atrasos ou correções retroativas nas fontes originais.
- Os ficheiros marcados com ⚙️ são intermédios do pipeline e o seu formato pode mudar sem aviso.
- Os nomes e caminhos dos restantes ficheiros são **estáveis**: alterações que partam URLs serão evitadas e, quando inevitáveis, anunciadas neste README.
- O estado de atualização de cada dataset está em [dados.tiagofelicia.pt/status.html](https://dados.tiagofelicia.pt/status.html).

## Contacto

Sugestões, erros ou dúvidas: [www.tiagofelicia.pt/contacto](https://www.tiagofelicia.pt/contacto)
