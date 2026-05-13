from pathlib import Path
from tempfile import TemporaryDirectory

from src.browser_listener import (
    BrowserEventBatchIn,
    BrowserEventStore,
    plan_listener_guided_frames,
)


def test_browser_listener_store_marks_navigation_and_large_scroll_as_key_candidates() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-1",
            "events": [
                {
                    "event_type": "navigation",
                    "page_url": "https://example.com/list",
                },
                {
                    "event_type": "scroll",
                    "scroll_y": 1200,
                    "delta_y": 640,
                },
                {
                    "event_type": "focus",
                },
            ],
        }
    )

    accepted = store.ingest(batch)

    assert accepted[0].is_key_candidate is True
    assert accepted[1].is_key_candidate is True
    assert accepted[2].is_key_candidate is False


def test_browser_listener_store_returns_newest_events_first() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-2",
            "events": [
                {"event_type": "click", "target_text": "First"},
                {"event_type": "click", "target_text": "Second"},
                {"event_type": "click", "target_text": "Third"},
            ],
        }
    )
    store.ingest(batch)

    events = store.list_events(limit=2)

    assert len(events) == 2
    assert events[0].target_text == "Third"
    assert events[1].target_text == "Second"


def test_browser_listener_store_trims_long_strings() -> None:
    store = BrowserEventStore(max_events=5)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-3",
            "events": [
                {
                    "event_type": "input",
                    "input_value": "x" * 700,
                    "target_selector": ".field",
                }
            ],
        }
    )
    accepted = store.ingest(batch)

    assert accepted[0].input_value is not None
    assert len(accepted[0].input_value) <= 500


def test_browser_listener_store_persists_screenshot_and_selects_analysis_candidates() -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=10, artifact_root=Path(temp_dir))
        batch = BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "session-shot",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Preview",
                        "page_url": "https://example.com/report",
                        "screenshot_data_url": png_data_url,
                    }
                ],
            }
        )

        accepted = store.ingest(batch)
        session_id, selected = store.select_analysis_candidates(session_id="session-shot", limit=5)

        assert accepted[0].screenshot_path is not None
        assert Path(accepted[0].screenshot_path).exists()
        assert session_id == "session-shot"
        assert len(selected) == 1
        assert selected[0].target_text == "Preview"


def test_plan_listener_guided_frames_uses_event_positions_and_neighbors() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-guided",
            "events": [
                {
                    "event_type": "click",
                    "target_text": "Open",
                    "client_timestamp_ms": 1000,
                    "key_candidate": True,
                },
                {
                    "event_type": "change",
                    "target_text": "Industry",
                    "client_timestamp_ms": 6000,
                    "key_candidate": True,
                },
                {
                    "event_type": "scroll",
                    "scroll_y": 900,
                    "delta_y": 600,
                    "client_timestamp_ms": 10000,
                    "key_candidate": True,
                },
            ],
        }
    )
    accepted = store.ingest(batch)

    guided = plan_listener_guided_frames(accepted, start_second=0.0, end_second=20.0, max_frames=6)

    assert 1 <= len(guided) <= 6
    assert all(0.0 <= item.timestamp_second <= 20.0 for item in guided)
    assert any("target=Open" in item.hint for item in guided)
    assert any("target=Industry" in item.hint for item in guided)
    assert guided == sorted(guided, key=lambda item: item.timestamp_second)
