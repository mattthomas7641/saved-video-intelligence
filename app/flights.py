"""Live flight fares, so a "cheap flights to Tokyo" video or a fare you're waiting on
is checked against real prices instead of the creator's say-so.

Google has no public Flights API; SerpApi reads Google Flights results (free plan:
250 searches a month, enough for checking deals and a few price watches). Without a
key, the agent falls back to Google Search, which finds fares less reliably.
"""
import logging
import re

import httpx

log = logging.getLogger(__name__)

URL = "https://serpapi.com/search.json"
_IATA = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def enabled() -> bool:
    from app.config import get_quest_settings

    return bool(get_quest_settings()["serpapi_key"])


def search(origin: str, destination: str, outbound_date: str, return_date: str | None = None,
           http: httpx.Client | None = None) -> dict:
    """The cheapest options and Google's price insight for one route and date. Returns
    {"error": ...} instead of raising, so the agent can recover (e.g. fix an airport code)."""
    from app.config import get_quest_settings

    origin, destination = origin.strip().upper(), destination.strip().upper()
    if not (_IATA.match(origin) and _IATA.match(destination)):
        return {"error": "Use 3-letter airport codes, e.g. PHL and NRT (or a city code like NYC)."}
    if not _DATE.match(outbound_date or "") or (return_date and not _DATE.match(return_date)):
        return {"error": "Dates must be YYYY-MM-DD."}
    key = get_quest_settings()["serpapi_key"]
    if not key:
        return {"error": "No flight price source set up; use web search instead."}
    params = {"engine": "google_flights", "departure_id": origin, "arrival_id": destination,
              "outbound_date": outbound_date, "currency": "USD", "hl": "en", "api_key": key,
              "type": "1" if return_date else "2"}
    if return_date:
        params["return_date"] = return_date
    client = http or httpx.Client(timeout=40)
    try:
        r = client.get(URL, params=params)
        if r.status_code in (401, 403):
            return {"error": "The flight price key was rejected; use web search instead."}
        if r.status_code == 429:
            return {"error": "This month's flight price lookups are used up; use web search instead."}
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Flight search failed: %s", type(e).__name__)  # the URL holds the key: never log it
        return {"error": "Flight price lookup failed; use web search instead."}
    finally:
        if http is None:
            client.close()
    return summarise(data, origin, destination, outbound_date, return_date)


def summarise(data: dict, origin: str, destination: str, outbound: str, ret: str | None) -> dict:
    options = []
    for o in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        legs = o.get("flights") or []
        if not legs or not isinstance(o.get("price"), int | float):
            continue
        options.append({
            "price": o["price"],
            "airlines": sorted({leg.get("airline", "") for leg in legs if leg.get("airline")}),
            "stops": max(len(legs) - 1, 0),
            "depart": (legs[0].get("departure_airport") or {}).get("time"),
            "arrive": (legs[-1].get("arrival_airport") or {}).get("time"),
            "minutes": o.get("total_duration"),
            "flights": [leg.get("flight_number") for leg in legs if leg.get("flight_number")],
        })
    options.sort(key=lambda x: x["price"])
    insight = data.get("price_insights") or {}
    typical = insight.get("typical_price_range") or []
    return {
        "route": f"{origin}-{destination}", "outbound_date": outbound, "return_date": ret,
        "round_trip": bool(ret), "lowest": options[0]["price"] if options else insight.get("lowest_price"),
        "price_level": insight.get("price_level"),
        "typical_range": typical if len(typical) == 2 else None,
        "options": options[:4],
        "google_flights": (data.get("search_metadata") or {}).get("google_flights_url"),
    }
