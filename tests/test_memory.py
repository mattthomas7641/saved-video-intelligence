"""app/memory.py: embedding past reports and recalling related ones, with a fake
embedding client (deterministic vectors, no network)."""
import json
from types import SimpleNamespace

from sqlmodel import select

from app import memory, tracing
from app.config import save_gemini_key
from app.models import Action, ActionStatus, ActionType, Memory, Run, Status, Video

TOPICS = ["agent harness", "pizza", "rust kernel"]


class FakeEmbed:
    """Vector = which of TOPICS the text mentions (plus a tiny constant), so related texts are close."""
    def __init__(self):
        self.calls = []
        self.models = SimpleNamespace(embed_content=self._embed)

    def _embed(self, model, contents, config):
        self.calls.append(contents)
        vecs = []
        for c in contents:
            low = c.lower()
            vecs.append(SimpleNamespace(values=[1.0 if t in low else 0.0 for t in TOPICS] + [0.05]))
        return SimpleNamespace(embeddings=vecs)


def _report(session, headline, summary, verdict="worth_it", status=ActionStatus.DRAFTED, fit=None):
    v = Video(tiktok_url=f"https://x.test/{headline}", status=Status.DONE, source="telegram", summary=summary)
    session.add(v)
    session.commit()
    result = {"headline": headline, "telegram_text": "t", "report_markdown": "r",
              "project_fit": [{"project": fit, "fit": "worth_it"}] if fit else []}
    a = Action(video_id=v.id, action_type=ActionType.SKILL, status=status, verdict=verdict, result=json.dumps(result))
    session.add(a)
    session.commit()
    return a, v


def test_remember_and_recall_related(session):
    client = FakeEmbed()
    a1, v1 = _report(session, "Checkpoint your agent harness", "agent harness crash recovery", fit="durable-agent-harness")
    a2, v2 = _report(session, "Best pizza in Philly", "pizza spot")
    for a, v in ((a1, v1), (a2, v2)):
        memory.remember(session, a, v, json.loads(a.result), client=client)
    related = memory.recall(session, "a new agent harness pattern", client=client)
    assert [r["action_id"] for r in related] == [a1.id]  # pizza is below the similarity bar
    assert related[0]["fits"] == ["durable-agent-harness"]
    block = memory.context_block(related)
    assert "Checkpoint your agent harness" in block and "durable-agent-harness" in block
    assert client.calls[-1][0].startswith("task: search result | query:")


def test_recall_excludes_the_item_itself_and_handles_empty(session):
    client = FakeEmbed()
    assert memory.recall(session, "anything", client=client) == []
    a, v = _report(session, "Rust kernel tips", "rust kernel")
    memory.remember(session, a, v, json.loads(a.result), client=client)
    assert memory.recall(session, "rust kernel", exclude_action_id=a.id, client=client) == []


def test_remember_replaces_and_costs_are_traced(session):
    client = FakeEmbed()
    a, v = _report(session, "Rust kernel tips", "rust kernel")
    with tracing.run("memory", action_id=a.id):
        memory.remember(session, a, v, json.loads(a.result), client=client)
        memory.remember(session, a, v, json.loads(a.result), client=client)
    assert len(session.exec(select(Memory)).all()) == 1
    run = session.exec(select(Run)).one()
    assert run.cost_usd > 0 and run.model == memory.MODEL


def test_backfill_embeds_only_missing_reports(session):
    client = FakeEmbed()
    a1, v1 = _report(session, "One", "agent harness")
    _report(session, "Two", "pizza", status=ActionStatus.DONE)
    memory.remember(session, a1, v1, json.loads(a1.result), client=client)
    assert memory.backfill(session, client=client) == 1
    assert memory.backfill(session, client=client) == 0


def test_pipeline_uses_memory_and_stores_related(session, monkeypatch):
    from app import agent, gemini_client, intake, pipeline, prefetch
    from app.analyze import AnalysisResult
    save_gemini_key("AIza-test")
    client = FakeEmbed()
    monkeypatch.setattr(memory, "_client", lambda: client)
    old, old_v = _report(session, "Checkpoint your agent harness", "agent harness crash recovery")
    memory.remember(session, old, old_v, json.loads(old.result), client=client)
    analysis = AnalysisResult(category="Tech / AI / Coding", summary="Another agent harness trick", tags=[],
                              worth_rewatching_score=3, worth_rewatching_reason="r", has_promo_code=False,
                              has_dated_offer=False, mentions_link_in_bio=False, key_facts=[], is_actionable=True,
                              action_type="skill", action_brief="agent harness retries")
    analysis.cost_usd = 0.0
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: analysis)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    seen = {}

    def fake_run(*a, **k):
        seen["memory"] = k["context"].memory
        return agent.Findings(verdict="maybe", headline="Use retries", telegram_text="t", report_markdown="r")
    monkeypatch.setattr(agent, "run", fake_run)
    vid = intake.ingest_shared_text("agent harness retries video")
    action = pipeline.process_shared_video(session, session.get(Video, vid))
    assert "Checkpoint your agent harness" in seen["memory"]
    assert json.loads(action.result)["related"][0]["action_id"] == old.id
    assert session.exec(select(Memory).where(Memory.action_id == action.id)).first() is not None


def test_memory_failure_never_fails_the_item(session, monkeypatch):
    from app import agent, gemini_client, intake, pipeline, prefetch
    from app.analyze import AnalysisResult
    save_gemini_key("AIza-test")

    def boom():
        raise RuntimeError("embedding service down")
    monkeypatch.setattr(memory, "_client", boom)
    analysis = AnalysisResult(category="Recipe / Cooking", summary="pizza", tags=[], worth_rewatching_score=3,
                              worth_rewatching_reason="r", has_promo_code=False, has_dated_offer=False,
                              mentions_link_in_bio=False, key_facts=[], is_actionable=True, action_type="place",
                              action_brief="pizza")
    analysis.cost_usd = 0.0
    monkeypatch.setattr(gemini_client, "analyze_video", lambda v: analysis)
    monkeypatch.setattr(prefetch, "gather", lambda v: {"repos": [], "pages": []})
    monkeypatch.setattr(agent, "run", lambda *a, **k: agent.Findings(verdict="maybe", headline="h",
                                                                      telegram_text="t", report_markdown="r"))
    vid = intake.ingest_shared_text("pizza place in fishtown")
    assert pipeline.process_shared_video(session, session.get(Video, vid)).status == ActionStatus.DRAFTED


def test_related_survives_report_normalising_and_renders(session):
    from fastapi.testclient import TestClient

    from app.agent import load_report
    from app.main import app
    raw = {"headline": "h", "telegram_text": "t", "report_markdown": "r",
           "related": [{"action_id": 7, "headline": "Older thing", "date": "Oct 1"}, {"action_id": "x"}]}
    assert load_report(json.dumps(raw))["related"] == [{"action_id": 7, "headline": "Older thing", "date": "Oct 1"}]
    a, _ = _report(session, "New thing", "s")
    a.result = json.dumps({**raw, "headline": "New thing"})
    session.add(a)
    session.commit()
    assert "Related things you sent" in TestClient(app).get("/").text

