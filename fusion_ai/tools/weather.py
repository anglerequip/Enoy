"""Weather from official open-data providers.

- Place names -> coordinates: OpenStreetMap Nominatim (usage policy: at most one
  request per second, identifying User-Agent, cache results; data ODbL).
- Nordic forecasts: SMHI open data, SNOW1g point forecast (CC BY 4.0), plus the
  latest hourly temperature measured at the nearest SMHI station.
- Elsewhere, or when SMHI fails: MET Norway Locationforecast 2.0 (CC BY 4.0).

Values are passed on as the providers report them, with the forecast time,
units and links. Nothing is estimated or filled in: a missing value stays
missing and a failed provider is reported as failed.
"""

from __future__ import annotations

import math
import threading
import time
import urllib.parse
from collections import Counter

from .registry import Tool, ToolContext, ToolError
from .web import get_json

SMHI_FCST = "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/geotype/point/lon/{lon}/lat/{lat}/data.json"
SMHI_OBS = "https://opendata-download-metobs.smhi.se/api/version/1.0/parameter/1/station-set/all/period/latest-hour/data.json"
METNO = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
NOMINATIM = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&accept-language=en&q={q}"

# SMHI weather symbols (27 codes, same table as Wsymb2).
SYMBOLS = {1: "Clear sky", 2: "Nearly clear sky", 3: "Variable cloudiness", 4: "Halfclear sky", 5: "Cloudy sky",
           6: "Overcast", 7: "Fog", 8: "Light rain showers", 9: "Moderate rain showers", 10: "Heavy rain showers",
           11: "Thunderstorm", 12: "Light sleet showers", 13: "Moderate sleet showers", 14: "Heavy sleet showers",
           15: "Light snow showers", 16: "Moderate snow showers", 17: "Heavy snow showers", 18: "Light rain",
           19: "Moderate rain", 20: "Heavy rain", 21: "Thunder", 22: "Light sleet", 23: "Moderate sleet",
           24: "Heavy sleet", 25: "Light snowfall", 26: "Moderate snowfall", 27: "Heavy snowfall"}
UNITS = {"temperature": "°C", "wind_speed": "m/s", "gust": "m/s", "precipitation": "mm per hour",
         "humidity": "%", "pressure": "hPa", "cloud_cover": "oktas (0-8)", "precip_probability": "%"}
MISSING = 9999

_geo_lock = threading.Lock()
_geo_last = [0.0]
_geo_cache: dict[str, dict] = {}
_obs_cache: list = [0.0, None]


def geocode(place: str, cfg: dict, cancel=None) -> dict:
    key = place.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    with _geo_lock:  # Nominatim policy: at most one request per second.
        wait = 1.0 - (time.monotonic() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.monotonic()
        rows = get_json(NOMINATIM.format(q=urllib.parse.quote(place)), cfg, cancel, timeout=10)
    if not rows:
        raise ToolError("failed", f"No place called '{place}' was found. Try adding the country.")
    r = rows[0]
    hit = {"name": r.get("name") or place, "full_name": r.get("display_name", ""),
           "lat": round(float(r["lat"]), 4), "lon": round(float(r["lon"]), 4)}
    _geo_cache[key] = hit
    return hit


def _num(v):
    return None if v is None or v == MISSING else v


def _smhi(loc: dict, cfg: dict, cancel, hours: int, days: int) -> dict:
    data = get_json(SMHI_FCST.format(lat=loc["lat"], lon=loc["lon"]), cfg, cancel, timeout=15)
    series = data.get("timeSeries") or []
    if not series:
        raise ToolError("failed", "SMHI returned no forecast values for this place.")
    rows = []
    for item in series:
        d = item.get("data", {})
        sym = _num(d.get("symbol_code"))
        rows.append({
            "time_utc": item["time"], "temperature": _num(d.get("air_temperature")),
            "wind_speed": _num(d.get("wind_speed")), "gust": _num(d.get("wind_speed_of_gust")),
            "wind_from_deg": _num(d.get("wind_from_direction")), "humidity": _num(d.get("relative_humidity")),
            "pressure": _num(d.get("air_pressure_at_mean_sea_level")),
            "cloud_cover": _num(d.get("cloud_area_fraction")),
            "precipitation": _num(d.get("precipitation_amount_mean")),
            "precip_probability": _num(d.get("probability_of_precipitation")),
            "conditions": SYMBOLS.get(int(sym)) if sym else None,
        })
    obs = _nearest_observation(loc, cfg, cancel)
    link = SMHI_FCST.format(lat=loc["lat"], lon=loc["lon"])  # The data itself; SMHI has no stable page per point.
    return {"provider": "SMHI (Swedish Meteorological and Hydrological Institute), SNOW1g point forecast",
            "forecast_issued_utc": data.get("referenceTime") or data.get("createdTime"),
            "grid_point": data.get("geometry", {}).get("coordinates"),
            "rows": rows, "observation": obs, "link": link,
            "api": SMHI_FCST.format(lat=loc["lat"], lon=loc["lon"]), "license": "CC BY 4.0, source: SMHI"}


def _nearest_observation(loc: dict, cfg: dict, cancel) -> dict | None:
    """Latest hourly air temperature measured at the closest SMHI station (within 60 km)."""
    try:
        if _obs_cache[1] is None or time.time() - _obs_cache[0] > 600:
            _obs_cache[:] = [time.time(), get_json(SMHI_OBS, cfg, cancel, timeout=15)]
        stations = _obs_cache[1].get("station", [])
    except ToolError:
        return None
    best, best_km = None, 1e9
    for s in stations:
        if not s.get("value"):
            continue
        km = _km(loc["lat"], loc["lon"], s["latitude"], s["longitude"])
        if km < best_km:
            best, best_km = s, km
    if not best or best_km > 60:
        return None
    v = best["value"][-1]
    try:
        temp = float(v["value"])
    except (TypeError, ValueError):
        return None
    return {"station": best["name"], "distance_km": round(best_km, 1), "temperature": temp,
            "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v["date"] / 1000)),
            "quality": {"G": "checked", "Y": "suspect or aggregated"}.get(v.get("quality"), v.get("quality"))}


def _km(a_lat, a_lon, b_lat, b_lon) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _metno(loc: dict, cfg: dict, cancel, hours: int, days: int) -> dict:
    data = get_json(METNO.format(lat=loc["lat"], lon=loc["lon"]), cfg, cancel, timeout=15)
    props = data.get("properties", {})
    rows = []
    for item in props.get("timeseries", []):
        det = item.get("data", {}).get("instant", {}).get("details", {})
        nxt = item.get("data", {}).get("next_1_hours") or {}
        code = (nxt.get("summary") or {}).get("symbol_code")
        rows.append({
            "time_utc": item["time"], "temperature": det.get("air_temperature"),
            "wind_speed": det.get("wind_speed"), "gust": None, "wind_from_deg": det.get("wind_from_direction"),
            "humidity": det.get("relative_humidity"), "pressure": det.get("air_pressure_at_sea_level"),
            "cloud_cover": None if det.get("cloud_area_fraction") is None else round(det["cloud_area_fraction"] * 8 / 100, 1),
            "precipitation": (nxt.get("details") or {}).get("precipitation_amount"), "precip_probability": None,
            "conditions": code.split("_")[0].replace("partlycloudy", "partly cloudy") if code else None,
        })
    if not rows:
        raise ToolError("failed", "MET Norway returned no forecast values for this place.")
    return {"provider": "MET Norway (Norwegian Meteorological Institute), Locationforecast 2.0",
            "forecast_issued_utc": props.get("meta", {}).get("updated_at"), "grid_point": None,
            "rows": rows, "observation": None,
            "link": METNO.format(lat=loc["lat"], lon=loc["lon"]),
            "api": METNO.format(lat=loc["lat"], lon=loc["lon"]), "license": "CC BY 4.0, source: MET Norway"}


def _shape(raw: dict, hours: int, days: int) -> dict:
    now = time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime())
    rows = [r for r in raw["rows"] if r["time_utc"] >= now] or raw["rows"]
    daily: dict[str, dict] = {}
    for r in raw["rows"]:
        day = r["time_utc"][:10]
        d = daily.setdefault(day, {"date": day, "min_temp": None, "max_temp": None, "precipitation_mm": 0.0,
                                   "max_wind": None, "_cond": Counter(), "_n": 0})
        t = r["temperature"]
        if t is not None:
            d["min_temp"] = t if d["min_temp"] is None else min(d["min_temp"], t)
            d["max_temp"] = t if d["max_temp"] is None else max(d["max_temp"], t)
        if r["precipitation"] is not None:
            d["precipitation_mm"] += r["precipitation"]
        if r["wind_speed"] is not None:
            d["max_wind"] = r["wind_speed"] if d["max_wind"] is None else max(d["max_wind"], r["wind_speed"])
        if r["conditions"]:
            d["_cond"][r["conditions"]] += 1
        d["_n"] += 1
    days_out = []
    for d in list(daily.values())[:days]:
        d["precipitation_mm"] = round(d["precipitation_mm"], 1)
        d["most_common_conditions"] = d["_cond"].most_common(1)[0][0] if d["_cond"] else None
        d["hours_covered"] = d.pop("_n")
        d.pop("_cond")
        days_out.append(d)
    return {"forecast_now": rows[0], "hourly": rows[1:1 + hours], "daily_utc": days_out}


def _weather(args: dict, ctx: ToolContext) -> dict:
    hours = int(args.get("hours", 12))
    days = int(args.get("days", 3))
    loc = geocode(args["location"], ctx.cfg, ctx.cancel)
    errors = []
    raw = None
    # SMHI covers the Nordic area; MET Norway covers the world.
    nordic = 52 <= loc["lat"] <= 72 and -5 <= loc["lon"] <= 35
    for provider in ((_smhi, _metno) if nordic else (_metno,)):
        if ctx.cancel.is_set():
            raise ToolError("cancelled", "Stopped.")
        try:
            raw = provider(loc, ctx.cfg, ctx.cancel, hours, days)
            break
        except ToolError as exc:
            errors.append(f"{provider.__name__.strip('_').upper()}: {exc}")
    if raw is None:
        raise ToolError("unavailable", "No weather provider answered. " + " | ".join(errors))
    shaped = _shape(raw, hours, days)
    out = {"location": loc, "provider": raw["provider"], "forecast_issued_utc": raw["forecast_issued_utc"],
           "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "units": UNITS, "times_are": "UTC (Sweden is UTC+1 in winter, UTC+2 in summer)",
           "measured_now": raw["observation"],
           "note": "forecast_now is the model forecast for the current hour, not a measurement; "
                   "measured_now (when present) is a real station reading.",
           **shaped}
    if errors:
        out["fallback_used"] = "; ".join(errors)
    out["_sources"] = [{"title": raw["provider"], "url": raw["link"], "provider": raw["license"],
                        "retrieved": out["retrieved_utc"]},
                       {"title": "Place lookup: OpenStreetMap Nominatim (ODbL)",
                        "url": "https://www.openstreetmap.org/copyright", "retrieved": out["retrieved_utc"]}]
    return out


WEATHER = Tool(
    name="get_weather",
    description="Current conditions and forecast for a named place, from official weather services (SMHI for "
                "the Nordic countries, MET Norway elsewhere). Use for any question about weather, temperature, "
                "rain, snow or wind.",
    parameters={"type": "object", "properties": {
        "location": {"type": "string", "description": "Place name, e.g. 'Trollhättan' or 'Paris, France'.",
                     "maxLength": 200},
        "hours": {"type": "integer", "description": "Hourly forecast rows to return (default 12).",
                  "minimum": 0, "maximum": 48},
        "days": {"type": "integer", "description": "Daily summaries to return (default 3).",
                 "minimum": 1, "maximum": 10}},
        "required": ["location"]},
    run=_weather,
    output="location, provider, forecast issue time, units, forecast for this hour, hourly rows, daily summaries, "
           "nearest station measurement (SMHI only)",
    needs_internet=True, permission="web", timeout_s=30, cache_ttl_s=600, attribution=True,
)
