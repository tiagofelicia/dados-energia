"""
Script de recolha INTRA-DIARIA para a pagina balanco-europa.html.

Fonte unica: ENTSO-E Transparency Platform (web-api.tp.entsoe.eu).
Obrigatorio: variavel de ambiente ENTSOE_TOKEN.

Endpoints utilizados:
  A75 / A16 — Actual generation per type (Solar, Wind, Nuclear, Gas, etc., + Pumped Storage in/out)
  A65 / A16 — Total Load actual
  A65 / A01 — Total Load forecast (day-ahead)
  A11      — Cross-border physical flow actual (saldo importador realizado)
  A09 / A01 — Day-ahead scheduled commercial exchanges (saldo importador previsto)
  A69 / A01 — Wind & Solar generation forecast (day-ahead)
  A69 / A40 — Wind & Solar generation forecast (intraday refresh; quando disponivel)
  A71 / A01 — Total generation forecast (day-ahead)
  A44      — Day-ahead auction prices (EUR/MWh)

Modos:
  python atualizar_producao_entsoe.py                     # diario: ultimos 10 dias (~2 semanas ISO)
  python atualizar_producao_entsoe.py --days 2            # ontem + hoje (semana ISO correspondente)
  python atualizar_producao_entsoe.py --weeks 1          # so a semana ISO atual
  python atualizar_producao_entsoe.py --weeks 3          # ultimas 3 semanas ISO
  python atualizar_producao_entsoe.py --countries pt,es  # so PT e ES (combinavel com --days/--weeks)
  python atualizar_producao_entsoe.py --backfill         # desde 2026-01-01

Guarda 1 ficheiro por pais-semana ISO (local time do pais):
  data/producao/producao-entsoe/{cc}/{YYYY}-W{WW}.json
  data/producao/producao-entsoe/metadata.json

Estrutura JSON (mantida compativel com a versao Energy-Charts anterior):
{
  country, iso_year, iso_week, timezone, interval_minutes,
  start_local, end_local, last_actual_ts,
  unix_seconds:        [...],                        # grid actuals
  production_types:    [{name, data:[...]}, ...],   # actuals por fonte (A75 + Load A65 + Cross-border A11)
  forecast: {                                        # forecasts (A69 + A65/A01)
    unix_seconds: [...],
    by_name: { "Solar":[...], "Wind onshore":[...], "Wind offshore":[...], "Load":[...] }
  },
  price:               {unix_seconds, values, bzn, unit},   # A44
  exchanges_forecast:  {unix_seconds, net_mw, by_neighbor}, # A09
  total_generation_forecast_entsoe: {unix_seconds, values},  # A71/A01 (day-ahead)
  wind_solar_forecast_intraday_entsoe: {unix_seconds, by_name}  # A69/A40 (intraday, opcional)
}
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import entsoe_client


COUNTRIES = [
    "at", "be", "bg", "ch", "cz", "de", "dk", "ee", "es", "fi",
    "fr", "gr", "hr", "hu", "ie", "it", "lt", "lv", "me", "nl",
    "no", "pl", "pt", "ro", "rs", "se", "si", "sk",
    # Balcãs com mix parcial na ENTSO-E (ver probe_entsoe_paises.py)
    "al", "mk", "xk",
]

COUNTRY_TIMEZONES = {
    "at": "Europe/Vienna", "be": "Europe/Brussels", "ch": "Europe/Zurich",
    "cz": "Europe/Prague", "de": "Europe/Berlin",
    "dk": "Europe/Copenhagen", "es": "Europe/Madrid", "fr": "Europe/Paris",
    "hr": "Europe/Zagreb", "hu": "Europe/Budapest", "it": "Europe/Rome",
    "me": "Europe/Podgorica", "nl": "Europe/Amsterdam", "no": "Europe/Oslo",
    "pl": "Europe/Warsaw", "rs": "Europe/Belgrade",
    "se": "Europe/Stockholm", "si": "Europe/Ljubljana", "sk": "Europe/Bratislava",
    "pt": "Europe/Lisbon", "ie": "Europe/Dublin",
    "bg": "Europe/Sofia", "ee": "Europe/Tallinn",
    "fi": "Europe/Helsinki", "gr": "Europe/Athens",
    "lt": "Europe/Vilnius", "lv": "Europe/Riga",
    "ro": "Europe/Bucharest",
    "al": "Europe/Tirane", "mk": "Europe/Skopje", "xk": "Europe/Belgrade",
}

# Caminho ancorado no diretório do script (e não no cwd), para funcionar
# quando corrido da raiz do repositório dados-energia ou de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data", "producao", "producao-entsoe")
BACKFILL_START = "2026-01-01"
ROUND_DECIMALS = 1     # MW
PRICE_DECIMALS = 2     # EUR/MWh


# ---------- Helpers de ISO week + alinhamento ----------

def iso_week_bounds_local(iso_year, iso_week, tz):
    monday_naive = datetime.strptime(f"{iso_year}-W{iso_week:02d}-1", "%G-W%V-%u")
    start_local = monday_naive.replace(tzinfo=tz)
    end_local = (monday_naive + timedelta(days=7)).replace(tzinfo=tz)
    return start_local, end_local


def weeks_between(start_date, end_date):
    seen = set()
    d = start_date
    while d <= end_date:
        iso = d.isocalendar()
        key = (iso.year, iso.week)
        if key not in seen:
            seen.add(key)
            yield key
        d += timedelta(days=1)


def _align_to_grid(series, grid_sorted):
    """Alinha [(ts,val)] a uma grid ordenada usando match exacto + nearest-neighbor
    dentro de tolerancia (metade do intervalo da serie origem). Devolve lista
    com len(grid_sorted), None onde nao ha match.
    """
    if not series:
        return [None] * len(grid_sorted)
    series_dict = {ts: val for ts, val in series}
    if not series_dict:
        return [None] * len(grid_sorted)
    series_ts = sorted(series_dict.keys())
    if len(series_ts) >= 2:
        series_interval = series_ts[1] - series_ts[0]
    else:
        series_interval = 3600
    tol = max(60, series_interval // 2)

    out = []
    for g in grid_sorted:
        if g in series_dict:
            out.append(series_dict[g])
            continue
        # Busca binaria do mais proximo
        if g <= series_ts[0]:
            best = series_ts[0]
        elif g >= series_ts[-1]:
            best = series_ts[-1]
        else:
            lo, hi = 0, len(series_ts) - 1
            while lo + 1 < hi:
                mid = (lo + hi) // 2
                if series_ts[mid] <= g:
                    lo = mid
                else:
                    hi = mid
            best = series_ts[lo] if abs(series_ts[lo] - g) <= abs(series_ts[hi] - g) else series_ts[hi]
        if abs(best - g) <= tol:
            out.append(series_dict[best])
        else:
            out.append(None)
    return out


# ---------- Collectors ENTSO-E ----------

def collect_actuals(country, start_local, end_local, token):
    """A75 + A65/A16 + A11 → formato production_types compatível com versão anterior.

    Devolve dict {interval_minutes, unix_seconds, last_actual_ts, production_types}.
    Returns None se A75 falhar (sem actuals = sem dados úteis).
    """
    eic = entsoe_client.EIC_MAP.get(country)
    if not eic:
        return None

    # 1. A75 — generation per PSR type
    try:
        gen_typed = entsoe_client.fetch_actual_generation(eic, start_local, end_local, token)
        time.sleep(0.6)
    except Exception as e:
        print(f"  [A75 {country}: {e}]")
        return None
    if not gen_typed:
        return None

    # Determinar grid (uniao de timestamps) e filtrar à janela local
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    grid = set()
    for series in gen_typed.values():
        for (ts, _v) in series:
            if start_ts <= ts < end_ts:
                grid.add(ts)
    grid_sorted = sorted(grid)
    if not grid_sorted:
        return None

    # Intervalo
    if len(grid_sorted) >= 2:
        interval_min = (grid_sorted[1] - grid_sorted[0]) // 60
    else:
        interval_min = 60

    # 2. A65 / A16 — Load actual
    try:
        load_actual = entsoe_client.fetch_total_load(eic, start_local, end_local, token, forecast=False)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A65 actual {country}: {e}]")
        load_actual = []

    # 3. A11 — Cross-border physical flow (saldo)
    try:
        cb = entsoe_client.get_net_cross_border_actual(country, start_local, end_local, token)
    except Exception as e:
        print(f"  [A11 {country}: {e}]")
        cb = None

    # Construir production_types alinhados à grid
    production_types = []
    last_actual_ts = None

    for name, series in gen_typed.items():
        series_dict = {ts: val for ts, val in series}
        data = []
        for g in grid_sorted:
            v = series_dict.get(g)
            if v is None:
                data.append(None)
            else:
                data.append(round(float(v), ROUND_DECIMALS))
                if last_actual_ts is None or g > last_actual_ts:
                    last_actual_ts = g
        production_types.append({"name": name, "data": data})

    # Load
    if load_actual:
        load_aligned = _align_to_grid(load_actual, grid_sorted)
        production_types.append({
            "name": "Load",
            "data": [round(float(v), ROUND_DECIMALS) if v is not None else None for v in load_aligned],
        })

    # Cross-border (saldo: positivo = importação, negativo = exportação)
    if cb and cb.get("unix_seconds"):
        cb_series = list(zip(cb["unix_seconds"], cb["net_mw"]))
        cb_aligned = _align_to_grid(cb_series, grid_sorted)
        production_types.append({
            "name": "Cross border electricity trading",
            "data": [round(float(v), ROUND_DECIMALS) if v is not None else None for v in cb_aligned],
        })

    return {
        "interval_minutes": interval_min,
        "unix_seconds": grid_sorted,
        "last_actual_ts": last_actual_ts,
        "production_types": production_types,
    }


def collect_forecast(country, start_local, end_local, token):
    """A69 (Solar/Wind on/off) + A65/A01 (Load forecast) → formato forecast.

    Devolve dict {unix_seconds, by_name}.
    """
    eic = entsoe_client.EIC_MAP.get(country)
    if not eic:
        return None

    try:
        ws_typed = entsoe_client.fetch_wind_solar_forecast(eic, start_local, end_local, token)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A69 {country}: {e}]")
        ws_typed = {}

    try:
        load_fc = entsoe_client.fetch_total_load(eic, start_local, end_local, token, forecast=True)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A65 forecast {country}: {e}]")
        load_fc = []

    if not ws_typed and not load_fc:
        return None

    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())

    # Grid: união de timestamps na janela
    grid = set()
    for series in ws_typed.values():
        for (ts, _v) in series:
            if start_ts <= ts < end_ts:
                grid.add(ts)
    for (ts, _v) in load_fc:
        if start_ts <= ts < end_ts:
            grid.add(ts)
    grid_sorted = sorted(grid)
    if not grid_sorted:
        return None

    by_name = {}
    for name, series in ws_typed.items():
        series_dict = {ts: val for ts, val in series}
        aligned = []
        for g in grid_sorted:
            v = series_dict.get(g)
            aligned.append(round(float(v), ROUND_DECIMALS) if v is not None else None)
        by_name[name] = aligned

    if load_fc:
        load_aligned = _align_to_grid(load_fc, grid_sorted)
        by_name["Load"] = [
            round(float(v), ROUND_DECIMALS) if v is not None else None for v in load_aligned
        ]

    return {"unix_seconds": grid_sorted, "by_name": by_name}


def collect_wind_solar_forecast_intraday(country, start_local, end_local, token):
    """A69 / A40 — Wind & Solar forecast intraday.

    Devolve dict {unix_seconds, by_name} com apenas Solar / Wind onshore / Wind offshore
    (sem Load, que so existe em day-ahead). Devolve None se nao houver dados.
    """
    eic = entsoe_client.EIC_MAP.get(country)
    if not eic:
        return None
    try:
        ws_typed = entsoe_client.fetch_wind_solar_forecast_intraday(eic, start_local, end_local, token)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A69/A40 {country}: {e}]")
        return None
    if not ws_typed:
        return None
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    grid = set()
    for series in ws_typed.values():
        for (ts, _v) in series:
            if start_ts <= ts < end_ts:
                grid.add(ts)
    grid_sorted = sorted(grid)
    if not grid_sorted:
        return None
    by_name = {}
    for name, series in ws_typed.items():
        # Ignorar entradas que nao sao Solar/Wind onshore/Wind offshore (defesa)
        if name not in ("Solar", "Wind onshore", "Wind offshore"):
            continue
        series_dict = {ts: val for ts, val in series}
        aligned = []
        for g in grid_sorted:
            v = series_dict.get(g)
            aligned.append(round(float(v), ROUND_DECIMALS) if v is not None else None)
        by_name[name] = aligned
    if not by_name:
        return None
    return {"unix_seconds": grid_sorted, "by_name": by_name}


def collect_price(country, start_local, end_local, token):
    """A44 → bloco price."""
    eic = entsoe_client.EIC_MAP.get(country)
    if not eic:
        return None
    try:
        prices = entsoe_client.fetch_day_ahead_prices(eic, start_local, end_local, token)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A44 {country}: {e}]")
        return None
    if not prices:
        return None
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    secs, vals = [], []
    for ts, val in prices:
        if start_ts <= ts < end_ts and val is not None:
            secs.append(ts)
            vals.append(round(float(val), PRICE_DECIMALS))
    if not secs:
        return None
    return {
        "unix_seconds": secs,
        "values": vals,
        "bzn": eic,
        "unit": "EUR/MWh",
    }


def collect_exchanges_forecast(country, start_local, end_local, token):
    """A09 — saldo importador previsto (day-ahead)."""
    try:
        result = entsoe_client.get_net_imports_forecast(
            country, start_local, end_local, token, verbose=False,
        )
    except Exception as e:
        print(f"  [A09 {country}: {e}]")
        return None
    if not result:
        return None
    return {
        "unix_seconds": result["unix_seconds"],
        "net_mw": result["net_mw"],
        "by_neighbor": result.get("by_neighbor", {}),
        "source": "ENTSO-E A09 day-ahead",
    }


def collect_total_generation_forecast(country, start_local, end_local, token):
    """A71 — Total generation forecast day-ahead."""
    eic = entsoe_client.EIC_MAP.get(country)
    if not eic:
        return None
    try:
        series = entsoe_client.fetch_total_generation_forecast(eic, start_local, end_local, token)
        time.sleep(0.5)
    except Exception as e:
        print(f"  [A71 {country}: {e}]")
        return None
    if not series:
        return None
    start_ts = int(start_local.timestamp())
    end_ts = int(end_local.timestamp())
    secs, vals = [], []
    for ts, val in series:
        if start_ts <= ts < end_ts and val is not None:
            secs.append(ts)
            vals.append(round(float(val), ROUND_DECIMALS))
    if not secs:
        return None
    return {"unix_seconds": secs, "values": vals}


# ---------- I/O ----------

def week_filepath(country, iso_year, iso_week):
    return os.path.join(DATA_DIR, country, f"{iso_year}-W{iso_week:02d}.json")


def save_week(country, iso_year, iso_week, tz_name, start_local, end_local,
              actuals, forecast, price, exchanges, total_gen_fc, ws_fc_intraday):
    filepath = week_filepath(country, iso_year, iso_week)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    body = {
        "country": country,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "start_local": start_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_local": end_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": tz_name,
        "interval_minutes": actuals["interval_minutes"],
        "unix_seconds": actuals["unix_seconds"],
        "last_actual_ts": actuals.get("last_actual_ts"),
        "production_types": actuals["production_types"],
    }
    if forecast:
        body["forecast"] = {
            "unix_seconds": forecast["unix_seconds"],
            "by_name": forecast["by_name"],
        }
    if price:
        body["price"] = price
    if exchanges:
        body["exchanges_forecast"] = exchanges
    if total_gen_fc:
        body["total_generation_forecast_entsoe"] = total_gen_fc
    if ws_fc_intraday:
        body["wind_solar_forecast_intraday_entsoe"] = ws_fc_intraday
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, separators=(",", ":"))


def load_metadata():
    fp = os.path.join(DATA_DIR, "metadata.json")
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_metadata(meta):
    fp = os.path.join(DATA_DIR, "metadata.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)


# ---------- Pipeline ----------

def process_country_weeks(country, week_list, token):
    tz_name = COUNTRY_TIMEZONES.get(country, "Europe/Berlin")
    tz = ZoneInfo(tz_name)
    ok = 0
    for (iso_year, iso_week) in week_list:
        start_local, end_local = iso_week_bounds_local(iso_year, iso_week, tz)
        try:
            actuals = collect_actuals(country, start_local, end_local, token)
            if actuals is None:
                print(f"  {iso_year}-W{iso_week:02d}: sem actuals (skip)")
                time.sleep(1.0)
                continue

            forecast = collect_forecast(country, start_local, end_local, token)
            price = collect_price(country, start_local, end_local, token)
            exchanges = collect_exchanges_forecast(country, start_local, end_local, token)
            total_gen_fc = collect_total_generation_forecast(country, start_local, end_local, token)
            ws_fc_id = collect_wind_solar_forecast_intraday(country, start_local, end_local, token)

            save_week(
                country, iso_year, iso_week, tz_name,
                start_local, end_local, actuals, forecast, price, exchanges,
                total_gen_fc, ws_fc_id,
            )
            ok += 1
            tags = (
                "A"  # actuals (A75) — guaranteed if we got here
                + ("F" if forecast else "-")    # A69 + A65/A01
                + ("f" if ws_fc_id else "-")    # A69/A40 (intraday W&S)
                + ("P" if price else "-")       # A44
                + ("X" if exchanges else "-")   # A09
                + ("G" if total_gen_fc else "-")  # A71 day-ahead
            )
            print(f"  {iso_year}-W{iso_week:02d}: OK [{tags}]")
            time.sleep(1.0)
        except Exception as e:
            print(f"  ERRO {iso_year}-W{iso_week:02d}: {e}")
            time.sleep(2.5)
    return ok


def main():
    token = os.environ.get("ENTSOE_TOKEN")
    if not token:
        print("ERRO: variavel de ambiente ENTSOE_TOKEN nao definida.")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    backfill = "--backfill" in sys.argv
    today_utc = datetime.now(timezone.utc).date()

    # --weeks N / --days N  definem a janela; cada um recolhe as semanas ISO que
    # cobrem essa janela (o ficheiro guardado é sempre por semana ISO).
    #   --weeks 1 = so a semana atual;  --weeks 3 = ultimas 3 semanas
    #   --days 2  = ontem + hoje (1 semana; 2 semanas se atravessar segunda)
    def _parse_int_flag(name):
        for ai, arg in enumerate(sys.argv):
            if arg == name and ai + 1 < len(sys.argv):
                src = sys.argv[ai + 1]
            elif arg.startswith(name + "="):
                src = arg.split("=", 1)[1]
            else:
                continue
            try:
                return max(1, int(src))
            except ValueError:
                print(f"ERRO: {name} requer um inteiro >= 1.")
                sys.exit(1)
        return None
    weeks_arg = _parse_int_flag("--weeks")
    days_arg = _parse_int_flag("--days")

    # --countries pt,es,fr   filtra a lista por códigos (case-insensitive, ignora desconhecidos)
    selected_countries = COUNTRIES
    for ai, arg in enumerate(sys.argv):
        if arg == "--countries" and ai + 1 < len(sys.argv):
            requested = [c.strip().lower() for c in sys.argv[ai + 1].split(",") if c.strip()]
            unknown = [c for c in requested if c not in COUNTRIES]
            if unknown:
                print(f"AVISO: códigos desconhecidos ignorados: {', '.join(unknown)}")
            selected_countries = [c for c in requested if c in COUNTRIES]
            if not selected_countries:
                print(f"ERRO: nenhum código válido em --countries. Disponíveis: {', '.join(COUNTRIES)}")
                sys.exit(1)
            break
        if arg.startswith("--countries="):
            requested = [c.strip().lower() for c in arg.split("=", 1)[1].split(",") if c.strip()]
            selected_countries = [c for c in requested if c in COUNTRIES]
            if not selected_countries:
                print(f"ERRO: nenhum código válido em --countries.")
                sys.exit(1)
            break

    if backfill:
        start = datetime.strptime(BACKFILL_START, "%Y-%m-%d").date()
        end = today_utc + timedelta(days=1)
        print(f"Modo BACKFILL: {start} -> {end}")
    elif weeks_arg is not None:
        # Recua (N-1) semanas; weeks_between(start, end) cobre N semanas ISO
        start = today_utc - timedelta(days=7 * (weeks_arg - 1))
        end = today_utc + timedelta(days=1)
        print(f"Modo SEMANAS ({weeks_arg}): {start} -> {end}")
    elif days_arg is not None:
        # Recua (N-1) dias; processa a(s) semana(s) ISO que cobrem esses dias
        start = today_utc - timedelta(days=(days_arg - 1))
        end = today_utc + timedelta(days=1)
        print(f"Modo DIAS ({days_arg}): {start} -> {end}")
    else:
        start = today_utc - timedelta(days=10)
        end = today_utc + timedelta(days=1)
        print(f"Modo DIARIO: {start} -> {end}")

    if selected_countries is not COUNTRIES:
        print(f"Países seleccionados: {', '.join(c.upper() for c in selected_countries)}")

    meta = load_metadata()
    total_ok = 0

    for i, country in enumerate(selected_countries, 1):
        tz = ZoneInfo(COUNTRY_TIMEZONES.get(country, "Europe/Berlin"))
        start_local = datetime.combine(start, datetime.min.time()).replace(tzinfo=tz)
        end_local = datetime.combine(end, datetime.min.time()).replace(tzinfo=tz)
        weeks = list(weeks_between(start_local.date(), end_local.date()))
        print(f"[{i}/{len(selected_countries)}] {country.upper()} ({len(weeks)} semana(s))")
        n = process_country_weeks(country, weeks, token)
        total_ok += n
        if weeks:
            last = weeks[-1]
            meta[country] = {
                "ultima_iso_year": last[0],
                "ultima_iso_week": last[1],
                "eic": entsoe_client.EIC_MAP.get(country),
                "actualizado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

    save_metadata(meta)
    print(f"\nConcluido. {total_ok} ficheiros semanais gravados.")


if __name__ == "__main__":
    main()
