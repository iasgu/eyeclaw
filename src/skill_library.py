from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4


@dataclass
class SavedSkill:
    id: str
    name: str
    description: str
    source_type: str
    steps: list[dict[str, Any]]
    site_url: str | None = None
    user_request: str = ""
    video_path: str | None = None
    listener_session_id: str | None = None
    created_at_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SkillLibrary:
    def __init__(self, storage_path: Path | str = Path("config/user_skills.json")) -> None:
        self._storage_path = Path(storage_path)
        self._lock = Lock()
        self._skills: dict[str, SavedSkill] = {}
        self._load()

    def list_skills(self) -> list[SavedSkill]:
        with self._lock:
            return sorted(self._skills.values(), key=lambda skill: skill.created_at_iso, reverse=True)

    def get_skill(self, skill_id: str) -> SavedSkill | None:
        with self._lock:
            return self._skills.get(skill_id)

    def get_many(self, skill_ids: list[str]) -> list[SavedSkill]:
        with self._lock:
            return [self._skills[skill_id] for skill_id in skill_ids if skill_id in self._skills]

    def update_skill(
        self,
        skill_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        source_type: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        site_url: str | None = None,
        user_request: str | None = None,
        video_path: str | None = None,
        listener_session_id: str | None = None,
    ) -> SavedSkill | None:
        with self._lock:
            skill = self._skills.get(skill_id)
            if skill is None:
                return None
            if name is not None:
                skill.name = name.strip() or "Untitled Skill"
            if description is not None:
                skill.description = description.strip()
            if source_type is not None:
                skill.source_type = source_type.strip() or "analysis"
            if steps is not None:
                skill.steps = steps
            if site_url is not None:
                skill.site_url = site_url.strip() if site_url.strip() else None
            if user_request is not None:
                skill.user_request = user_request.strip()
            if video_path is not None:
                skill.video_path = video_path
            if listener_session_id is not None:
                skill.listener_session_id = listener_session_id
            self._persist()
            return skill

    def delete_skill(self, skill_id: str) -> SavedSkill | None:
        with self._lock:
            skill = self._skills.pop(skill_id, None)
            if skill is not None:
                self._persist()
            return skill

    def create_skill(
        self,
        *,
        name: str,
        description: str,
        source_type: str,
        steps: list[dict[str, Any]],
        site_url: str | None = None,
        user_request: str = "",
        video_path: str | None = None,
        listener_session_id: str | None = None,
    ) -> SavedSkill:
        skill = SavedSkill(
            id=uuid4().hex[:12],
            name=name.strip() or "Untitled Skill",
            description=description.strip(),
            source_type=source_type.strip() or "analysis",
            steps=steps,
            site_url=site_url.strip() if isinstance(site_url, str) and site_url.strip() else None,
            user_request=user_request.strip(),
            video_path=video_path,
            listener_session_id=listener_session_id,
        )
        with self._lock:
            self._skills[skill.id] = skill
            self._persist()
        return skill

    def _load(self) -> None:
        if not self._storage_path.exists():
            return
        data = json.loads(self._storage_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        self._skills = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            skill = SavedSkill(**item)
            self._skills[skill.id] = skill

    def _persist(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            asdict(skill)
            for skill in sorted(self._skills.values(), key=lambda item: item.created_at_iso, reverse=True)
        ]
        self._storage_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
