"""
atualizar_albufeiras_agora.py

O nível «de agora» das albufeiras: os valores de hora a hora das últimas 48 horas que a APA publica no InfoÁgua
(infoagua.apambiente.pt, secção Cheias), em TODAS as estações de albufeira do InfoÁgua (62 a 21/11/2026, 9 delas
em Espanha, junto à fronteira) — o volume armazenado (% da capacidade, como a APA o publica), a cota e os caudais
afluente e efluente. O InfoÁgua só guarda 48 h: recolher todas desde já faz o histórico de todas começar agora.

Ao contrário do SNIRH, o InfoÁgua aceita ligações de fora de Portugal: corre no GitHub, de hora a hora
(.github/workflows/atualizar_albufeiras_agora.yml). A lista das estações vem da página de pesquisa do InfoÁgua
(DATA_Stations; uma estação nova entra sozinha); se essa página falhar, ficam as do atualizar_albufeiras_snirh.py.
Cada albufeira tem por chave a coluna do albufeiras_snirh_diario.csv (pelo código numérico da estação no SNIRH) ou,
fora dele, o nome com a mesma regra das colunas (minúsculas, sem acentos, «_»): «Monte da Rocha» → monte_da_rocha.

Output:
  data/producao/albufeiras_agora.json
      as últimas 48 h de cada albufeira, para as páginas /albufeira/<nome> do site
  data/producao/albufeiras-horario/albufeiras_horario_AAAA.csv
      o histórico de hora a hora (o InfoÁgua só guarda 48 h): uma linha por hora e albufeira, um ficheiro por ano
      (o ano do dia de Lisboa); cada corrida junta as 48 h que vierem às que já lá estavam

As de Espanha vêm sem coordenadas e com o nome sem acentos: ficam com o nome e com o sítio do paredão no
OpenStreetMap (© colaboradores do OpenStreetMap, ODbL), em ESPANHA.

Horas: o InfoÁgua dá a hora sem fuso; tratamo-la como hora de Lisboa (a das estações da APA). No inverno é igual à
UTC. Se alguma hora tratada assim ficar no futuro, ou mais de 3 h atrás em todas as estações, o log avisa: é sinal
de que a APA publica em UTC.

Uso:
  python atualizar_albufeiras_agora.py
"""
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
from atualizar_albufeiras_snirh import ALBUFEIRAS  # noqa: E402  (coluna, código, estação, nome, bacia, referência)

PASTA = os.path.join(ROOT_DIR, "data", "producao")
CAMINHO_JSON = os.path.join(PASTA, "albufeiras_agora.json")
PASTA_HORARIO = os.path.join(PASTA, "albufeiras-horario")
URL = "https://infoagua.apambiente.pt/pt/cheias/cheia-detalhe/{}"
URL_LISTA = "https://infoagua.apambiente.pt/pt/cheias/cheias-pesquisa"
UA = "Mozilla/5.0 (compatible; dados-energia; +https://dados.tiagofelicia.pt)"
PAUSA = 1.0          # segundos entre pedidos
TENTATIVAS = 3
LISBOA = ZoneInfo("Europe/Lisbon")
# Os parâmetros da estação de albufeira no InfoÁgua (id → coluna)
PARAMETROS = {7: "volume_pct", 3: "cota_m", 6: "caudal_afluente_m3s", 2: "caudal_efluente_m3s"}
VALORES = list(PARAMETROS.values())
COLUNAS_CSV = ["data", "albufeira"] + VALORES + ["data_utc"]
# As albufeiras de Espanha do InfoÁgua (chave → nome, latitude e longitude do paredão no OpenStreetMap)
ESPANHA = {
    "alcantara": ("Alcántara", 39.72944, -6.88534), "cedillo": ("Cedillo", 39.66386, -7.54000),
    "saucelle": ("Saucelle", 41.04723, -6.80440), "aldeadavila": ("Aldeadávila", 41.21163, -6.68619),
    "almendra": ("Almendra", 41.26595, -6.32303), "castro": ("Castro", 41.57533, -6.18741),
    "ricobayo": ("Ricobayo", 41.52649, -5.98472), "las_conchas": ("Las Conchas", 41.94402, -8.03493),
    "las_salas": ("Las Salas", 41.92462, -7.94444),
}


def pedir(url):
    """O HTML da página e o endereço final (a página de uma estação que não existe redireciona para a inicial)."""
    erro = None
    for t in range(TENTATIVAS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace"), r.geturl()
        except Exception as e:  # noqa: BLE001
            erro = e
            time.sleep(3 * (t + 1))
    raise erro


def json_da_pagina(html, nome):
    """O valor de «var <nome> = …» de um <script> da página (JSON)."""
    i = html.find("var " + nome + " =")
    if i < 0:
        return None
    i = html.index("=", i) + 1
    return json.JSONDecoder().raw_decode(html[i:].lstrip())[0]


def utc_de(momento):
    """«AAAA-MM-DD HH:MM:SS» (hora de Lisboa) → «AAAA-MM-DD HH:MM» em UTC. Na hora repetida de outubro conta a 1.ª."""
    local = datetime.strptime(momento[:16], "%Y-%m-%d %H:%M").replace(tzinfo=LISBOA)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def recolher(site):
    """{data_utc: {coluna: valor}} das últimas 48 h, ou None se a estação não existe no InfoÁgua."""
    html, final = pedir(URL.format(site))
    if "cheia-detalhe" not in final:
        return None
    horas = {}
    for p in json_da_pagina(html, "DATA_StationParameters") or []:
        col = PARAMETROS.get(p.get("id"))
        if not col:
            continue
        for v in p.get("values") or []:
            if v.get("value") is None or not v.get("moment"):
                continue
            horas.setdefault(utc_de(v["moment"]), {})[col] = float(v["value"])
    return horas


def chave_de(nome):
    """A regra das colunas do albufeiras_snirh_diario.csv: «Monte da Rocha» → monte_da_rocha, «Açude Ponte Coimbra» →
    acude_ponte_coimbra."""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def estacoes():
    """[(chave, site, ficha)] das estações de albufeira do InfoÁgua; sem a página de pesquisa, as do SNIRH."""
    por_site = {site: (col, nome, bacia) for col, _cod, site, nome, bacia, _ref in ALBUFEIRAS}
    try:
        html, _ = pedir(URL_LISTA)
        lista = [e for e in json_da_pagina(html, "DATA_Stations") or [] if e.get("station_type_id") == "estacao_albufeira"]
    except Exception as e:  # noqa: BLE001
        print(f"⚠ Sem a lista do InfoÁgua ({type(e).__name__}: {e}): ficam as albufeiras do SNIRH")
        lista = []
    if not lista:
        return [(col, site, dict(nome=nome, bacia=bacia, pais="PT", snirh=site)) for site, (col, nome, bacia) in por_site.items()]
    out, vistas = [], set()
    for e in lista:
        site = e.get("snirh_source_id")
        if not site or site in vistas:
            continue
        vistas.add(site)
        nossa = por_site.get(site)
        ficha = dict(nome=nossa[1] if nossa else e.get("station_name"), bacia=nossa[2] if nossa else e.get("basin_name"),
                     rio=e.get("river") or None, snirh=site,
                     # as de Espanha não têm concelho português (dico) nem coordenadas no InfoÁgua
                     pais="PT" if e.get("dico") else "ES")
        if e.get("latitude") and e.get("longitude"):
            ficha["lat"], ficha["lon"] = round(float(e["latitude"]), 5), round(float(e["longitude"]), 5)
        chave = nossa[0] if nossa else chave_de(e.get("station_name") or str(site))
        if ficha["pais"] == "ES" and chave in ESPANHA:
            ficha["nome"], la, lo = ESPANHA[chave]
            ficha.setdefault("lat", la)
            ficha.setdefault("lon", lo)
        out.append((chave, site, ficha))
    return out


def ler_json():
    try:
        with open(CAMINHO_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def lisboa(data_utc):
    return datetime.strptime(data_utc, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(LISBOA)


def num(v):
    """Como a APA o publica (o repr mais curto do número), sem «.0» nos inteiros."""
    if v is None:
        return ""
    s = repr(v)
    return s[:-2] if s.endswith(".0") else s


def gravar(caminho, texto):
    """Só escreve se mudou (o commit do workflow fica vazio quando a APA não publicou nada de novo)."""
    try:
        with open(caminho, encoding="utf-8", newline="") as f:
            if f.read() == texto:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8", newline="") as f:
        f.write(texto)
    return True


def juntar_horario(recolhidas):
    """Junta as horas recolhidas ao CSV do ano (chave: data_utc + albufeira; o valor novo substitui o antigo)."""
    por_ano = {}
    for col, horas in recolhidas.items():
        for h, vals in horas.items():
            por_ano.setdefault(lisboa(h).year, []).append((h, col, vals))
    mudados = []
    for ano, linhas in sorted(por_ano.items()):
        caminho = os.path.join(PASTA_HORARIO, f"albufeiras_horario_{ano}.csv")
        tabela = {}
        if os.path.exists(caminho):
            with open(caminho, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    tabela[(r["data_utc"], r["albufeira"])] = {c: (float(r[c]) if r.get(c) not in (None, "") else None) for c in VALORES}
        for h, col, vals in linhas:
            antigo = tabela.get((h, col), {})
            tabela[(h, col)] = {c: vals.get(c, antigo.get(c)) for c in VALORES}
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(COLUNAS_CSV)
        for (h, col) in sorted(tabela):
            v = tabela[(h, col)]
            w.writerow([lisboa(h).strftime("%Y-%m-%d %H:%M"), col] + [num(v[c]) for c in VALORES] + [h])
        if gravar(caminho, out.getvalue()):
            mudados.append(os.path.basename(caminho))
    return mudados


def main():
    agora = datetime.now(timezone.utc)
    antes = ler_json()
    anterior, fichas_antes = antes.get("albufeiras", {}), antes.get("estacoes", {})
    lista = estacoes()
    recolhidas, sem_estacao, falhas, fichas = {}, [], [], {}
    for col, site, ficha in lista:
        try:
            horas = recolher(site)
        except Exception as e:  # noqa: BLE001
            falhas.append(col)
            print(f"  ✗ {ficha['nome']}: {type(e).__name__}: {e}")
            continue
        finally:
            time.sleep(PAUSA)
        if horas is None:
            sem_estacao.append(ficha["nome"])
        elif horas:
            recolhidas[col] = horas
            fichas[col] = ficha
    print(f"InfoÁgua: {len(lista)} estações de albufeira; {len(recolhidas)} com valores, {len(sem_estacao)} sem página, "
          f"{len(falhas)} com erro")
    if not recolhidas:
        print("✗ Nenhuma albufeira com valores: o ficheiro fica como estava.")
        sys.exit(1)

    # A hora sem fuso: uma hora de Lisboa não pode estar no futuro, e a mais recente de todas não costuma ter mais de
    # 3 h; se não for assim, a APA está a publicar noutra hora
    ultimas = [max(h) for h in recolhidas.values()]
    mais_recente = max(ultimas)
    futuro = [u for u in ultimas if datetime.strptime(u, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) > agora + timedelta(minutes=10)]
    atraso_h = (agora - datetime.strptime(mais_recente, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)).total_seconds() / 3600
    if futuro:
        print(f"⚠ {len(futuro)} estações com horas no futuro se lidas como hora de Lisboa (ex.: {futuro[0]} UTC): ver a hora da APA")
    elif atraso_h > 3:
        print(f"⚠ O valor mais recente de todas as estações tem {atraso_h:.1f} h: ver se a APA atrasou ou se a hora não é a de Lisboa")

    # O JSON: as horas recolhidas agora; uma albufeira que falhou nesta corrida fica com as que tinha
    albufeiras = {}
    for col, _site, _ficha in lista:
        if col in recolhidas:
            horas = recolhidas[col]
            albufeiras[col] = [[h] + [horas[h].get(c) for c in VALORES] for h in sorted(horas)]
        elif col in anterior and col in falhas:
            albufeiras[col] = anterior[col]
            fichas[col] = fichas_antes.get(col) or _ficha
    doc = {
        "fonte": "APA — InfoÁgua (estações de albufeira de tempo real), https://infoagua.apambiente.pt",
        "descricao": "As últimas 48 horas, de hora a hora: volume armazenado (% da capacidade, como a APA o publica), "
                     "cota (m) e caudais afluente e efluente (m3/s). Horas em UTC (início da hora). Em estacoes, a "
                     "ficha de cada albufeira (nome, bacia, rio, código no SNIRH, país; coordenadas da estação, "
                     "quando o InfoÁgua as dá).",
        "ultima_data": lisboa(mais_recente).strftime("%Y-%m-%d"),
        "ultima_hora_utc": mais_recente,
        "colunas": ["data_utc"] + VALORES,
        "estacoes": {c: fichas[c] for c in albufeiras},
        "albufeiras": albufeiras,
    }
    texto = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    if gravar(CAMINHO_JSON, texto):
        print(f"✓ albufeiras_agora.json: {len(albufeiras)} albufeiras, última hora {mais_recente} UTC")
    else:
        print("= albufeiras_agora.json sem mudanças")
    mudados = juntar_horario(recolhidas)
    print("✓ horário: " + (", ".join(mudados) if mudados else "sem mudanças"))


if __name__ == "__main__":
    main()
