from src.replay import build_selector_candidates


def test_build_selector_candidates_prefers_selector_hint_then_text_variants() -> None:
    candidates = build_selector_candidates("Search", "#search-input")

    assert candidates[0] == "css=#search-input"
    assert "text=Search" in candidates
    assert "placeholder=Search" in candidates
    assert "label=Search" in candidates
