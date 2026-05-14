import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import src.webapp as webapp_module
from src.browser_listener import BrowserEventBatchIn, BrowserEventStore
from src.dsl import ReplayBundle


class DummyRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def test_browser_listener_analyze_route_uses_candidate_screenshots() -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir))
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "api-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Open report",
                            "page_url": "https://example.com/report",
                            "screenshot_data_url": png_data_url,
                        }
                    ],
                }
            )
        )

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_build_replay_plan(frame_paths, config, user_request=None, site_url=None, frame_hints=None):
            assert len(frame_paths) == 1
            assert frame_hints is not None
            assert "click" in frame_hints[0]
            return SimpleNamespace(
                sop=["Open the report from the current page."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Open the report from the current page."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Open report",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_browser_listener_session(
                    DummyRequest({"session_id": "api-session", "max_events": 4})
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "api-session" in payload
    assert "Open report" in payload


def test_video_analyze_route_requires_listener_guidance_and_uses_listener_session() -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_get_video_metadata = webapp_module.get_video_metadata
    original_extract_frames = webapp_module.extract_frames
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir) / "listener")
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "video-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Search",
                            "client_timestamp_ms": 1000,
                            "key_candidate": True,
                        },
                        {
                            "event_type": "change",
                            "target_text": "Province",
                            "client_timestamp_ms": 5000,
                            "key_candidate": True,
                        },
                    ],
                }
            )
        )

        fake_video = Path(temp_dir) / "fake.mp4"
        fake_video.write_bytes(b"not-a-real-video")
        generated_frames = []

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_get_video_metadata(path: Path):
            return SimpleNamespace(duration_seconds=20.0, fps=10.0, width=100, height=100)

        def fake_extract_frames(video_path, timestamps, job_id):
            paths = []
            for index, _ in enumerate(timestamps, start=1):
                frame_path = Path(temp_dir) / f"{job_id}_{index:02d}.png"
                frame_path.write_bytes(b"frame")
                paths.append(frame_path)
            generated_frames[:] = paths
            return paths

        def fake_build_replay_plan(frame_paths, config, user_request=None, site_url=None, frame_hints=None):
            assert len(frame_paths) == len(generated_frames)
            assert frame_hints is not None
            assert any("target=Search" in hint for hint in frame_hints)
            return SimpleNamespace(
                sop=["Use listener-guided frames."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Use listener-guided frames."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Search",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.get_video_metadata = fake_get_video_metadata
        webapp_module.extract_frames = fake_extract_frames
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_video(
                    DummyRequest(
                        {
                            "video_path": str(fake_video),
                            "listener_session_id": "video-session",
                            "start_second": 0.0,
                            "end_second": 20.0,
                            "max_frames": 6,
                        }
                    )
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.get_video_metadata = original_get_video_metadata
            webapp_module.extract_frames = original_extract_frames
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "\"listener_guided\":true" in payload
    assert "video-session" in payload


def test_video_analyze_route_uses_session_recording_when_video_path_missing() -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_get_video_metadata = webapp_module.get_video_metadata
    original_extract_frames = webapp_module.extract_frames
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir) / "listener")
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "recording-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Search",
                            "client_timestamp_ms": 1000,
                            "key_candidate": True,
                        }
                    ],
                }
            )
        )
        recording_path = Path(temp_dir) / "recording.webm"
        recording_path.write_bytes(b"webm")
        store.set_session_recording(
            "recording-session",
            recording_path=str(recording_path),
            mime_type="video/webm",
            tab_id=1,
            started_at_ms=10,
            ended_at_ms=20,
        )

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_get_video_metadata(path: Path):
            assert path == recording_path
            return SimpleNamespace(duration_seconds=20.0, fps=10.0, width=100, height=100)

        def fake_extract_frames(video_path, timestamps, job_id):
            assert video_path == recording_path
            frame_path = Path(temp_dir) / f"{job_id}_01.png"
            frame_path.write_bytes(b"frame")
            return [frame_path]

        def fake_build_replay_plan(frame_paths, config, user_request=None, site_url=None, frame_hints=None):
            return SimpleNamespace(
                sop=["Use session recording."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Use session recording."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Search",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.get_video_metadata = fake_get_video_metadata
        webapp_module.extract_frames = fake_extract_frames
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_video(
                    DummyRequest(
                        {
                            "listener_session_id": "recording-session",
                            "start_second": 0.0,
                            "end_second": 20.0,
                            "max_frames": 4,
                        }
                    )
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.get_video_metadata = original_get_video_metadata
            webapp_module.extract_frames = original_extract_frames
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "recording-session" in payload
    assert "\"listener_guided\":true" in payload
