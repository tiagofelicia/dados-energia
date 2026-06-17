"""
Cliente ligeiro para a API ENTSO-E Transparency Platform.

Foco actual: scheduled commercial exchanges (day-ahead, A09) entre cada um dos
28 paises e os seus vizinhos, para poder calcular import/export liquido previsto.

Usar:
    import os
    import entsoe_client as ec
    token = os.environ['ENTSOE_TOKEN']
    net = ec.get_net_imports_forecast('pt', start_dt_utc, end_dt_utc, token)
    # net = {'unix_seconds': [...], 'net_mw': [...], 'by_neighbor': {'es': [...]}}

Notas:
- Todos os timestamps de periodStart/periodEnd em UTC, formato YYYYMMDDhhmm.
- A API devolve XML; aqui parseamos com ElementTree.
- Resolucao tipica: PT60M (1 valor por hora). Pontos em falta = 0 ou herdam o anterior.
"""

import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"

# EIC codes principais por pais (bidding zone usado nos leiloes day-ahead).
# Para paises com multiplas zonas, escolhida zona representativa (DE-LU uniao,
# DK1, NO2, SE3, IT-North, IE-SEM).
EIC_MAP = {
    "at": "10YAT-APG------L",
    "be": "10YBE----------2",
    "bg": "10YCA-BULGARIA-R",
    "ch": "10YCH-SWISSGRIDZ",
    "cz": "10YCZ-CEPS-----N",
    "de": "10Y1001A1001A82H",  # DE-LU
    "dk": "10YDK-1--------W",  # DK1 (Jutland)
    "ee": "10Y1001A1001A39I",
    "es": "10YES-REE------0",
    "fi": "10YFI-1--------U",
    "fr": "10YFR-RTE------C",
    "gr": "10YGR-HTSO-----Y",
    "hr": "10YHR-HEP------M",
    "hu": "10YHU-MAVIR----U",
    "ie": "10Y1001A1001A59C",  # IE-SEM
    "it": "10Y1001A1001A73I",  # IT-North
    "lt": "10YLT-1001A0008Q",
    "lv": "10YLV-1001A00074",
    "me": "10YCS-CG-TSO---S",
    "nl": "10YNL----------L",
    "no": "10YNO-1--------2",  # NO1
    "pl": "10YPL-AREA-----S",
    "pt": "10YPT-REN------W",
    "ro": "10YRO-TEL------P",
    "rs": "10YCS-SERBIATSOV",
    "se": "10Y1001A1001A46L",  # SE3
    "si": "10YSI-ELES-----O",
    "sk": "10YSK-SEPS-----K",
    # Balcãs — mix parcial na ENTSO-E (poucas séries A75), mas com dados.
    "al": "10YAL-KESH-----5",   # Albânia (quase só hídrica; load A65 muito esparso)
    "mk": "10YMK-MEPSO----8",   # Macedónia do Norte (mais completo dos três)
    "xk": "10Y1001C--00100H",   # Kosovo (geração ~só lignite; load + preço OK)
}

# Vizinhos (bidding zones) por pais — usado para iterar pares no scheduled exchanges.
# Inclui vizinhos externos (GB, MA, UA, BY, MD, AL, MK, BA, TR) com seus EICs
# para nao perder fluxos relevantes (ex: ES-MA, IE-GB, RO-MD).
EIC_EXTRA = {
    "gb": "10YGB----------A",      # Great Britain
    "ma": "10YMA-MAR-CTL---",       # Morocco (placeholder; ENTSO-E pode nao ter)
    "ua_bei": "10YUA-WEPS-----0",   # Ukraine BEI (Burshtyn island)
    "ua_ips": "10Y1001C--00003F",   # Ukraine IPS
    "by": "10Y1001A1001A51S",       # Belarus
    "md": "10Y1001A1001A990",       # Moldova
    "al": "10YAL-KESH-----5",       # Albania
    "mk": "10YMK-MEPSO----8",       # North Macedonia
    "ba": "10YBA-JPCC-----D",       # Bosnia & Herzegovina
    "tr": "10YTR-TEIAS----W",       # Turkey
    # Zonas adicionais dentro dos paises ja listados (usadas em vizinhanca):
    "dk2": "10YDK-2--------M",
    "no2": "10YNO-2--------T",
    "no3": "10YNO-3--------J",
    "no4": "10YNO-4--------9",
    "no5": "10Y1001A1001A48H",
    "se1": "10Y1001A1001A44P",
    "se2": "10Y1001A1001A45N",
    "se4": "10Y1001A1001A47J",
    "it_centre_north": "10Y1001A1001A70O",
    "it_south": "10Y1001A1001A788",
}

# Vizinhos por pais (lista de chaves do EIC_MAP/EIC_EXTRA).
# Foco no PT primeiro; restantes paises suportados mas com sub-zonas omitidas
# (limitacao razoavel — saldo principal cobre 90%+ do fluxo).
NEIGHBORS = {
    "pt": ["es"],
    "es": ["pt", "fr", "ma"],
    "fr": ["es", "be", "de", "ch", "it", "gb"],
    "be": ["fr", "nl", "de", "gb"],
    "nl": ["be", "de", "no", "gb", "dk"],
    "de": ["fr", "be", "nl", "ch", "at", "cz", "pl", "dk", "se"],
    "ch": ["de", "fr", "it", "at"],
    "at": ["de", "ch", "it", "si", "hu", "cz"],
    "it": ["fr", "ch", "at", "si", "gr"],  # via IT-North/IT-South
    "si": ["at", "it", "hr", "hu"],
    "hr": ["si", "hu", "rs", "ba"],
    "hu": ["at", "sk", "ro", "rs", "hr", "ua_bei"],
    "ro": ["hu", "bg", "rs", "md", "ua_bei"],
    "bg": ["ro", "gr", "rs", "mk", "tr"],
    "gr": ["bg", "it", "al", "mk", "tr"],
    "rs": ["hu", "ro", "bg", "me", "ba", "mk", "al", "hr"],
    "ba": ["hr", "rs", "me"],
    "me": ["al", "rs", "ba", "it"],
    "cz": ["de", "pl", "sk", "at"],
    "sk": ["cz", "at", "hu", "pl", "ua_bei"],
    "pl": ["de", "cz", "sk", "lt", "se", "ua_ips"],
    "dk": ["de", "no", "se", "nl", "dk2"],
    "se": ["fi", "no", "dk", "pl", "lt", "se1", "se2", "se4"],
    "no": ["se", "dk", "nl", "no2", "no3", "no5"],
    "fi": ["se", "no", "ee"],
    "ee": ["fi", "lv"],
    "lv": ["ee", "lt"],
    "lt": ["lv", "pl", "se"],
    "ie": ["gb"],
}


def _eic(key):
    return EIC_MAP.get(key) or EIC_EXTRA.get(key)


def _fmt_period(dt):
    """Formato ENTSO-E: YYYYMMDDhhmm em UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


def _get(params, token, retries=3):
    """GET com retry em 429/5xx."""
    params = dict(params)
    params['securityToken'] = token
    url = ENTSOE_BASE + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=60)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code in (400, 404):
            # API devolve 400 quando nao ha dados
            return None
        if resp.status_code >= 500:
            time.sleep(3 * (attempt + 1))
            continue
        return resp.text
    return None


def _strip_ns(tag):
    return tag.split('}', 1)[1] if '}' in tag else tag


# Mapeamento PSR type code -> nome canónico (alinhado com Energy-Charts/ficheiros JSON existentes)
PSR_TYPES = {
    'B01': 'Biomass',
    'B02': 'Fossil brown coal / lignite',
    'B03': 'Fossil coal-derived gas',
    'B04': 'Fossil gas',
    'B05': 'Fossil hard coal',
    'B06': 'Fossil oil',
    'B07': 'Fossil oil shale',
    'B08': 'Fossil peat',
    'B09': 'Geothermal',
    'B10': 'Hydro pumped storage',
    'B11': 'Hydro Run-of-River',
    'B12': 'Hydro water reservoir',
    'B13': 'Marine',
    'B14': 'Nuclear',
    'B15': 'Other renewables',
    'B16': 'Solar',
    'B17': 'Waste',
    'B18': 'Wind offshore',
    'B19': 'Wind onshore',
    'B20': 'Others',
    # B21-B24 = AC/DC Link, Substation, Transformer (não são geração).
    # B25 = Energy storage (baterias). Lado de descarga = geração; com
    # _extract_psr_with_direction o consumo (carga) sai como 'Energy storage consumption'.
    'B25': 'Energy storage',
}


def _parse_period_points(period_elem):
    """Itera Points de um <Period>. Retorna lista (unix_ts, value)."""
    time_interval_start = None
    resolution_min = 60
    for child in period_elem:
        tg = _strip_ns(child.tag)
        if tg == 'timeInterval':
            for c2 in child:
                if _strip_ns(c2.tag) == 'start':
                    time_interval_start = c2.text
        elif tg == 'resolution':
            res = (child.text or '').strip()
            if res.startswith('PT') and res.endswith('M'):
                try:
                    resolution_min = int(res[2:-1])
                except ValueError:
                    resolution_min = 60
            elif res == 'PT1H':
                resolution_min = 60
    if not time_interval_start:
        return []
    try:
        start_dt = datetime.strptime(time_interval_start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            start_dt = datetime.strptime(time_interval_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return []
    out = []
    for pt in period_elem:
        if _strip_ns(pt.tag) != 'Point':
            continue
        pos = None
        qty = None
        for c3 in pt:
            tg3 = _strip_ns(c3.tag)
            if tg3 == 'position':
                try:
                    pos = int(c3.text)
                except (TypeError, ValueError):
                    pass
            # quantity (geração/load) ou price.amount (preços A44)
            elif tg3 in ('quantity', 'price.amount'):
                try:
                    qty = float(c3.text)
                except (TypeError, ValueError):
                    pass
        if pos is None or qty is None:
            continue
        ts_dt = start_dt + timedelta(minutes=(pos - 1) * resolution_min)
        out.append((int(ts_dt.timestamp()), qty))
    return out


def parse_timeseries_points(xml_text):
    """Devolve lista de (unix_ts_utc, value), um valor por timestamp.

    Usado para grandezas ESCALARES por timestamp (A44 preço, A65 load, A71 total
    generation forecast). NÃO somar TimeSeries sobrepostas: a ENTSO-E pode devolver
    várias TimeSeries para o mesmo período (revisões/versões, ou 60min + 15min — caso
    do preço day-ahead de Espanha, que vinha ~3x inflacionado por ser somado). Fica-se
    com o ÚLTIMO valor por timestamp (ordem do documento ≈ revisão mais recente).
    Para séries não-sobrepostas (ex.: 1 TimeSeries/dia) o resultado é idêntico ao de
    antes — cada timestamp aparece uma só vez.
    """
    if not xml_text:
        return []
    if "No matching data found" in xml_text or "<Acknowledgement_MarketDocument" in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    by_ts = {}
    for ts_elem in root.iter():
        if _strip_ns(ts_elem.tag) != 'TimeSeries':
            continue
        for period in ts_elem:
            if _strip_ns(period.tag) != 'Period':
                continue
            for k, v in _parse_period_points(period):
                by_ts[k] = v  # último valor por timestamp (não somar sobreposições)
    return sorted(by_ts.items())


def _is_consumption_ts(ts_elem):
    """Detecta se um TimeSeries representa consumo (ex: bombagem) por presença de outBiddingZone_Domain.mRID."""
    has_in = False
    has_out = False
    for child in ts_elem.iter():
        tg = _strip_ns(child.tag)
        if tg == 'inBiddingZone_Domain.mRID':
            has_in = True
        elif tg == 'outBiddingZone_Domain.mRID':
            has_out = True
    # Pure consumption: só out, sem in
    return has_out and not has_in


def _extract_psr_with_direction(ts_elem):
    """Devolve nome canónico para o TimeSeries. None se não tem psrType."""
    psr_code = None
    for child in ts_elem.iter():
        if _strip_ns(child.tag) == 'psrType':
            psr_code = child.text
            break
    if psr_code is None:
        return None
    name = PSR_TYPES.get(psr_code, psr_code)
    if _is_consumption_ts(ts_elem):
        name = name + ' consumption'
    return name


def parse_typed_timeseries(xml_text, type_extractor):
    """Parse XML com múltiplas TimeSeries categorizadas por tipo.

    type_extractor: função(ts_elem) -> str | None (chave para agrupar)
    Devolve dict {tipo: [(unix_ts, value), ...]}.
    """
    if not xml_text:
        return {}
    if "No matching data found" in xml_text or "<Acknowledgement_MarketDocument" in xml_text:
        return {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    by_type = {}
    for ts_elem in root.iter():
        if _strip_ns(ts_elem.tag) != 'TimeSeries':
            continue
        type_key = type_extractor(ts_elem)
        if type_key is None:
            continue
        if type_key not in by_type:
            by_type[type_key] = {}
        for period in ts_elem:
            if _strip_ns(period.tag) != 'Period':
                continue
            for k, v in _parse_period_points(period):
                by_type[type_key][k] = by_type[type_key].get(k, 0.0) + v
    return {k: sorted(v.items()) for k, v in by_type.items()}


def fetch_scheduled_exchange(in_eic, out_eic, start_dt, end_dt, token):
    """Day-ahead finalised schedule (A09 / A01) entre duas zonas.

    Devolve lista [(unix_ts, mw)] com fluxo de out -> in (ou seja, valor
    positivo = quantidade entregue na in_Domain por out_Domain).
    """
    params = {
        'documentType': 'A09',
        'contract_MarketAgreement.Type': 'A01',
        'in_Domain': in_eic,
        'out_Domain': out_eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_timeseries_points(xml)


def fetch_actual_generation(eic, start_dt, end_dt, token):
    """A75 / A16 — Actual generation per type. Multi-PSR.

    Devolve dict {nome_canónico: [(unix_ts, mw)]}.
    Inclui 'Hydro pumped storage consumption' como chave separada quando há outflow.
    """
    params = {
        'documentType': 'A75',
        'processType': 'A16',
        'in_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_typed_timeseries(xml, _extract_psr_with_direction)


def fetch_total_generation_forecast(eic, start_dt, end_dt, token):
    """A71 / A01 — Total day-ahead generation forecast (uma série única, MW)."""
    params = {
        'documentType': 'A71',
        'processType': 'A01',
        'in_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_timeseries_points(xml)


def fetch_wind_solar_forecast(eic, start_dt, end_dt, token):
    """A69 / A01 — Wind & Solar generation forecast day-ahead. Devolve dict por tipo."""
    params = {
        'documentType': 'A69',
        'processType': 'A01',
        'in_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_typed_timeseries(xml, _extract_psr_with_direction)


def fetch_wind_solar_forecast_intraday(eic, start_dt, end_dt, token):
    """A69 / A40 — Wind & Solar forecast intraday (refrescado várias vezes ao dia).

    Estrutura idêntica à day-ahead — dict {tipo: [(unix_ts, mw), ...]}.
    Nem todos os países publicam; devolve {} silenciosamente se não houver dados.
    """
    params = {
        'documentType': 'A69',
        'processType': 'A40',
        'in_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_typed_timeseries(xml, _extract_psr_with_direction)


def fetch_total_load(eic, start_dt, end_dt, token, forecast=False):
    """A65 — Total load. forecast=True (A01) day-ahead, False (A16) actual."""
    params = {
        'documentType': 'A65',
        'processType': 'A01' if forecast else 'A16',
        'outBiddingZone_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_timeseries_points(xml)


def fetch_day_ahead_prices(eic, start_dt, end_dt, token):
    """A44 — Day-ahead auction prices (EUR/MWh). EIC nas duas direcções (in=out)."""
    params = {
        'documentType': 'A44',
        'in_Domain': eic,
        'out_Domain': eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_timeseries_points(xml)


def fetch_cross_border_physical_flow(in_eic, out_eic, start_dt, end_dt, token):
    """A11 — Cross-border physical flow (actuals) entre duas zonas. Sem processType."""
    params = {
        'documentType': 'A11',
        'in_Domain': in_eic,
        'out_Domain': out_eic,
        'periodStart': _fmt_period(start_dt),
        'periodEnd': _fmt_period(end_dt),
    }
    xml = _get(params, token)
    return parse_timeseries_points(xml)


def get_net_cross_border_actual(country, start_dt, end_dt, token, verbose=False):
    """A11 actuals — saldo importador realizado por país.

    Estrutura igual a get_net_imports_forecast: dict
        {unix_seconds, net_mw, by_neighbor: {nb: [...]}}
    """
    cc = country.lower()
    home_eic = EIC_MAP.get(cc)
    if not home_eic:
        return None
    neighbors = NEIGHBORS.get(cc, [])
    if not neighbors:
        return None

    net_total = {}
    by_neighbor = {}

    for nb in neighbors:
        nb_eic = _eic(nb)
        if not nb_eic:
            continue
        try:
            imports = fetch_cross_border_physical_flow(home_eic, nb_eic, start_dt, end_dt, token)
            time.sleep(0.4)
            exports = fetch_cross_border_physical_flow(nb_eic, home_eic, start_dt, end_dt, token)
            time.sleep(0.4)
        except Exception as e:
            if verbose:
                print(f"  [A11 {cc}-{nb}: {e}]")
            continue

        nb_net = {}
        for ts, mw in imports:
            nb_net[ts] = nb_net.get(ts, 0.0) + mw
        for ts, mw in exports:
            nb_net[ts] = nb_net.get(ts, 0.0) - mw

        if nb_net:
            by_neighbor[nb] = nb_net
            for ts, mw in nb_net.items():
                net_total[ts] = net_total.get(ts, 0.0) + mw

    if not net_total:
        return None

    sorted_ts = sorted(net_total.keys())
    out = {
        'unix_seconds': sorted_ts,
        'net_mw': [round(net_total[t], 1) for t in sorted_ts],
        'by_neighbor': {},
    }
    for nb, nb_net in by_neighbor.items():
        out['by_neighbor'][nb] = [round(nb_net.get(t, 0.0), 1) for t in sorted_ts]
    return out


def get_net_imports_forecast(country, start_dt, end_dt, token, verbose=False):
    """Saldo importador previsto (day-ahead) para um pais.

    Para cada vizinho conhecido faz duas chamadas:
        in=country, out=neighbor  ->  imports
        in=neighbor, out=country  ->  exports
    Net = imports - exports.

    Devolve dict:
        {
            'unix_seconds': [...],          # timestamps UTC ordenados
            'net_mw': [...],                # +import / -export
            'by_neighbor': {                # detalhe por vizinho (em MW liquido)
                'es': [...],
                'fr': [...],
                ...
            }
        }
    """
    cc = country.lower()
    home_eic = EIC_MAP.get(cc)
    if not home_eic:
        return None
    neighbors = NEIGHBORS.get(cc, [])
    if not neighbors:
        return None

    # Mapa ts -> mw acumulado
    net_total = {}
    by_neighbor = {}

    for nb in neighbors:
        nb_eic = _eic(nb)
        if not nb_eic:
            continue
        try:
            imports = fetch_scheduled_exchange(home_eic, nb_eic, start_dt, end_dt, token)
            time.sleep(0.4)
            exports = fetch_scheduled_exchange(nb_eic, home_eic, start_dt, end_dt, token)
            time.sleep(0.4)
        except Exception as e:
            if verbose:
                print(f"  [entsoe {cc}-{nb}: {e}]")
            continue

        nb_net = {}
        for ts, mw in imports:
            nb_net[ts] = nb_net.get(ts, 0.0) + mw
        for ts, mw in exports:
            nb_net[ts] = nb_net.get(ts, 0.0) - mw

        if nb_net:
            by_neighbor[nb] = nb_net
            for ts, mw in nb_net.items():
                net_total[ts] = net_total.get(ts, 0.0) + mw

    if not net_total:
        return None

    sorted_ts = sorted(net_total.keys())
    out = {
        'unix_seconds': sorted_ts,
        'net_mw': [round(net_total[t], 1) for t in sorted_ts],
        'by_neighbor': {},
    }
    for nb, nb_net in by_neighbor.items():
        out['by_neighbor'][nb] = [round(nb_net.get(t, 0.0), 1) for t in sorted_ts]
    return out


if __name__ == '__main__':
    # Smoke test rapido para PT na semana actual
    import os
    tok = os.environ.get('ENTSOE_TOKEN')
    if not tok:
        print("Define ENTSOE_TOKEN no ambiente para testar.")
        raise SystemExit(1)
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    next_monday = monday + timedelta(days=7)
    print(f"PT scheduled exchanges {monday} -> {next_monday}")
    res = get_net_imports_forecast('pt', monday, next_monday, tok, verbose=True)
    if res:
        print(f"  {len(res['unix_seconds'])} pontos, vizinhos: {list(res['by_neighbor'].keys())}")
        if res['unix_seconds']:
            t0 = datetime.fromtimestamp(res['unix_seconds'][0], tz=timezone.utc)
            print(f"  primeiro ts: {t0}, net: {res['net_mw'][0]:.1f} MW")
            tlast = datetime.fromtimestamp(res['unix_seconds'][-1], tz=timezone.utc)
            print(f"  ultimo  ts: {tlast}, net: {res['net_mw'][-1]:.1f} MW")
    else:
        print("  Sem dados.")
