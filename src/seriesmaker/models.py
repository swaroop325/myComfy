from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum


class InvalidTransition(Exception):
    pass


class EpisodeExists(Exception):
    pass


class EpisodeState(str, Enum):
    DRAFT = "draft"
    GENERATING = "generating"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"


_ALLOWED: dict[EpisodeState, set[EpisodeState]] = {
    EpisodeState.DRAFT: {EpisodeState.GENERATING},
    EpisodeState.GENERATING: {EpisodeState.IN_REVIEW, EpisodeState.DRAFT},
    EpisodeState.IN_REVIEW: {
        EpisodeState.APPROVED,
        EpisodeState.REJECTED,
        EpisodeState.GENERATING,
    },
    EpisodeState.APPROVED: {EpisodeState.POSTED},
}


def transition(current: EpisodeState, target: EpisodeState) -> EpisodeState:
    if target not in _ALLOWED.get(current, set()):
        raise InvalidTransition(f"{current.value} -> {target.value}")
    return target


@dataclass
class StyleProfile:
    name: str
    checkpoint: str
    lora: str | None
    prompt_prefix: str
    seed: int
    motion: str


@dataclass
class StoryBible:
    series_id: str
    title: str
    style: StyleProfile
    characters: list[dict]
    recaps: list[str]


@dataclass
class Episode:
    series_id: str
    number: int
    state: EpisodeState
    script: dict | None
    s3_prefix: str
    updated_at: int | None = None  # epoch seconds of the last create/set_state
    generation: int = 0  # bumped by every Regenerate tap; varies scene seeds


@dataclass
class Job:
    type: str  # chat | episode | edit
    chat_id: int
    series_id: str | None
    payload: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> Job:
        return cls(**json.loads(s))
