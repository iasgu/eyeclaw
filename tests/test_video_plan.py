from src.video import clamp_segment, plan_frame_timestamps


def test_plan_frame_timestamps_caps_frames_within_selected_window() -> None:
    timestamps = plan_frame_timestamps(
        duration_seconds=43.0,
        start_second=5.0,
        end_second=21.0,
        max_frames=8,
    )

    assert len(timestamps) == 8
    assert timestamps[0] == 5.0
    assert timestamps[-1] == 21.0
    assert all(5.0 <= ts <= 21.0 for ts in timestamps)


def test_clamp_segment_stays_inside_video_bounds() -> None:
    start_second, end_second = clamp_segment(
        duration_seconds=43.0,
        start_second=-4.0,
        end_second=50.0,
    )

    assert start_second == 0.0
    assert end_second == 43.0
