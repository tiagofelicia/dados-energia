"""
Script de recolha de dados de producao de eletricidade por pais europeu.
Utiliza a API Energy-Charts (/public_power) para obter dados de producao
publica liquida por tipo de fonte (solar, eolica, hidrica, nuclear, etc.).

Modo diario (default):  python atualizar_mapa_producao.py
Modo backfill:          python atualizar_mapa_producao.py --backfill

Gera ficheiros JSON mensais em data/mapa_producao/ (ex: 2026-01.json)
e um metadata.json com a ultima data disponivel.
"""

import requests
import json
import os
import sys
import time
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Paises disponiveis na API Energy-Charts (parametro country, minusculas)
COUNTRIES = [
    "at", "be", "bg", "ch", "cz", "de", "dk", "ee", "es", "fi",
    "fr", "gr", "hr", "hu", "ie", "it", "lt", "lv", "me", "nl",
    "no", "pl", "pt", "ro", "rs", "se", "si", "sk",
]  # 28 paises

# Fuso horario local de cada pais
# Critico: os timestamps da API sao UTC, mas o dia deve ser agrupado pela hora LOCAL
COUNTRY_TIMEZONES = {
    # CET/CEST (UTC+1/+2) - Europa Central
    "at": "Europe/Vienna", "be": "Europe/Brussels", "ch": "Europe/Zurich",
    "cz": "Europe/Prague", "de": "Europe/Berlin",
    "dk": "Europe/Copenhagen", "es": "Europe/Madrid", "fr": "Europe/Paris",
    "hr": "Europe/Zagreb", "hu": "Europe/Budapest", "it": "Europe/Rome",
    "me": "Europe/Podgorica", "nl": "Europe/Amsterdam", "no": "Europe/Oslo",
    "pl": "Europe/Warsaw", "rs": "Europe/Belgrade",
    "se": "Europe/Stockholm", "si": "Europe/Ljubljana", "sk": "Europe/Bratislava",
    # WET/WEST (UTC+0/+1) - Portugal e Irlanda
    "pt": "Europe/Lisbon", "ie": "Europe/Dublin",
    # EET/EEST (UTC+2/+3) - Europa Oriental
    "bg": "Europe/Sofia", "ee": "Europe/Tallinn",
    "fi": "Europe/Helsinki", "gr": "Europe/Athens",
    "lt": "Europe/Vilnius", "lv": "Europe/Riga",
    "ro": "Europe/Bucharest",
}

# Mapeamento: nome do tipo de producao (API) -> categoria agregada
# A API pode retornar nomes com capitalizacao variavel consoante o pais.
# Inclui todas as variantes conhecidas.
TYPE_MAP = {
    # Renováveis
    "Solar": "solar",
    "Wind onshore": "wind_onshore",
    "Wind offshore": "wind_offshore",
    "Hydro Run-of-River": "hydro_run_of_river",
    "Hydro run-of-river": "hydro_run_of_river",
    "Hydro Water Reservoir": "hydro_water_reservoir",
    "Hydro water reservoir": "hydro_water_reservoir",
    "Hydro Pumped Storage": "hydro_pumped_storage",
    "Hydro pumped storage": "hydro_pumped_storage",
    "Biomass": "biomass",
    "Geothermal": "geothermal",
    "Waste": "waste",
    "Other renewables": "other_renewables",
    
    # Não Renováveis
    "Nuclear": "nuclear",
    "Fossil gas": "gas",
    "Fossil coal-derived gas": "gas_coal_derived",
    "Fossil hard coal": "coal_hard",
    "Fossil brown coal / lignite": "coal_lignite",
    "Fossil brown coal/Lignite": "coal_lignite",
    "Fossil oil": "oil",
    "Oil": "oil",
    "Others": "other",
    "Other": "other",
    "other": "other",
    # Tipos adicionais (recolhidos para outros usos, nao contam para producao)
    "Cross border electricity trading": "cross_border",
    "Hydro pumped storage consumption": "pumped_storage_consumption",
    "Hydro Pumped Storage consumption": "pumped_storage_consumption",
    "Load": "load",
    "Residual load": "residual_load",
}

# Tipos a ignorar (valores derivados/calculados, nao sao dados primarios)
SKIP_TYPES = {
    "Renewable share of load",
    "Renewable share of generation",
}

# Categorias renovaveis (para calculo de ren_share)
RENEWABLE_CATS = {
    "solar", "wind_onshore", "wind_offshore",
    "hydro_run_of_river", "hydro_water_reservoir",
    "biomass", "geothermal", "waste", "other_renewables"
}

# Categorias de producao (contam para total e ren_share)
PRODUCTION_CATS = [
    "solar", "wind_onshore", "wind_offshore",
    "hydro_run_of_river", "hydro_water_reservoir", "hydro_pumped_storage",
    "biomass", "geothermal", "waste", "other_renewables",
    "nuclear", "gas", "gas_coal_derived", "coal_hard", "coal_lignite", "oil", "other"
]

# Categorias extra (recolhidas para outros usos, nao contam para total)
EXTRA_CATS = ["cross_border", "pumped_storage_consumption", "load", "residual_load"]

# Todas as categorias (ordem fixa para consistencia)
ALL_CATS = PRODUCTION_CATS + EXTRA_CATS

# Caminho ancorado no diretório do script (e não no cwd), para funcionar
# tanto quando é corrido a partir da raiz do repositório como de scripts/.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data", "mapas", "producao")
BACKFILL_START = "2026-01-01"
API_BASE = "https://api.energy-charts.info/public_power"
API_FORECAST_BASE = "https://api.energy-charts.info/public_power_forecast"

# Tipos de producao disponiveis na API de previsao (day-ahead)
FORECAST_TYPES = ["solar", "wind_onshore", "wind_offshore", "load"]


def fetch_production(country, start_date, end_date):
    """Buscar dados de producao da API Energy-Charts para um pais e intervalo.
    
    Inclui retry com backoff progressivo longo para lidar com erros 429 (rate limiting),
    e ignora erros 404 retornando dados vazios.
    """
    url = f"{API_BASE}?country={country}&start={start_date}&end={end_date}"
    
    for attempt in range(5):
        resp = requests.get(url, timeout=60)
        
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f" [Aviso API] Limite atingido. A aguardar {wait}s (Tentativa {attempt + 1}/5)...", end=" ", flush=True)
            time.sleep(wait)
            continue
            
        # Se o país não tiver dados (404 Not Found), devolvemos um dicionário vazio
        if resp.status_code == 404:
            return {}
            
        resp.raise_for_status()
        return resp.json()
        
    raise Exception(f"Falha ao obter dados após 5 tentativas devido a limites da API (Erro 429).")


def fetch_forecast(country, production_type):
    """Buscar previsao day-ahead da API Energy-Charts para um pais e tipo.

    Inclui retry com backoff para erros 429 (rate limiting).
    Retorna None se o recurso nao existe (404).
    """
    url = (
        f"{API_FORECAST_BASE}?country={country}"
        f"&production_type={production_type}&forecast_type=day-ahead"
    )
    for attempt in range(3):
        resp = requests.get(url, timeout=60)
        if resp.status_code in (400, 404):
            return None  # Tipo nao disponivel para este pais
        if resp.status_code == 429:
            wait = 5 * (attempt + 1)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    # Ultima tentativa falhou com 429
    return None


def compute_daily_forecasts(country, tz_name):
    """Buscar e calcular previsoes diarias de solar, eolica onshore e offshore.

    Chama a API de previsao day-ahead para cada tipo, agrupa por dia local,
    e converte MW*h para GWh.

    Retorna dict: {day_str: {solar: GWh, wind_onshore: GWh, wind_offshore: GWh}}
    """
    tz = ZoneInfo(tz_name)
    daily_fc = {}

    for ptype in FORECAST_TYPES:
        try:
            data = fetch_forecast(country, ptype)
            if data is None:
                continue  # Tipo nao disponivel (404) ou rate limited

            unix_secs = data.get("unix_seconds", [])
            values = data.get("forecast_values", [])

            if len(unix_secs) >= 2:
                interval_hours = (unix_secs[1] - unix_secs[0]) / 3600.0
            else:
                interval_hours = 1.0

            for i, ts in enumerate(unix_secs):
                if i >= len(values) or values[i] is None:
                    continue
                local_dt = datetime.fromtimestamp(ts, tz=tz)
                day = local_dt.strftime("%Y-%m-%d")

                if day not in daily_fc:
                    daily_fc[day] = {}
                daily_fc[day].setdefault(ptype, 0.0)
                daily_fc[day][ptype] += values[i] * interval_hours / 1000.0

            time.sleep(5)  # Respeitar rate limits da API
        except Exception as e:
            print(f"[fc:{ptype}:{e}]", end="")

    # Arredondar
    for day in daily_fc:
        for ptype in daily_fc[day]:
            daily_fc[day][ptype] = round(daily_fc[day][ptype], 2)

    return daily_fc


def compute_daily_production(api_data, tz_name):
    """Agrupar producao por dia LOCAL do pais e calcular totais diarios.

    Os timestamps da API sao UTC. O agrupamento deve usar a hora local
    do pais para que os dias coincidam com os valores publicados.

    NOTA: A API retorna valores em MW. O intervalo entre timestamps pode
    variar (15 min para DE, 1h para PT, etc.). O intervalo e detetado
    automaticamente a partir dos primeiros 2 timestamps.

    Retorna por dia: production (dict por categoria em GWh),
                     total (GWh), ren_share (%).
    """
    tz = ZoneInfo(tz_name)
    unix_seconds = api_data.get("unix_seconds", [])
    production_types = api_data.get("production_types", [])

    if not unix_seconds or not production_types:
        return {}

    # Detetar intervalo em horas a partir dos timestamps
    # (ex: 900s = 0.25h para 15 min, 3600s = 1h para horario)
    if len(unix_seconds) >= 2:
        interval_hours = (unix_seconds[1] - unix_seconds[0]) / 3600.0
    else:
        interval_hours = 1.0  # fallback: assumir 1 hora

    # Estrutura: daily[day][categoria] = lista de valores em MW
    daily = {}

    for ptype in production_types:
        type_name = ptype.get("name", "")
        data_values = ptype.get("data", [])

        # Ignorar tipos que nao sao producao real
        if type_name in SKIP_TYPES:
            continue

        category = TYPE_MAP.get(type_name, "other")
        is_pumped_storage = type_name in (
            "Hydro Pumped Storage", "Hydro pumped storage"
        )

        for i, ts in enumerate(unix_seconds):
            if i >= len(data_values):
                break
            value = data_values[i]
            if value is None:
                continue
            # Para Hydro Pumped Storage, ignorar valores negativos (consumo)
            if is_pumped_storage and value < 0:
                continue

            local_dt = datetime.fromtimestamp(ts, tz=tz)
            day = local_dt.strftime("%Y-%m-%d")

            if day not in daily:
                daily[day] = {cat: [] for cat in ALL_CATS}
            daily[day][category].append(value)

    # Converter MW para GWh diarios
    # Energia (MWh) = Potencia (MW) * intervalo (h)
    # GWh = MWh / 1000
    result = {}
    production_set = set(PRODUCTION_CATS)
    for day, categories in daily.items():
        production = {}
        total = 0.0
        ren_total = 0.0

        for cat in ALL_CATS:
            # sum(MW) * interval_hours = MWh total; /1000 = GWh
            gwh = sum(categories[cat]) * interval_hours / 1000.0
            production[cat] = round(gwh, 2)
            # Apenas categorias de producao contam para total e ren_share
            if cat in production_set:
                total += gwh
                if cat in RENEWABLE_CATS:
                    ren_total += gwh

        ren_share = round((ren_total / total) * 100, 2) if total > 0 else 0.0

        result[day] = {
            "ren_share": ren_share,
            "production": production,
            "total": round(total, 2),
        }

    return result


def load_monthly_file(year_month):
    """Carregar ficheiro JSON mensal existente, ou retornar dict vazio."""
    filepath = os.path.join(DATA_DIR, f"{year_month}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_monthly_file(year_month, data):
    """Guardar ficheiro JSON mensal."""
    filepath = os.path.join(DATA_DIR, f"{year_month}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True, ensure_ascii=False)


def save_metadata(latest_date):
    """Guardar metadata com a ultima data disponivel."""
    filepath = os.path.join(DATA_DIR, "metadata.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"ultima_data": latest_date}, f, ensure_ascii=False)


def get_month_ranges(start_date, end_date):
    """Gerar intervalos mensais entre duas datas (para backfill eficiente)."""
    ranges = []
    current = start_date.replace(day=1)
    while current <= end_date:
        month_start = max(current, start_date)
        # Ultimo dia do mes
        if current.month == 12:
            month_end_limit = current.replace(year=current.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end_limit = current.replace(month=current.month + 1, day=1) - timedelta(days=1)
        month_end = min(month_end_limit, end_date)
        ranges.append((month_start, month_end))
        # Proximo mes
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return ranges


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Configuração dos argumentos de linha de comando
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="Modo de recolha histórica")
    parser.add_argument("--countries", nargs="+", help="Lista de países (ex: pt es)")
    args = parser.parse_args()

    backfill = args.backfill
    
    # Se fornecer países, usa esses. Se não, usa a lista completa (COUNTRIES)
    countries_to_run = [c.lower() for c in args.countries] if args.countries else COUNTRIES
   
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    today = datetime.now(timezone.utc).date()

    if backfill:
        start_date = datetime.strptime(BACKFILL_START, "%Y-%m-%d").date()
        end_date = yesterday
        print(f"Modo BACKFILL: {start_date} a {end_date}")
    else:
        # Modo diario (corre as 14:30 UTC):
        # - Amanha: dados day-ahead ja devem estar disponiveis
        # - Hoje: re-verificar (pode ter sido incompleto na execucao anterior)
        # - 6 dias anteriores: corrigir atualizacoes tardias dos operadores de rede
        tomorrow = today + timedelta(days=1)
        start_date = today - timedelta(days=6)
        end_date = tomorrow
        print(f"Modo DIARIO: {start_date} a {end_date} (6 dias anteriores + hoje + amanha)")

    # Cache de ficheiros mensais carregados
    monthly_cache = {}
    latest_date = None
    total_countries = len(countries_to_run)

    for i, country in enumerate(countries_to_run, 1):
        print(f"[{i}/{total_countries}] A recolher {country.upper()}...", end=" ")

        if backfill:
            # Em backfill, buscar mes a mes para evitar respostas muito grandes
            month_ranges = get_month_ranges(start_date, end_date)
            country_days = 0
            for m_start, m_end in month_ranges:
                try:
                    api_data = fetch_production(
                        country, m_start.isoformat(), m_end.isoformat()
                    )
                    daily_prod = compute_daily_production(
                        api_data, COUNTRY_TIMEZONES.get(country, "Europe/Berlin")
                    )

                    for date_str, stats in daily_prod.items():
                        year_month = date_str[:7]
                        if year_month not in monthly_cache:
                            monthly_cache[year_month] = load_monthly_file(year_month)
                        monthly_cache[year_month].setdefault(date_str, {})[country] = stats
                        country_days += 1

                        if latest_date is None or date_str > latest_date:
                            latest_date = date_str

                    time.sleep(5)  # Respeitar rate limits
                except Exception as e:
                    print(f"ERRO {country.upper()} ({m_start}-{m_end}): {e}")
                    time.sleep(3)

            print(f"{country_days} dias")
        else:
            # Modo diario: buscar intervalo completo
            try:
                api_data = fetch_production(
                    country, start_date.isoformat(), end_date.isoformat()
                )
                daily_prod = compute_daily_production(
                    api_data, COUNTRY_TIMEZONES.get(country, "Europe/Berlin")
                )

                for date_str, stats in daily_prod.items():
                    year_month = date_str[:7]
                    if year_month not in monthly_cache:
                        monthly_cache[year_month] = load_monthly_file(year_month)
                    # Marcar fonte: dados de hoje sao provisorios, restantes sao finais
                    if date_str == str(today):
                        stats["source"] = "provisional"
                    else:
                        stats["source"] = "real"
                    monthly_cache[year_month].setdefault(date_str, {})[country] = stats

                    if latest_date is None or date_str > latest_date:
                        latest_date = date_str

                days_found = len(daily_prod)
                print(f"OK ({days_found} dias)")
                time.sleep(5)
            except Exception as e:
                print(f"ERRO: {e}")
                time.sleep(1)

    # --- PREVISOES DAY-AHEAD (apenas modo diario) ---
    if not backfill:
        tomorrow = today + timedelta(days=1)
        today_str = str(today)
        tomorrow_str = str(tomorrow)
        yesterday_str = str(yesterday)

        print(f"\nA recolher previsões day-ahead ({today_str}, {tomorrow_str})...")
        for i, country in enumerate(countries_to_run, 1):
            print(f"  [{i}/{total_countries}] Previsão {country.upper()}...", end=" ")
            try:
                tz_name = COUNTRY_TIMEZONES.get(country, "Europe/Berlin")
                daily_fc = compute_daily_forecasts(country, tz_name)

                # Obter dados reais de ontem para proxy das categorias sem previsao
                yesterday_ym = yesterday_str[:7]
                yesterday_entry = (
                    monthly_cache.get(yesterday_ym, {})
                    .get(yesterday_str, {})
                    .get(country, {})
                )
                yesterday_prod = yesterday_entry.get("production", {})

                for date_str, fc_data in daily_fc.items():
                    year_month = date_str[:7]
                    if year_month not in monthly_cache:
                        monthly_cache[year_month] = load_monthly_file(year_month)

                    existing = (
                        monthly_cache[year_month]
                        .get(date_str, {})
                        .get(country)
                    )

                    if existing and existing.get("source") != "forecast":
                        # Dados reais existem - juntar sub-objeto de previsao
                        existing["forecast"] = fc_data
                    else:
                        # Sem dados reais - criar entrada de previsao
                        # Solar/eolica: da previsao; restantes: proxy de ontem
                        production = {}
                        total = 0.0
                        ren_total = 0.0

                        for cat in PRODUCTION_CATS:
                            if cat in fc_data:
                                production[cat] = fc_data[cat]
                            else:
                                production[cat] = yesterday_prod.get(cat, 0.0)
                            total += production[cat]
                            if cat in RENEWABLE_CATS:
                                ren_total += production[cat]

                        # Extras: usar previsao se disponivel (ex: load), senao proxy de ontem
                        for cat in EXTRA_CATS:
                            if cat in fc_data:
                                production[cat] = fc_data[cat]
                            else:
                                production[cat] = yesterday_prod.get(cat, 0.0)

                        ren_share = (
                            round((ren_total / total) * 100, 2)
                            if total > 0 else 0.0
                        )

                        entry = {
                            "ren_share": ren_share,
                            "production": production,
                            "total": round(total, 2),
                            "source": "forecast",
                            "forecast": fc_data,
                        }

                        monthly_cache[year_month].setdefault(
                            date_str, {}
                        )[country] = entry

                        if latest_date is None or date_str > latest_date:
                            latest_date = date_str

                fc_days = len(daily_fc)
                print(f"OK ({fc_days} dias)")
                time.sleep(2.0)  # Pausa extra entre paises
            except Exception as e:
                print(f"ERRO: {e}")
                time.sleep(1)

    # Guardar todos os ficheiros mensais atualizados
    print(f"\nA guardar {len(monthly_cache)} ficheiro(s) mensal(is)...")
    for year_month, data in sorted(monthly_cache.items()):
        save_monthly_file(year_month, data)
        num_days = len(data)
        print(f"  {year_month}.json ({num_days} dias)")

    # Guardar metadata
    if latest_date:
        save_metadata(latest_date)
        print(f"\nMetadata: ultima_data = {latest_date}")

    print("Concluido!")


if __name__ == "__main__":
    main()
