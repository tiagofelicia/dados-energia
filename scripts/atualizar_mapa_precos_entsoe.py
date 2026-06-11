"""
Recolha de precos day-ahead via API ENTSO-E Transparency Platform.
Preferencial PT15M (quarto-horario); fallback PT60M (horario) ou PT30M.

Token: deve estar em variavel de ambiente ENTSOE_TOKEN
       (no GitHub Actions, configurar como Secret).

Modo diario (default):  python atualizar_mapa_precos_entsoe.py
Modo backfill:          python atualizar_mapa_precos_entsoe.py --backfill

Gera ficheiros em data/mapa_precos_qh/ (formato compativel com o anterior,
mas com campos extra: 'resolution' e 'values' [lista de precos]).

Formato:
{
  "2026-04-25": {
    "PT": {
      "avg": 45.30,
      "min": 12.10, "min_hour": "04:15",
      "max": 89.50, "max_hour": "19:00",
      "resolution": "PT15M",
      "values": [25.1, 25.1, 24.8, ...]  // 96 ou 24 valores
    }
  }
}
"""

import os
import sys
import time
import json
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

import requests


API_URL = "https://web-api.tp.entsoe.eu/api"
# Caminho ancorado no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data", "mapas", "precos_qh")
BACKFILL_START = "2026-01-01"
BACKFILL_END = None   # None = ate ontem; ou "2026-12-31"

# ----- ZONAS -----------------------------------------------------------------
# Mapeamento: codigo curto (compatibilidade com frontend) -> EIC ENTSO-E
ZONE_EIC = {
    "AT": "10YAT-APG------L",
    "BE": "10YBE----------2",
    "BG": "10YCA-BULGARIA-R",
    "CH": "10YCH-SWISSGRIDZ",
    "CZ": "10YCZ-CEPS-----N",
    "DE-LU": "10Y1001A1001A82H",
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "EE": "10Y1001A1001A39I",
    "ES": "10YES-REE------0",
    "FI": "10YFI-1--------U",
    "FR": "10YFR-RTE------C",
    "GR": "10YGR-HTSO-----Y",
    "HR": "10YHR-HEP------M",
    "HU": "10YHU-MAVIR----U",
    "IE(SEM)": "10Y1001A1001A59C",
    "IT-North": "10Y1001A1001A73I",
    "IT-Centre-North": "10Y1001A1001A70O",
    "IT-Centre-South": "10Y1001A1001A71M",
    "IT-South": "10Y1001A1001A788",
    "IT-Sicily": "10Y1001A1001A75E",
    "IT-Sardinia": "10Y1001A1001A74G",
    "IT-Calabria": "10Y1001C--00096J",
    "IT-SACOAC": "10Y1001A1001A885",
    "IT-SACODC": "10Y1001A1001A893",
    "LT": "10YLT-1001A0008Q",
    "LV": "10YLV-1001A00074",
    "AL": "10YAL-KESH-----5",
    "ME": "10YCS-CG-TSO---S",
    "MK": "10YMK-MEPSO----8",
    "XK": "10Y1001C--00100H",
    "NL": "10YNL----------L",
    "NO1": "10YNO-1--------2",
    "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J",
    "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "PL": "10YPL-AREA-----S",
    "PT": "10YPT-REN------W",
    "RO": "10YRO-TEL------P",
    "RS": "10YCS-SERBIATSOV",
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
    "SI": "10YSI-ELES-----O",
    "SK": "10YSK-SEPS-----K",
}

ZONE_TIMEZONES = {
    "AT": "Europe/Vienna", "BE": "Europe/Brussels", "CH": "Europe/Zurich",
    "CZ": "Europe/Prague", "DE-LU": "Europe/Berlin",
    "DK1": "Europe/Copenhagen", "DK2": "Europe/Copenhagen",
    "ES": "Europe/Madrid", "FR": "Europe/Paris",
    "HR": "Europe/Zagreb", "HU": "Europe/Budapest",
    "IT-North": "Europe/Rome", "IT-Centre-North": "Europe/Rome",
    "IT-Centre-South": "Europe/Rome", "IT-South": "Europe/Rome",
    "IT-Sicily": "Europe/Rome", "IT-Sardinia": "Europe/Rome",
    "IT-Calabria": "Europe/Rome", "IT-SACOAC": "Europe/Rome", "IT-SACODC": "Europe/Rome",
    "AL": "Europe/Tirane",
    "ME": "Europe/Podgorica", "MK": "Europe/Skopje", "XK": "Europe/Belgrade",
    "NL": "Europe/Amsterdam",
    "NO1": "Europe/Oslo", "NO2": "Europe/Oslo", "NO2NSL": "Europe/Oslo",
    "NO3": "Europe/Oslo", "NO4": "Europe/Oslo", "NO5": "Europe/Oslo",
    "PL": "Europe/Warsaw", "RS": "Europe/Belgrade",
    "SE1": "Europe/Stockholm", "SE2": "Europe/Stockholm",
    "SE3": "Europe/Stockholm", "SE4": "Europe/Stockholm",
    "SI": "Europe/Ljubljana", "SK": "Europe/Bratislava",
    "PT": "Europe/Lisbon", "IE(SEM)": "Europe/Dublin",
    "BG": "Europe/Sofia", "EE": "Europe/Tallinn",
    "FI": "Europe/Helsinki", "GR": "Europe/Athens",
    "LT": "Europe/Vilnius", "LV": "Europe/Riga",
    "RO": "Europe/Bucharest",
}

# Resolucao em segundos
RESOLUTION_SECONDS = {
    "PT15M": 15 * 60,
    "PT30M": 30 * 60,
    "PT60M": 60 * 60,
    "PT1H": 60 * 60,
}


# ----- ENTSO-E API -----------------------------------------------------------

VERBOSE = False


def fetch_entsoe_xml(token, eic, start_utc, end_utc):
    """Consulta day-ahead prices (A44) para um EIC e intervalo UTC."""
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": start_utc.strftime("%Y%m%d%H%M"),
        "periodEnd":   end_utc.strftime("%Y%m%d%H%M"),
    }
    resp = requests.get(API_URL, params=params, timeout=60)
    if VERBOSE:
        # Esconder o token na URL impressa
        printed_url = resp.url
        if token:
            printed_url = printed_url.replace(token, "***TOKEN***")
        print(f"  [HTTP {resp.status_code}] {printed_url}")
    if resp.status_code == 200:
        if VERBOSE:
            print(f"  [body] {len(resp.text)} bytes; head: {resp.text[:160].strip()!r}")
        return resp.text
    if resp.status_code == 400 and "No matching data found" in resp.text:
        if VERBOSE:
            print(f"  [400] No matching data found")
        return None
    if VERBOSE:
        print(f"  [body] {resp.text[:300]!r}")
    resp.raise_for_status()
    return None


def _local(tag):
    """Devolve o tag sem namespace."""
    return tag.split("}")[-1] if "}" in tag else tag


def parse_entsoe_xml(xml_text):
    """Parse XML A44.

    Devolve lista de tuplos (utc_dt, price, resolution_str) ja com
    forward-fill aplicado para posicoes ausentes (sparse points).
    Pode haver multiplos TimeSeries no mesmo response (ex: mudanca de hora).
    """
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)

    # ENTSO-E pode devolver varias TimeSeries no mesmo response. Filtros:
    #  1) contract_MarketAgreement.type = "A01" (day-ahead puro). Outros tipos
    #     (A07 = intradiario, etc.) sao descartados se houver pelo menos uma
    #     TS A01 — exemplo: ES devolve TS A01 (canonica) e TS A07.
    #  2) classificationSequence_AttributeInstanceComponent.position = "1"
    #     quando existir (DE-LU/AT publicam Sequence 1 + Sequence 2 com precos
    #     diferentes; queremos a 1).
    #  3) Se nenhum dos atributos discriminar, usamos todas as TS (caso comum).
    parsed_ts = []  # lista de dicts {seq, contract, points}
    has_any_a01 = False

    for ts in root.iter():
        if _local(ts.tag) != "TimeSeries":
            continue
        seq = None
        contract = None
        for direct_child in list(ts):
            tag = _local(direct_child.tag)
            if tag == "classificationSequence_AttributeInstanceComponent.position":
                seq = (direct_child.text or "").strip()
            elif tag == "contract_MarketAgreement.type":
                contract = (direct_child.text or "").strip()
                if contract == "A01":
                    has_any_a01 = True

        ts_points = []
        for period in ts.iter():
            if _local(period.tag) != "Period":
                continue
            time_int = None
            time_int_end = None
            resolution = None
            points = []
            for child in period:
                tag = _local(child.tag)
                if tag == "timeInterval":
                    for sub in child:
                        if _local(sub.tag) == "start":
                            time_int = sub.text
                        elif _local(sub.tag) == "end":
                            time_int_end = sub.text
                elif tag == "resolution":
                    resolution = child.text
                elif tag == "Point":
                    pos = None
                    price = None
                    for sub in child:
                        st = _local(sub.tag)
                        if st == "position":
                            pos = int(sub.text)
                        elif st == "price.amount":
                            price = float(sub.text)
                    if pos is not None and price is not None:
                        points.append((pos, price))

            if not (time_int and resolution and points):
                continue

            ts_str = time_int.replace("Z", "+00:00")
            start_dt = datetime.fromisoformat(ts_str)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            else:
                start_dt = start_dt.astimezone(timezone.utc)

            res_sec = RESOLUTION_SECONDS.get(resolution)
            if not res_sec:
                # Fallback: tentar parse generico ISO 8601 (PT15M, PT60M, PT1H, P1H, ...)
                m = resolution.strip().upper() if resolution else ""
                if m.startswith("PT") or m.startswith("P"):
                    body = m[2:] if m.startswith("PT") else m[1:]
                    try:
                        if body.endswith("M"):
                            res_sec = int(body[:-1]) * 60
                        elif body.endswith("H"):
                            res_sec = int(body[:-1]) * 3600
                    except ValueError:
                        res_sec = None
                if not res_sec:
                    continue

            # Forward-fill sparse encoding. Sparse encoding ENTSO-E:
            # - Posicoes ausentes propagam o ultimo valor conhecido
            # - Se a ultima posicao publicada for menor que o total esperado
            #   (calculado a partir de timeInterval.end - start / resolution),
            #   o ultimo valor estende-se ate ao fim do periodo. Era esta a
            #   parte em falta — slot 96 ficava None quando 95==96 e 96 era
            #   omitido.
            points.sort(key=lambda x: x[0])
            max_pos = points[-1][0]
            expected_pos = max_pos
            if time_int_end:
                try:
                    end_str = time_int_end.replace("Z", "+00:00")
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    else:
                        end_dt = end_dt.astimezone(timezone.utc)
                    duration_sec = (end_dt - start_dt).total_seconds()
                    if duration_sec > 0:
                        expected_pos = int(duration_sec // res_sec)
                except (ValueError, TypeError):
                    pass
            fill_to = max(max_pos, expected_pos)

            filled = {}
            last_price = None
            pos_to_price = dict(points)
            for p in range(1, fill_to + 1):
                if p in pos_to_price:
                    last_price = pos_to_price[p]
                if last_price is not None:
                    filled[p] = last_price

            # Normalizar tudo para PT15M: se a resolucao for >15min,
            # expandir cada ponto em N quartos identicos (PT60M -> 4, PT30M -> 2).
            # Garante uniformidade (sempre 96 valores por dia local).
            quarters_per_point = max(1, res_sec // (15 * 60))
            for p, price in filled.items():
                pt_utc = start_dt + timedelta(seconds=res_sec * (p - 1))
                if quarters_per_point > 1:
                    for q in range(quarters_per_point):
                        ts_points.append((pt_utc + timedelta(minutes=15 * q), price, "PT15M"))
                else:
                    ts_points.append((pt_utc, price, "PT15M" if res_sec == 900 else resolution))

        if ts_points:
            parsed_ts.append({"seq": seq, "contract": contract, "points": ts_points})
        elif VERBOSE:
            print(f"  [TS] seq={seq!r} contract={contract!r} -> 0 pontos (descartada)")

    if VERBOSE:
        print(f"  [parse] {len(parsed_ts)} TS com pontos; has_any_a01={has_any_a01}")
        for idx, t in enumerate(parsed_ts):
            print(f"    TS#{idx}: seq={t['seq']!r} contract={t['contract']!r} pontos={len(t['points'])}")

    if not parsed_ts:
        return []

    # 1) Se houver alguma A01, manter apenas A01 (descarta A07 etc.).
    kept = parsed_ts
    if has_any_a01:
        kept = [t for t in kept if t["contract"] == "A01"]
        if VERBOSE:
            print(f"  [parse] apos filtro A01: {len(kept)} TS")

    # 2) Descartar TS com Sequence != 1. Mantemos:
    #    - TS sem atributo seq (canonicas para a maioria das zonas)
    #    - TS com seq == "1" (canonica em DE-LU/AT)
    #    Descartamos apenas seq="2", "3", ... (cenarios alternativos).
    #    DK2 publica seq=None + seq="2" lado a lado; queremos as None.
    before = len(kept)
    kept = [t for t in kept if (not t["seq"]) or t["seq"] == "1"]
    if VERBOSE and len(kept) != before:
        print(f"  [parse] apos descartar Sequence != 1: {len(kept)} TS (antes {before})")

    flat = []
    for t in kept:
        flat.extend(t["points"])
    if VERBOSE:
        print(f"  [parse] {len(flat)} pontos finais")
    return flat


# ----- Agregacao -------------------------------------------------------------

def group_by_local_day(points, tz_name):
    """Agrupa pontos por dia local. Devolve dict {YYYY-MM-DD: list[(local_dt, price, resolution)]}.

    Se o mesmo (dia, hora_local) aparecer com varias resolucoes, prefere PT15M > PT30M > PT60M.
    """
    tz = ZoneInfo(tz_name)
    # bucket: (day, local_dt) -> (price, res_priority)
    res_priority = {"PT15M": 3, "PT30M": 2, "PT60M": 1, "PT1H": 1}
    bucket = {}
    for utc_dt, price, res in points:
        local_dt = utc_dt.astimezone(tz)
        day = local_dt.strftime("%Y-%m-%d")
        key = (day, local_dt)
        prio = res_priority.get(res, 0)
        existing = bucket.get(key)
        if existing is None or prio > existing[1]:
            bucket[key] = (price, prio, res)

    daily = {}
    for (day, local_dt), (price, prio, res) in bucket.items():
        daily.setdefault(day, []).append((local_dt, price, res))

    for day in daily:
        daily[day].sort(key=lambda x: x[0])
    return daily


def compute_day_stats(day_points):
    """Recebe lista [(local_dt, price, res)] de UM dia local, ordenada.

    Devolve dict com avg/min/max/min_hour/max_hour/resolution/values,
    onde values cobre 24h locais (pode ser 96 para PT15M ou 24 para PT60M).
    """
    if not day_points:
        return None

    # Determinar resolucao dominante (a maior granularidade encontrada)
    res_priority = {"PT15M": 3, "PT30M": 2, "PT60M": 1, "PT1H": 1}
    best_res = max((p[2] for p in day_points), key=lambda r: res_priority.get(r, 0))
    res_sec = RESOLUTION_SECONDS[best_res]
    expected_slots = 24 * 3600 // res_sec  # 96, 48 ou 24

    # Reduzir a lista a um valor por slot (caso haja sobreposicao).
    # Se houver pontos com resolucao mais grossa misturados (ex: HH:00 com PT60M
    # e HH:00 PT15M), o passo anterior ja preferiu o mais fino.
    # Aqui assumimos slots alinhados ao inicio do dia local.
    day_start = day_points[0][0].replace(hour=0, minute=0, second=0, microsecond=0)

    slots = [None] * expected_slots
    for local_dt, price, _res in day_points:
        delta = (local_dt - day_start).total_seconds()
        idx = int(delta // res_sec)
        if 0 <= idx < expected_slots:
            slots[idx] = price

    # Forward-fill APENAS entre slots conhecidos (gaps internos do sparse
    # encoding do ENTSO-E). NAO estender para alem do ultimo slot publicado:
    # ex. para PT no ultimo dia, os ultimos 4 quartos (23:00-23:45) so sao
    # publicados no dia seguinte porque o leilao e em CET.
    last_known_idx = -1
    for i in range(expected_slots - 1, -1, -1):
        if slots[i] is not None:
            last_known_idx = i
            break

    if last_known_idx < 0:
        return None

    last = None
    for i in range(last_known_idx + 1):
        if slots[i] is None and last is not None:
            slots[i] = last
        elif slots[i] is not None:
            last = slots[i]

    # Heuristica para zonas horarias (RS, IT-Calabria, etc.) quando ENTSO-E
    # publica o ultimo intervalo como TimeSeries PT15M de 1 ponto: se o
    # ultimo slot conhecido for inicio de hora (idx multiplo de 4) e a hora
    # anterior tiver os 4 quartos iguais (assinatura de dados horarios
    # expandidos), replicar o ultimo valor nos 3 quartos seguintes.
    if (
        res_sec == 900
        and last_known_idx >= 4
        and last_known_idx % 4 == 0
        and last_known_idx + 3 < expected_slots
    ):
        prev_hour = slots[last_known_idx - 4: last_known_idx]
        if (
            prev_hour[0] is not None
            and all(v == prev_hour[0] for v in prev_hour)
        ):
            v = slots[last_known_idx]
            for j in range(1, 4):
                slots[last_known_idx + j] = v
            last_known_idx += 3

    # Os slots para alem de last_known_idx ficam None (representam horas
    # ainda nao publicadas).
    values_known = [v for v in slots if v is not None]
    if not values_known:
        return None

    avg = sum(values_known) / len(values_known)
    # min/max so consideram slots conhecidos (sentinel ignora None)
    min_idx = min(range(expected_slots), key=lambda i: slots[i] if slots[i] is not None else float("inf"))
    max_idx = max(range(expected_slots), key=lambda i: slots[i] if slots[i] is not None else float("-inf"))

    def slot_label(idx):
        total_min = idx * (res_sec // 60)
        return f"{total_min // 60:02d}:{total_min % 60:02d}"

    return {
        "avg": round(avg, 2),
        "min": round(slots[min_idx], 2),
        "min_hour": slot_label(min_idx),
        "max": round(slots[max_idx], 2),
        "max_hour": slot_label(max_idx),
        "resolution": best_res,
        "values": [round(v, 2) if v is not None else None for v in slots],
    }


# ----- Persistencia ----------------------------------------------------------

def load_monthly_file(year_month):
    filepath = os.path.join(DATA_DIR, f"{year_month}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_monthly_file(year_month, data):
    filepath = os.path.join(DATA_DIR, f"{year_month}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def save_metadata(latest_date):
    filepath = os.path.join(DATA_DIR, "metadata.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"ultima_data": latest_date}, f, ensure_ascii=False)


# ----- Datas -----------------------------------------------------------------

def local_day_to_utc_range(day, tz_name):
    """Converte um dia local [00:00, 24:00) para intervalo UTC."""
    tz = ZoneInfo(tz_name)
    local_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def get_month_ranges(start_date, end_date):
    ranges = []
    current = start_date.replace(day=1)
    while current <= end_date:
        month_start = max(current, start_date)
        if current.month == 12:
            month_end_limit = current.replace(year=current.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end_limit = current.replace(month=current.month + 1, day=1) - timedelta(days=1)
        month_end = min(month_end_limit, end_date)
        ranges.append((month_start, month_end))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return ranges


# ----- Main ------------------------------------------------------------------

def fetch_zone_range(token, zone, start_date, end_date):
    """Busca todos os pontos para uma zona/intervalo. Devolve dict {dia_local: stats}."""
    tz = ZONE_TIMEZONES.get(zone, "Europe/Berlin")
    eic = ZONE_EIC[zone]

    # Pedir UTC range que cobre [start_date 00:00 local, end_date 24:00 local]
    utc_start, _ = local_day_to_utc_range(start_date, tz)
    _, utc_end = local_day_to_utc_range(end_date, tz)

    xml = fetch_entsoe_xml(token, eic, utc_start, utc_end)
    if not xml:
        return {}
    points = parse_entsoe_xml(xml)
    if not points:
        return {}

    daily = group_by_local_day(points, tz)
    # Filtrar apenas dias dentro do intervalo pedido. Zonas a leste de CET
    # (UTC+2/+3 — BG, EE, FI, GR, etc.) recebem dias parciais nos extremos
    # porque os periodos ENTSO-E sao em CET e "espalham-se" por 2 dias locais.
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()
    result = {}
    for day_str, day_points in daily.items():
        if day_str < start_str or day_str > end_str:
            continue
        stats = compute_day_stats(day_points)
        if stats:
            result[day_str] = stats
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--zones", nargs="+", help="Subconjunto de zonas (debug)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Imprime URLs, status HTTP e excertos das respostas")
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = bool(args.verbose)

    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        print("ERRO: variavel de ambiente ENTSOE_TOKEN nao definida.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)
    overmorrow = today + timedelta(days=2)

    if args.backfill:
        start_date = datetime.strptime(BACKFILL_START, "%Y-%m-%d").date()
        if BACKFILL_END:
            end_date = datetime.strptime(BACKFILL_END, "%Y-%m-%d").date()
        else:
            end_date = yesterday
        print(f"Modo BACKFILL: {start_date} -> {end_date}")
    else:
        # Recolhemos ate +2 dias (em vez de +1) para garantir que zonas EET
        # (BG, EE, FI, GR, LT, LV, RO; UTC+2/+3) tem a totalidade da CET-day
        # de amanha disponivel — os ultimos 4 slots CET 23:00-23:45 caem no
        # dia LOCAL seguinte para estas zonas, que so e capturado se pedirmos
        # tambem "depois-de-amanha".
        # Custo: ~45 pedidos extra/dia. Para zonas CET/oeste, depois-de-amanha
        # devolve "No matching data found" e o script salta silenciosamente.
        start_date = yesterday
        end_date = overmorrow
        print(f"Modo DIARIO: {start_date} -> {end_date} (ontem + hoje + amanha + depois)")

    zones = args.zones if args.zones else list(ZONE_EIC.keys())
    monthly_cache = {}
    latest_date = None
    total = len(zones)

    for i, zone in enumerate(zones, 1):
        print(f"[{i}/{total}] {zone}...", end=" ", flush=True)
        try:
            # Limite ate ao qual um dia conta para 'ultima_data' do metadata.
            # Em diario, 'depois-de-amanha' so e recolhido para preencher slots
            # iniciais de zonas EET na vista CET — nao representa um dia
            # completo, nao deve ser apresentado como "ultimo dia disponivel".
            metadata_limit = tomorrow.isoformat() if not args.backfill else None

            if args.backfill:
                # Buscar mes a mes
                month_ranges = get_month_ranges(start_date, end_date)
                zone_days = 0
                for m_start, m_end in month_ranges:
                    daily = fetch_zone_range(token, zone, m_start, m_end)
                    for day_str, stats in daily.items():
                        ym = day_str[:7]
                        if ym not in monthly_cache:
                            monthly_cache[ym] = load_monthly_file(ym)
                        monthly_cache[ym].setdefault(day_str, {})[zone] = stats
                        zone_days += 1
                        if (latest_date is None or day_str > latest_date) and (metadata_limit is None or day_str <= metadata_limit):
                            latest_date = day_str
                    time.sleep(0.5)
                print(f"{zone_days} dias")
            else:
                daily = fetch_zone_range(token, zone, start_date, end_date)
                for day_str, stats in daily.items():
                    ym = day_str[:7]
                    if ym not in monthly_cache:
                        monthly_cache[ym] = load_monthly_file(ym)
                    monthly_cache[ym].setdefault(day_str, {})[zone] = stats
                    if (latest_date is None or day_str > latest_date) and (metadata_limit is None or day_str <= metadata_limit):
                        latest_date = day_str
                print(f"OK ({len(daily)} dias)")
                time.sleep(0.5)
        except Exception as e:
            print(f"ERRO: {e}")
            time.sleep(2)

    print(f"\nA guardar {len(monthly_cache)} ficheiro(s)...")
    for ym, data in sorted(monthly_cache.items()):
        save_monthly_file(ym, data)
        print(f"  {ym}.json ({len(data)} dias)")

    if latest_date:
        save_metadata(latest_date)
        print(f"\nMetadata: ultima_data = {latest_date}")

    print("Concluido!")


if __name__ == "__main__":
    main()
