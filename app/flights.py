"""Live flight fares, so a "cheap flights to Tokyo" post, a trip question or a fare you're
waiting on is checked against real prices instead of the creator's say-so.

Google has no public Flights API. Kiwi.com runs an official MCP server (free, no key)
whose results carry prices and booking links, so this calls it through the MCP client
(app/mcp_client.py) and returns a compact summary: the cheapest options, in USD.
"""
import json
import re
from datetime import datetime

_IATA = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def enabled() -> bool:
    return True   # Kiwi.com needs no key


def search(origin: str, destination: str, outbound_date: str, return_date: str | None = None, caller=None) -> dict:
    """The cheapest options for one route and date. Returns {"error": ...} instead of raising,
    so the agent can recover (e.g. fix an airport code)."""
    from app import mcp_client

    origin, destination = origin.strip().upper(), destination.strip().upper()
    if not (_IATA.match(origin) and _IATA.match(destination)):
        return {"error": "Use 3-letter airport codes, e.g. PHL and NRT (or a city code like NYC)."}
    if not _DATE.match(outbound_date or "") or (return_date and not _DATE.match(return_date)):
        return {"error": "Dates must be YYYY-MM-DD."}
    args = {"flyFrom": origin, "flyTo": destination, "departureDate": _kiwi_date(outbound_date),
            "currency": "USD", "sort": "price"}
    if return_date:
        args["returnDate"] = _kiwi_date(return_date)
    out = mcp_client.call("kiwi__search_flight", args, caller=caller, internal=True)
    if "error" in out:
        return {"error": "Flight search failed; use web search instead."}
    try:
        data = json.loads(out["result"])
    except ValueError:
        return {"error": "Flight search returned something unreadable; use web search instead."}
    return summarise(data, origin, destination, outbound_date, return_date)


def _kiwi_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def summarise(data: dict, origin: str, destination: str, outbound: str, ret: str | None) -> dict:
    options = []
    for it in data.get("itineraries") or []:
        if not isinstance(it.get("price"), int | float):
            continue
        legs = [leg for leg in (it.get("outbound"), it.get("inbound")) if isinstance(leg, dict)]
        segs = [s for leg in legs for s in leg.get("segments") or []]
        options.append({
            "price": it["price"],
            "airlines": sorted({s.get("carrierName", "") for s in segs if s.get("carrierName")}),
            "stops": sum(int(leg.get("stops") or 0) for leg in legs),
            "depart": legs[0].get("departureTime") if legs else None,
            "arrive": legs[0].get("arrivalTime") if legs else None,
            "flights": [s.get("flightNumber") for s in segs if s.get("flightNumber")],
            "booking_url": it.get("bookingUrl"),
        })
    options.sort(key=lambda x: x["price"])
    return {"route": f"{origin}-{destination}", "outbound_date": outbound, "return_date": ret,
            "round_trip": bool(ret), "currency": data.get("currency") or "USD",
            "lowest": options[0]["price"] if options else None, "options": options[:4]}
