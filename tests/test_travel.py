"""Flights and trips: the travel report fields, trip steps (calendar, check-in reminder,
status check), fare watches and the live fare lookup. GitHub/SerpApi/Gemini are faked."""
import json
from datetime import datetime, timedelta

from app import agent, flights, quests
from app.models import Action, ActionStatus, ActionType, Status, Task, Video


def _soon(days, hour=8):
    return (datetime.now() + timedelta(days=days)).replace(hour=hour, minute=5).strftime("%Y-%m-%dT%H:%M")


TRIP = {"destination_city": "Austin", "confirmation": "QX7Z2P", "hotel": "Hotel Van Zandt",
        "segments": [{"flight": "AA 1234", "from": "PHL", "to": "AUS", "depart": _soon(5), "arrive": _soon(5, 11),
                      "depart_tz": "America/New_York", "arrive_tz": "America/Chicago", "terminal": "B"},
                     {"flight": "AA 99", "from": "AUS", "to": "PHL", "depart": _soon(9, 17),
                      "depart_tz": "America/Chicago"}]}


def _travel(session, report):
    v = Video(tiktok_url=f"image:{abs(hash(json.dumps(report)))}", status=Status.DONE, source="telegram")
    session.add(v)
    session.commit()
    a = Action(video_id=v.id, action_type=ActionType.TRAVEL, status=ActionStatus.DRAFTED, verdict="worth_it",
               result=json.dumps({"headline": "Austin trip", "telegram_text": "t", "report_markdown": "r", **report}))
    session.add(a)
    session.commit()
    return a


def test_report_keeps_trip_and_fare_check():
    f = agent._findings_from({"verdict": "worth_it", "headline": "h", "telegram_text": "t", "report_markdown": "r",
                              "trip": {**TRIP, "segments": TRIP["segments"] + [{"from": "X"}]},
                              "fare_check": {"origin": "phl", "destination": "nrt", "verdict": "Real deal",
                                             "watch_below": "cheap", "current_lowest": 612}}, 0.0)
    assert len(f.trip["segments"]) == 2 and f.trip["confirmation"] == "QX7Z2P"
    assert f.fare_check["origin"] == "PHL" and f.fare_check["watch_below"] is None and f.fare_check["current_lowest"] == 612
    assert agent.load_report(f.to_json())["trip"]["destination_city"] == "Austin"


def test_trip_becomes_calendar_step_reminders_and_status_checks(session):
    a = _travel(session, {"trip": TRIP})
    tasks = quests.plan(session, a, json.loads(a.result))
    kinds = sorted(t.kind for t in tasks)
    assert kinds == ["flight_status", "flight_status", "flights_calendar", "reminder", "reminder"]
    reminder = [t for t in tasks if t.kind == "reminder"][0]
    dep_utc = datetime.fromisoformat(TRIP["segments"][0]["depart"]).replace(
        tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).astimezone(
        __import__("zoneinfo").ZoneInfo("UTC")).replace(tzinfo=None)
    assert reminder.run_at == dep_utc - timedelta(hours=24)
    assert "QX7Z2P" in json.loads(reminder.payload)["message"]
    cal = [t for t in tasks if t.kind == "flights_calendar"][0]
    message, url = quests.approve(cal.id)
    session.expire_all()
    done = session.get(Task, cal.id)
    events = json.loads(done.result)["events"]
    assert done.status == "done" and len(events) == 2 and "AA+1234" in events[0]["google_url"]
    ics = quests.calendar_ics(done)
    assert ics.count(b"BEGIN:VEVENT") == 2


def test_past_flights_get_no_reminders(session):
    old = {**TRIP, "segments": [{**TRIP["segments"][0], "depart": "2020-01-01T08:00"}]}
    a = _travel(session, {"trip": old})
    assert [t.kind for t in quests.plan(session, a, json.loads(a.result))] == ["flights_calendar"]


def test_reminder_sends_and_is_free(session, monkeypatch):
    a = _travel(session, {})
    session.add(Task(action_id=a.id, kind="reminder", status="scheduled", run_at=datetime.utcnow() - timedelta(minutes=1),
                     title="Check-in reminder", payload=json.dumps({"title": "✈️ Check in now: AA 1", "message": "m"})))
    session.commit()
    sent = []
    monkeypatch.setattr(quests, "_notify", lambda title, message, click=None: sent.append(title))
    assert quests.run_due() == 1 and sent == ["✈️ Check in now: AA 1"]


def test_fare_watch_reschedules_then_notifies_on_drop(session, monkeypatch):
    out = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
    a = _travel(session, {"fare_check": {"origin": "PHL", "destination": "NRT", "outbound_date": out,
                                         "verdict": "Wait", "watch_below": 700.0}})
    [t] = quests.plan(session, a, json.loads(a.result))
    assert t.kind == "fare_watch" and t.status == "scheduled"
    prices = iter([850, 640])
    sent = []
    monkeypatch.setattr(quests, "_notify", lambda title, message, click=None: sent.append(title))
    for _ in range(2):   # the first check is above target: it waits and looks again
        session.expire_all()
        row = session.get(Task, t.id)
        row.status = "running"
        session.add(row)
        session.commit()
        quests._fare_watch(t.id, search=lambda *args: {"lowest": next(prices)})
        session.expire_all()
        if session.get(Task, t.id).status == "scheduled":
            assert session.get(Task, t.id).run_at > datetime.utcnow()
    session.expire_all()
    row = session.get(Task, t.id)
    assert row.status == "done" and json.loads(row.result)["hit"] and json.loads(row.result)["lowest"] == 640
    assert sent == ["💸 Fare drop: PHL→NRT now $640"]
    assert [h["lowest"] for h in json.loads(row.result)["history"]] == [850, 640]


def test_flight_search_goes_through_kiwi_and_summarises():
    seen = {}
    kiwi = {"currency": "USD", "itineraries": [
        {"price": 702.0, "bookingUrl": "https://kiwi.com/u/a",
         "outbound": {"stops": 0, "departureTime": "2027-03-02T11:00:00", "arrivalTime": "2027-03-03T15:00:00",
                      "segments": [{"carrierName": "ANA", "flightNumber": "NH11"}]}, "inbound": None},
        {"price": 655.0, "bookingUrl": "https://kiwi.com/u/b",
         "outbound": {"stops": 1, "segments": [{"carrierName": "United", "flightNumber": "UA1"},
                                                {"carrierName": "ANA", "flightNumber": "NH2"}]},
         "inbound": {"stops": 0, "segments": [{"carrierName": "ANA", "flightNumber": "NH3"}]}},
        {"priceFormatted": "no price"}]}

    def caller(url, tool, args):
        seen.update(url=url, tool=tool, args=args)
        return json.dumps(kiwi)

    out = flights.search("phl", "nrt", "2027-03-02", "2027-03-12", caller=caller)
    assert seen["tool"] == "search-flight" and seen["args"]["departureDate"] == "02/03/2027"
    assert seen["args"]["returnDate"] == "12/03/2027" and seen["args"]["currency"] == "USD"
    assert out["lowest"] == 655 and out["options"][0]["stops"] == 1 and out["round_trip"]
    assert out["options"][0]["booking_url"] == "https://kiwi.com/u/b" and out["options"][0]["airlines"] == ["ANA", "United"]
    assert "error" in flights.search("Philly", "NRT", "2027-03-02", caller=caller)
    assert "error" in flights.search("PHL", "NRT", "2027-03-02",
                                     caller=lambda *a: (_ for _ in ()).throw(ConnectionError("x")))


def test_kiwi_is_not_offered_to_the_model_raw():
    from app import mcp_client
    assert "error" in mcp_client.call("kiwi__search_flight", {"flyFrom": "PHL"}, caller=lambda *a: "{}")


def test_flight_tool_offered_for_everyday_content_not_tech():
    names = lambda tools: [t["name"] for t in tools]  # noqa: E731
    assert "flight_prices" in names(agent.client_tools(False))
    assert "flight_prices" not in names(agent.client_tools(True))


def test_trip_report_page_and_saved_places(session):
    from fastapi.testclient import TestClient

    from app.main import app

    pv = Video(tiktok_url="https://x.test/taco", status=Status.DONE, source="telegram")
    session.add(pv)
    session.commit()
    session.add(Action(video_id=pv.id, action_type=ActionType.PLACE, status=ActionStatus.DRAFTED, verdict="worth_it",
                       result=json.dumps({"headline": "Tacos", "telegram_text": "t", "report_markdown": "r",
                                          "place": {"name": "Veracruz", "city": "Austin"}})))
    session.commit()
    a = _travel(session, {"trip": TRIP})
    quests.plan(session, a, json.loads(a.result))
    client = TestClient(app)
    assert "Add your flights to the calendar" in client.get("/").text
    page = client.get(f"/q/{a.id}").text
    assert "Your trip to Austin" in page and "QX7Z2P" in page and "Veracruz" in page
    assert "PHL → AUS" in page and "Add 2 flights" in page
