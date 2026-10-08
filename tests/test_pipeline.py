"""app/pipeline.py: apply_analysis's DB-state transitions, especially the
idempotent Action creation (manually verified against real videos this
session - re-analyzing the same 4 videos twice produced 4 Action rows, not
8; this is that check, formalized)."""
from sqlmodel import select

from app import analyze, pipeline
from app.models import Action, Status, Video


def _make_video(session) -> Video:
    v = Video(tiktok_url="https://www.tiktok.com/@x/video/123", status=Status.TRANSCRIBED)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _actionable_result(**overrides):
    fields = dict(
        category="Tech / AI / Coding", summary="s", tags=[], worth_rewatching_score=3,
        worth_rewatching_reason="r", has_promo_code=False, has_dated_offer=False,
        mentions_link_in_bio=False, key_facts=[], is_actionable=True, action_type="skill",
        action_brief="do the thing",
    )
    fields.update(overrides)
    return analyze.AnalysisResult(input_tokens=1000, output_tokens=200, **fields)


def test_apply_analysis_marks_video_done_and_sets_cost(session):
    v = _make_video(session)
    cost = pipeline.apply_analysis(session, v, _actionable_result(is_actionable=False, action_type=None))
    assert v.status == Status.DONE
    assert cost > 0
    assert v.cost_usd == cost


def test_apply_analysis_creates_one_action_when_actionable(session):
    v = _make_video(session)
    pipeline.apply_analysis(session, v, _actionable_result())
    session.commit()
    actions = session.exec(select(Action).where(Action.video_id == v.id)).all()
    assert len(actions) == 1
    assert actions[0].action_type.value == "skill"


def test_apply_analysis_is_idempotent_on_repeat_analysis(session):
    """The exact regression this session verified by hand: re-analyzing the
    same video twice must not duplicate its Action row."""
    v = _make_video(session)
    pipeline.apply_analysis(session, v, _actionable_result())
    session.commit()
    pipeline.apply_analysis(session, v, _actionable_result())  # simulate a re-run
    session.commit()
    actions = session.exec(select(Action).where(Action.video_id == v.id)).all()
    assert len(actions) == 1


def test_apply_analysis_does_not_create_action_when_not_actionable(session):
    v = _make_video(session)
    pipeline.apply_analysis(session, v, _actionable_result(is_actionable=False, action_type=None))
    session.commit()
    actions = session.exec(select(Action).where(Action.video_id == v.id)).all()
    assert actions == []


def test_apply_analysis_sets_needs_verification_for_stale_promo(session, monkeypatch):
    from datetime import datetime, timedelta
    v = _make_video(session)
    v.saved_date = datetime.utcnow() - timedelta(days=200)
    pipeline.apply_analysis(session, v, _actionable_result(is_actionable=False, action_type=None, has_promo_code=True))
    assert v.needs_verification is True
