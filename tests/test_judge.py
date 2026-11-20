"""The pairwise judge (app/judge.py): order-swapped, position bias scored as a tie."""
from app import judge


def _asker(prefers):
    """A fake judge that prefers whichever answer contains `prefers` (or always says A)."""
    def ask(client, request, first, second):
        if prefers is None:
            return "A", 0.001                      # position-biased: always the first one
        return ("A" if prefers in first else "B" if prefers in second else "TIE"), 0.001
    return ask


def test_consistent_preference_wins():
    assert judge.pairwise("r", "good plan", "meh", ask=_asker("good"))[0] == "x"
    assert judge.pairwise("r", "meh", "good plan", ask=_asker("good"))[0] == "y"


def test_position_bias_is_a_tie():
    verdict, cost = judge.pairwise("r", "one", "two", ask=_asker(None))
    assert verdict == "tie" and cost == 0.002


def test_compare_tallies():
    pairs = [("r1", "good", "meh"), ("r2", "meh", "good"), ("r3", "x", "y")]
    out = judge.compare(pairs, ("single", "team"), ask=_asker("good"))
    assert out["wins"] == {"single": 1, "team": 1, "tie": 1} and out["pairs"] == 3
