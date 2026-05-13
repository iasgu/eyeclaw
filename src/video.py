from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, List

import cv2


ARTIFACTS_DIR = Path("artifacts")
UPLOADS_DIR = ARTIFACTS_DIR / "uploads"
FRAMES_DIR = ARTIFACTS_DIR / "frames"


@dataclass(frozen=True)
class VideoMetadata:
    path: Path
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int


def save_uploaded_video(upload_name: str, stream: BinaryIO) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target_path = UPLOADS_DIR / upload_name
    target_path.write_bytes(stream.read())
    return target_path


def get_video_metadata(video_path: Path) -> VideoMetadata:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration_seconds = frame_count / fps if fps > 0 else 0.0
    capture.release()

    return VideoMetadata(
        path=video_path,
        duration_seconds=duration_seconds,
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
    )


def clamp_segment(duration_seconds: float, start_second: float, end_second: float) -> tuple[float, float]:
    if duration_seconds <= 0:
        return 0.0, 0.0

    bounded_start = max(0.0, min(start_second, duration_seconds))
    bounded_end = max(bounded_start, min(end_second, duration_seconds))
    return bounded_start, bounded_end


def plan_frame_timestamps(
    duration_seconds: float,
    start_second: float,
    end_second: float,
    max_frames: int,
) -> List[float]:
    if max_frames <= 0:
        return []

    start_second, end_second = clamp_segment(duration_seconds, start_second, end_second)
    if duration_seconds <= 0 or end_second <= start_second:
        return [round(start_second, 2)]

    if max_frames == 1:
        return [round(start_second, 2)]

    segment_duration = end_second - start_second
    step = segment_duration / (max_frames - 1)
    timestamps = [round(start_second + index * step, 2) for index in range(max_frames)]
    timestamps[-1] = round(end_second, 2)
    return timestamps


def extract_frames(video_path: Path, timestamps: Iterable[float], job_id: str) -> List[Path]:
    target_dir = FRAMES_DIR / job_id
    target_dir.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    frame_paths: List[Path] = []
    for index, timestamp in enumerate(timestamps, start=1):
        capture.set(cv2.CAP_PROP_POS_MSEC, max(timestamp, 0.0) * 1000)
        success, frame = capture.read()
        if not success:
            continue

        frame_path = target_dir / f"frame_{index:02d}.png"
        cv2.imwrite(str(frame_path), frame)
        frame_paths.append(frame_path)

    capture.release()
    return frame_paths


def format_duration(duration_seconds: float) -> str:
    total_seconds = max(0, int(round(duration_seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"
