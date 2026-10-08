"""app/config.py: category normalization and the group hierarchy.

normalize_category exists because Claude occasionally returns a category
outside the fixed CATEGORIES list - these are regression tests for real
off-list values observed against the live library this session.
"""
import pytest

from app.config import CATEGORIES, group_of, normalize_category


@pytest.mark.parametrize("raw,expected", [
    ("Sports / Fantasy Football", "Sports / Gaming"),
    ("Entertainment / Comedy", "Comedy / Entertainment"),
    ("Commentary / News", "News / Commentary"),
    ("Movie / Media Recommendation", "Book / Media Recommendation"),
    ("Restaurant / Food Deal", "Finance / Money / Deals"),
    ("Pet / Animal Content", "Other"),
])
def test_normalize_category_folds_off_list_values(raw, expected):
    assert normalize_category(raw) == expected


def test_normalize_category_passes_through_canonical_values():
    for c in CATEGORIES:
        assert normalize_category(c) == c


def test_normalize_category_handles_none():
    assert normalize_category(None) == "Other"


@pytest.mark.parametrize("category,expected_group", [
    ("Tech / AI / Coding", "Tech & Career"),
    ("Career / Job Search", "Tech & Career"),
    ("Finance / Money / Deals", "Money & Business"),
    ("Recipe / Cooking", "Food & Home"),
    ("Sports / Gaming", "Fun & Culture"),
    ("Other", "Other"),
])
def test_group_of_maps_every_canonical_category(category, expected_group):
    assert group_of(category) == expected_group


def test_group_of_unknown_category_falls_back_by_keyword():
    assert group_of("Some Weird Travel Thing") == "Travel & Shopping"


def test_group_of_none_is_other():
    assert group_of(None) == "Other"
