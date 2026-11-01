"""The example renderer must not silently drop credited authors."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'tools'))
    return importlib.import_module('attribution_demo')


def test_preferred_text_missing_second_author_is_completed(demo):
    profile = {
        'preferred_text': 'Analytics by Example Lab',
        'authors': [{'name': 'Example Lab'}, {'name': 'Vision Workshop'}],
    }
    assert demo.overlay_label(profile) == 'ANALYTICS BY EXAMPLE LAB | VISION WORKSHOP'
    original, annotated = demo.frame(profile)
    assert original != annotated


def test_author_name_must_not_be_accepted_as_part_of_a_different_word(demo):
    profile = {'preferred_text': 'Analytics by Laboratory', 'authors': [{'name': 'Lab'}]}
    assert demo.overlay_label(profile) == 'ANALYTICS BY LABORATORY | LAB'


def test_demo_rejects_labels_its_font_cannot_represent_or_fit(demo):
    with pytest.raises(ValueError, match='ASCII'):
        demo.overlay_label({'authors': [{'name': 'Автор'}]})
    with pytest.raises(ValueError, match='maximum 50'):
        demo.overlay_label({'authors': [{'name': 'A' * 45}]})
