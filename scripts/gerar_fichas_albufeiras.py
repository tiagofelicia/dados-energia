#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gerar_fichas_albufeiras.py
A ficha técnica de cada albufeira: as características da albufeira, da barragem e da bacia, do inventário de
albufeiras do SNIRH (Agência Portuguesa do Ambiente), para a secção «Ficha» das páginas /albufeira/<nome> do site.

Fonte: https://snirh.apambiente.pt/index.php?idRef=MTE3Nw==&simbolo_redehidro=<código da estação>, uma página por
albufeira, com separadores (Barragem, Identificação, Dados Técnicos, Órgãos de Descarga, Albufeira, Tipos de
aproveitamento, Bacia Hidrográfica). O SNIRH pede que se refira a origem dos dados.

As albufeiras são as do atualizar_albufeiras_snirh.py (ALBUFEIRAS). As centrais mais recentes (Baixo Sabor,
Feiticeiro, Foz Tua, as três do Tâmega, Ribeiradio, Alto Ceira, Lagoacho, Pedrógão) não estão no inventário: para
essas, a ficha é a da CNPGB (Comissão Nacional Portuguesa das Grandes Barragens, na APA),
https://cnpgb.apambiente.pt/content/<nome da barragem>, em texto corrido e com menos campos (sem bacia própria,
altitudes, bacia em Espanha, freguesia nem entidade exploradora). O ano é o de conclusão da obra (ano_conclusao), não o
de entrada em funcionamento. As cotas da CNPGB estão no sistema altimétrico nacional, como as do InfoÁgua (verificado a
21/11/2026 com o volume e a cota do Baixo Sabor, Foz Tua, Daivões, Ribeiradio e Pedrógão).

ONDE CORRE: num PC em Portugal, à mão, de vez em quando (as fichas quase não mudam): o SNIRH só aceita ligações de
Portugal. Não está em nenhum workflow.

COTAS: nas albufeiras do Alto Rabagão, Alto Cávado, Caniçada, Paradela e Salamonde, o inventário dá as cotas num
sistema local (é preciso subtrair 9,20 m para as ter no sistema altimétrico nacional); ficam como o inventário as dá,
com referencial_local = true. As cotas de tempo real do InfoÁgua usam o mesmo referencial (verificado a 21/11/2026:
a Salamonde a 98% fica a 0,46 m do NPA), por isso a distância ao NPA dá certa sem acerto.

SAÍDA: data/producao/albufeiras_fichas.json — por coluna do albufeiras_snirh_diario.csv:
  codigo, albufeira {capacidade_total_dam3, capacidade_util_dam3, volume_morto_dam3, npa_m, nmc_m, nme_m,
  area_npa_ha, bacia_em_espanha}, barragem {curso_agua, ano, tipo, altura_m, cota_coroamento_m, coroamento_m,
  escada_peixes, descarregador, descarregador_m3s}, local {distrito, concelho, freguesia, dono_obra,
  entidade_exploradora, projetista}, bacia {area_total_km2, area_propria_km2, precipitacao_mm, altitude_min_m,
  altitude_media_m, altitude_max_m}, aproveitamentos [tipos] (e aproveitamentos_notas, se o inventário as tiver),
  referencial_local (só quando true), fonte ("snirh" ou "cnpgb"); nas da CNPGB, barragem.ano_conclusao em vez de
  barragem.ano. Um campo vazio na fonte não aparece.
  bacia_em_espanha é o campo «Existe bacia drenante em Espanha» do inventário, que diz respeito à BACIA PRÓPRIA (a
  parte da bacia entre esta barragem e as de montante): o Alto Lindoso tem «sim» e o Touvedo, logo abaixo, «não».

USO
---
  python gerar_fichas_albufeiras.py
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
from atualizar_albufeiras_snirh import ALBUFEIRAS  # noqa: E402

CAMINHO = os.path.join(ROOT_DIR, "data", "producao", "albufeiras_fichas.json")
URL = "https://snirh.apambiente.pt/index.php?idRef=MTE3Nw==&simbolo_redehidro={}"
PAUSA = 0.5
CNPGB_URL = "https://cnpgb.apambiente.pt/content/{}"
# As centrais que não estão no inventário do SNIRH → o nome da página da barragem na CNPGB
CNPGB = {"baixo_sabor": "baixo-sabor", "alto_tamega": "alto-tâmega", "foz_tua": "foz-tua", "daivoes": "daivões",
         "feiticeiro": "feiticeiro", "gouvaes": "gouvães", "ribeiradio": "ribeiradio", "alto_ceira": "alto-ceira-ii",
         "lagoacho": "lagoacho", "pedrogao": "pedrógão"}
REFERENCIAL_LOCAL = {"alto_rabagao", "alto_cavado", "canicada", "paradela", "salamonde"}
# separador do inventário → (grupo, {campo do inventário: (chave, tipo)})
CAMPOS = {
    "Albufeira": ("albufeira", {
        "Capacidade total (dam3)": ("capacidade_total_dam3", "n"), "Capacidade útil (dam3)": ("capacidade_util_dam3", "n"),
        "Volume morto (dam3)": ("volume_morto_dam3", "n"),
        "Cota do nível de pleno armazenamento - NPA (m)": ("npa_m", "n"),
        "Cota do nível de máxima cheia - NMC (m)": ("nmc_m", "n"),
        "Cota do nível mínimo de exploração - NmE (m)": ("nme_m", "n"),
        "Superfície inundável ao NPA (ha)": ("area_npa_ha", "n"), "Existe bacia drenante em Espanha": ("bacia_em_espanha", "b")}),
    "Barragem": ("barragem", {"Curso de água": ("curso_agua", "t")}),
    "Dados Técnicos": ("barragem", {
        "Ano entrada funcionamento": ("ano", "n"), "Tipo de barragem": ("tipo", "t"),
        "Altura da barragem acima da fundação (m)": ("altura_m", "n"), "Cota do coroamento (m)": ("cota_coroamento_m", "n"),
        "Desenvolvimento do coroamento (m)": ("coroamento_m", "n"), "Existem escadas para peixes": ("escada_peixes", "b")}),
    "Órgãos de Descarga": ("barragem", {
        "Tipo de descarregador": ("descarregador", "t"), "Capacidade do descarregador (m3/s)": ("descarregador_m3s", "n")}),
    "Identificação": ("local", {
        "Distrito": ("distrito", "t"), "Concelho": ("concelho", "t"), "Freguesia": ("freguesia", "t"),
        "Dono da obra": ("dono_obra", "t"), "Entidade exploradora": ("entidade_exploradora", "t"), "Projectista": ("projetista", "t")}),
    "Bacia Hidrográfica": ("bacia", {
        "Área da bacia hidrográfica total (km2)": ("area_total_km2", "n"),
        "Área da bacia hidrográfica própria (km2)": ("area_propria_km2", "n"),
        "Precipitação média anual (mm)": ("precipitacao_mm", "n"), "Altitude mínima (m)": ("altitude_min_m", "n"),
        "Altitude média (m)": ("altitude_media_m", "n"), "Altitude máxima (m)": ("altitude_max_m", "n")}),
}


def limpa(x):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", x)).replace("\xa0", " ")).strip()


def valor(v, tipo):
    if tipo == "n":
        try:
            x = float(v.replace(" ", "").replace(",", "."))
        except ValueError:
            return None
        return int(x) if x.is_integer() else x
    if tipo == "b":
        return {"sim": True, "não": False, "nao": False}.get(v.lower())
    return v


def ficha(t):
    """O texto de uma página do inventário → a ficha (None se a página não tiver os separadores)."""
    tabs = [(m.start(), limpa(m.group(1))) for m in re.finditer(r"<div class='tab'[^>]*>([^<]*)</div>", t)]
    out, aprov = {}, []
    for k, (i, titulo) in enumerate(tabs):
        seg = t[i: tabs[k + 1][0] if k + 1 < len(tabs) else len(t)]
        if titulo == "Tipos de aproveitamento":
            aprov = [limpa(x) for x in re.findall(r"<li>(.*?)</li>", seg, re.S) if limpa(x)]
            continue
        if titulo not in CAMPOS:
            continue
        grupo, campos = CAMPOS[titulo]
        # o <th> pode ter um title com <sup> lá dentro: os atributos leem-se aspas a aspas
        for th, td in re.findall(r'<th(?:[^>"]|"[^"]*")*>(.*?)</th>\s*<td[^>]*>(.*?)</td>', seg, re.S):
            chave = campos.get(limpa(th))
            v = limpa(td)
            if chave and v:
                x = valor(v, chave[1])
                if x is not None:
                    out.setdefault(grupo, {})[chave[0]] = x
    if not out:
        return None
    if aprov:
        out["aproveitamentos"] = [a.split(" - ")[0].strip() for a in aprov]
        notas = [a for a in aprov if " - " in a]
        if notas:
            out["aproveitamentos_notas"] = notas
    return out


# ── CNPGB: uma linha por <div>, «Rótulo – valor», com títulos de secção em maiúsculas ──
# (secção, rótulo em minúsculas (início), grupo, chave, tipo)
REGRAS_CNPGB = [
    ("LOCALIZA", r"distrito$", "local", "distrito", "t"),
    ("LOCALIZA", r"concelho$", "local", "concelho", "t"),
    ("LOCALIZA", r"linha de água$", "barragem", "curso_agua", "t"),
    ("DADOS GERAIS", r"dono de obra", "local", "dono_obra", "t"),
    ("DADOS GERAIS", r"projec?tista$", "local", "projetista", "t"),
    ("DADOS GERAIS", r"ano de conclusão$", "barragem", "ano_conclusao", "n"),
    ("CARACTERÍSTICAS HIDROL", r"área da bacia", "bacia", "area_total_km2", "n"),
    ("CARACTERÍSTICAS HIDROL", r"precipitação", "bacia", "precipitacao_mm", "n"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"área inundada", "albufeira", "area_npa_ha", "ha"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"capacidade( total| \(npa)", "albufeira", "capacidade_total_dam3", "dam3"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"capacidade útil", "albufeira", "capacidade_util_dam3", "dam3"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"volume morto", "albufeira", "volume_morto_dam3", "dam3"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"nível de pleno armazenamento", "albufeira", "npa_m", "n"),
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"nível de máxima cheia", "albufeira", "nmc_m", "n"),
    # «normal (NmEn)» conta; o «extraordinário (NmEE)» não; o Feiticeiro tem «exploraçãol»
    ("CARACTERÍSTICAS DA ALBUFEIRA", r"nível mínimo de exploração(?!.*extraordin)", "albufeira", "nme_m", "n"),
    ("CARACTERÍSTICAS DA BARRAGEM", r"altura acima da fundação", "barragem", "altura_m", "n"),
    ("CARACTERÍSTICAS DA BARRAGEM", r"cota do coroamento", "barragem", "cota_coroamento_m", "n"),
    ("CARACTERÍSTICAS DA BARRAGEM", r"comprimento do coroamento", "barragem", "coroamento_m", "n"),
    ("DESCARREGADOR DE CHEIAS", r"caudal máximo", "barragem", "descarregador_m3s", "n"),
]
# «UTILIZAÇÕES – …» → os tipos de aproveitamento com os nomes do inventário do SNIRH
USOS_CNPGB = [(r"energia|hidroel", "Energia"), (r"rega|irriga", "Irrigação"), (r"abastecimento", "Abastecimento"),
              (r"derivação", "Derivação"), (r"cheias", "Defesa de Cheias"), (r"outr", "Outros")]


def _numero(v):
    """O primeiro número do texto: «1 095 hm3» → 1095, «(138)» → 138, «661.34 (soleira da derivação)» → 661.34."""
    m = re.search(r"\d{1,3}(?: \d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?", v)
    return float(m.group(0).replace(" ", "").replace(",", ".")) if m else None


def _arred(x):
    x = round(x, 3)
    return int(x) if float(x).is_integer() else x


def _valor_cnpgb(v, tipo):
    if tipo == "t":
        return v.strip(" ,;") or None
    x = _numero(v)
    if x is None:
        return None
    u = v.lower().replace(" ", "")
    if tipo == "dam3":      # «1 095 hm3», «131700 x 10³ m³», «1525 x 1000m3»; outra unidade → fica de fora
        return _arred(x * 1000) if "hm" in u else _arred(x) if re.search(r"x10[³3]|x1000", u) else None
    if tipo == "ha":        # «466,1 ha», «237 x 1000m2»
        return _arred(x) if re.search(r"\dha", u) else _arred(x / 10) if re.search(r"x1000m[²2]", u) else None
    return _arred(x)


def _linhas_cnpgb(t):
    i = t.find("field-name-body")
    if i < 0:
        return []
    t = t[t.find(">", i) + 1:]      # o texto começa depois da etiqueta do campo
    j = t.find("Localização no mapa")
    t = t[:j] if j > 0 else t
    t = re.sub(r"<sup>\s*2\s*</sup>", "²", re.sub(r"<sup>\s*3\s*</sup>", "³", t))
    return [x for x in (limpa(p) for p in re.split(r"</div>|</p>|<br\s*/?>", t)) if x]


def ficha_cnpgb(t):
    """O texto de uma página da CNPGB → a ficha, com as chaves do inventário do SNIRH (None sem nenhum campo)."""
    out, seccao, tipo, desc = {}, "", {}, {}
    for linha in _linhas_cnpgb(t):
        m = re.match(r"(.*?)\s*[–-]\s*(.*)$", linha)
        if linha.upper().startswith("UTILIZA"):
            usos = m.group(2).lower() if m else ""
            a = [n for r, n in USOS_CNPGB if re.search(r, usos)]
            if a:
                out["aproveitamentos"] = a
            continue
        if linha.isupper() and not re.search(r"\d|\s[–-]\s", linha):
            seccao = linha
            continue
        if not m or not m.group(2):
            continue
        rot, v = m.group(1).strip(), m.group(2).strip()
        r = rot.lower()
        if seccao.startswith("CARACTERÍSTICAS DA BARRAGEM") and r in ("betão", "aterro", "estrutura", "tipo", "material"):
            tipo.setdefault(r, v)
            continue
        if seccao.startswith("DESCARREGADOR DE CHEIAS") and r in ("tipo de descarregador", "tipo de controlo", "controlo"):
            desc.setdefault("controlo" if "controlo" in r else "tipo", v)
            continue
        for sec, rx, grupo, chave, tp in REGRAS_CNPGB:
            if seccao.startswith(sec) and re.match(rx, r):
                x = _valor_cnpgb(v, tp)
                if x is not None:
                    out.setdefault(grupo, {}).setdefault(chave, x)
                break
    minusc = lambda s: s[:1].lower() + s[1:]  # noqa: E731
    # o tipo de barragem: «Betão – Abóbada de dupla curvatura» → «Betão, abóbada de dupla curvatura»
    if "betão" in tipo or "aterro" in tipo:
        k = "betão" if "betão" in tipo else "aterro"
        x = f"{k.capitalize()}, {minusc(tipo[k])}"
    elif "tipo" in tipo:
        x = f"{tipo['material']}, {minusc(tipo['tipo'])}" if "material" in tipo else tipo["tipo"]
        if x.startswith("Betão ") and "," not in x:    # «Betão gravidade» → «Betão, gravidade»
            x = "Betão, " + x[6:]
    else:
        x = tipo.get("estrutura")
    if x:
        out.setdefault("barragem", {})["tipo"] = x
    # o descarregador: o tipo e as comportas («Lâmina livre, com 4 comportas de segmento»)
    if desc.get("tipo"):
        d = desc["tipo"][:1].upper() + desc["tipo"][1:]
        c = desc.get("controlo", "").rstrip(". ")
        if c and "equipado" not in d.lower():       # «não equipado» já diz que não tem comportas
            d += ", sem comportas" if re.match(r"(sem|não)\b", c.lower()) else f", com {minusc(c)}"
        out.setdefault("barragem", {})["descarregador"] = d
    if not any(k in out for k in ("albufeira", "barragem", "bacia")):
        return None
    return out


def _ler(url, cod):
    req = urllib.request.Request(url, headers={"User-Agent": "dados-energia (tiagofelicia.pt)"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode(cod)


def main():
    fichas, sem, da_cnpgb = {}, [], []
    for col, cod, _site, nome, _bacia, _cap in ALBUFEIRAS:
        try:
            t = _ler(URL.format(cod), "latin-1")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {nome} ({cod}): {e}")
            sem.append(nome)
            continue
        finally:
            time.sleep(PAUSA)
        f, fonte = ficha(t), "snirh"
        if f is None and col in CNPGB:
            try:
                f, fonte = ficha_cnpgb(_ler(CNPGB_URL.format(urllib.parse.quote(CNPGB[col])), "utf-8")), "cnpgb"
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {nome} (CNPGB): {e}")
            finally:
                time.sleep(PAUSA)
        if f is None:
            sem.append(nome)
            continue
        f = {"codigo": cod, **f}
        if col in REFERENCIAL_LOCAL:
            f["referencial_local"] = True
        f["fonte"] = fonte
        if fonte == "cnpgb":
            da_cnpgb.append(nome)
        fichas[col] = f
    print(f"Fichas: {len(fichas)} de {len(ALBUFEIRAS)}; da CNPGB: {', '.join(da_cnpgb) or '—'}; "
          f"sem ficha: {', '.join(sem) or '—'}")
    if not fichas:
        print("ERRO: nenhuma ficha (o SNIRH só aceita ligações de Portugal).")
        sys.exit(1)
    texto = json.dumps(fichas, ensure_ascii=False, indent=1) + "\n"
    antigo = open(CAMINHO, encoding="utf-8").read() if os.path.exists(CAMINHO) else None
    if texto != antigo:
        os.makedirs(os.path.dirname(CAMINHO), exist_ok=True)
        with open(CAMINHO, "w", encoding="utf-8", newline="\n") as f:
            f.write(texto)
        print(f"✓ {os.path.relpath(CAMINHO, ROOT_DIR)}")
    else:
        print("= sem alterações")


if __name__ == "__main__":
    main()
