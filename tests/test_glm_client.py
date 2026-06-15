from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.glm_client import merge_batch_outputs, split_frame_batches
import src.glm_client as glm_client_module


def test_split_frame_batches_groups_three_frames_per_batch() -> None:
    frame_paths = [Path(f"frame_{index:02d}.png") for index in range(1, 8)]
    frame_hints = [f"hint-{index}" for index in range(1, 8)]

    batches = split_frame_batches(frame_paths, frame_hints, batch_size=3)

    assert len(batches) == 3
    assert [path.name for path in batches[0][0]] == ["frame_01.png", "frame_02.png", "frame_03.png"]
    assert [path.name for path in batches[1][0]] == ["frame_04.png", "frame_05.png", "frame_06.png"]
    assert [path.name for path in batches[2][0]] == ["frame_07.png"]
    assert batches[0][1] == ["hint-1", "hint-2", "hint-3"]
    assert batches[2][1] == ["hint-7"]


def test_merge_batch_outputs_reindexes_actions() -> None:
    merged = merge_batch_outputs(
        [
            {
                "session_summary": "batch one",
                "observed_actions": [
                    {"step_number": 1, "action": "click", "target": "A"},
                    {"step_number": 2, "action": "type", "target": "B"},
                ],
                "uncertainties": ["first note"],
            },
            {
                "session_summary": "batch two",
                "observed_actions": [
                    {"step_number": 1, "action": "click", "target": "C"},
                ],
                "uncertainties": ["second note"],
            },
        ]
    )

    assert merged["session_summary"] == "batch one batch two"
    assert [item["step_number"] for item in merged["observed_actions"]] == [1, 2, 3]
    assert [item["target"] for item in merged["observed_actions"]] == ["A", "B", "C"]
    assert merged["uncertainties"] == ["batch 1: first note", "batch 2: second note"]


def test_analyze_frames_reports_batch_progress() -> None:
    config = SimpleNamespace(glm_base_url="https://example.com/v1", glm_model="stub-model", glm_api_key="secret")
    client = glm_client_module.GLMClient(config, timeout_seconds=1)
    progress_events: list[tuple[int, int]] = []

    class FakeResponse:
        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"session_summary":"","observed_actions":[],"uncertainties":[]}',
                        }
                    }
                ]
            }

    def fake_post_chat_completion(*args, **kwargs):
        return FakeResponse()

    original_post_chat_completion = glm_client_module.post_chat_completion
    glm_client_module.post_chat_completion = fake_post_chat_completion

    try:
        with TemporaryDirectory() as temp_dir:
            frame_paths = []
            for index in range(4):
                frame_path = Path(temp_dir) / f"frame_{index:02d}.png"
                frame_path.write_bytes(b"frame")
                frame_paths.append(frame_path)

            client.analyze_frames(
                frame_paths=frame_paths,
                site_url="https://example.com",
                batch_progress_callback=lambda current, total: progress_events.append((current, total)),
            )
    finally:
        glm_client_module.post_chat_completion = original_post_chat_completion

    assert progress_events == [(1, 2), (2, 2)]


def test_analyze_frames_disables_glm_thinking_for_json_output(monkeypatch) -> None:
    monkeypatch.delenv("VLM_GLM_THINKING", raising=False)
    config = SimpleNamespace(
        glm_base_url="https://open.bigmodel.cn/api/paas/v4",
        glm_model="glm-5v-turbo",
        glm_api_key="secret",
    )
    client = glm_client_module.GLMClient(config, timeout_seconds=1)
    captured_payload: dict = {}

    class FakeResponse:
        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"session_summary":"","observed_actions":[],"uncertainties":[]}',
                        }
                    }
                ]
            }

    def fake_post_chat_completion(*args, **kwargs):
        captured_payload.update(kwargs["payload"])
        return FakeResponse()

    original_post_chat_completion = glm_client_module.post_chat_completion
    glm_client_module.post_chat_completion = fake_post_chat_completion

    try:
        with TemporaryDirectory() as temp_dir:
            frame_path = Path(temp_dir) / "frame.png"
            frame_path.write_bytes(b"frame")
            client.analyze_frames(frame_paths=[frame_path], site_url="https://example.com")
    finally:
        glm_client_module.post_chat_completion = original_post_chat_completion

    assert captured_payload["model"] == "glm-5v-turbo"
    assert captured_payload["thinking"] == {"type": "disabled"}
    image_part = captured_payload["messages"][1]["content"][1]
    assert image_part["image_url"]["url"] == "ZnJhbWU="
    assert not image_part["image_url"]["url"].startswith("data:image/")
