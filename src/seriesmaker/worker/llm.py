from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import requests

from seriesmaker.models import StoryBible

MIN_SCENES, MAX_SCENES = 6, 10
# Narration pace ~2.5 words/s -> 30-60 s of speech.
MIN_WORDS, MAX_WORDS = 75, 150
MAX_ATTEMPTS = 3
MAX_RECAP_CHARS = 600
MAX_SCAN_CHARS = 200_000
# Trade-off: a reply with 200 or more stray '{' before the bible/script JSON is not scanned.
MAX_FAILED_DECODES = 200


class ScriptError(Exception):
    """The model replied but never produced a valid script."""


class LlmUnavailable(Exception):
    """The LLM endpoint could not be reached or returned a server error."""


def iter_json_objects(text: str):
    """Yield (obj, start, end) for each top-level JSON object found in text.

    Works for bare JSON, ```json fenced blocks and prose around an object.
    """
    if not isinstance(text, str):
        return
    # Bounded work: scan at most MAX_SCAN_CHARS and give up after MAX_FAILED_DECODES
    # failed attempts, so unterminated input cannot cause quadratic behavior.
    text = text[:MAX_SCAN_CHARS]
    dec = json.JSONDecoder()
    i = 0
    failures = 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            failures += 1
            if failures >= MAX_FAILED_DECODES:
                return
            i += 1
            continue
        yield obj, i, end
        i = end


def extract_json(text: str) -> dict:
    """First JSON object in text; ValueError if there is none."""
    for obj, _, _ in iter_json_objects(text):
        if isinstance(obj, dict):
            return obj
    raise ValueError("reply was not valid JSON")


def truncate(text: str, limit: int = MAX_RECAP_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class Scene:
    narration: str
    image_prompt: str
    caption: str


@dataclass
class Script:
    title: str
    scenes: list[Scene]

    def to_dict(self) -> dict:
        return asdict(self)


SCRIPT_SYSTEM = (
    "You write short faceless-video episode scripts. Reply with JSON ONLY, no prose, in the form "
    '{"title": str, "scenes": [{"narration": str, "image_prompt": str, "caption": str}]}. '
    f"Write between {MIN_SCENES} and {MAX_SCENES} scenes. Total narration across all scenes must be "
    f"between {MIN_WORDS} and {MAX_WORDS} words (about 30-60 seconds spoken). Reuse the series' "
    "characters by name and keep each character's look consistent in every image_prompt. "
    "Continue naturally from the previous episode recaps."
)


class LlmClient:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _complete(self, messages: list[dict], json_mode: bool = False) -> str:
        body: dict = {"model": self.model, "messages": messages}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise LlmUnavailable(f"LLM unreachable: {type(e).__name__}") from None
        if r.status_code >= 500:
            raise LlmUnavailable(f"LLM server error: HTTP {r.status_code}")
        r.raise_for_status()
        try:
            # content may legitimately be null/non-str; callers handle that.
            return r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LlmUnavailable("malformed response from LLM server") from None

    def chat(self, messages: list[dict]) -> str:
        return self._complete(messages)

    @staticmethod
    def _parse(raw: str) -> Script:
        """Parse and validate; raises ValueError (message is fed back to the model)."""
        data = extract_json(raw)
        raw_scenes = data.get("scenes")
        if not isinstance(raw_scenes, list):
            raise ValueError("'scenes' must be a list")
        if not all(isinstance(s, dict) for s in raw_scenes):
            raise ValueError("every scene must be an object")
        try:
            scenes = [
                Scene(str(s["narration"]), str(s["image_prompt"]), str(s["caption"]))
                for s in raw_scenes
            ]
        except KeyError as e:
            raise ValueError(f"scene missing field {e}") from None
        if not MIN_SCENES <= len(scenes) <= MAX_SCENES:
            raise ValueError(f"need {MIN_SCENES}-{MAX_SCENES} scenes, got {len(scenes)}")
        words = sum(len(s.narration.split()) for s in scenes)
        if not MIN_WORDS <= words <= MAX_WORDS:
            raise ValueError(
                f"invalid: {words} words, need {MIN_WORDS}-{MAX_WORDS} total narration"
            )
        if "title" not in data:
            raise ValueError("missing 'title'")
        return Script(str(data["title"]), scenes)

    def write_script(
        self,
        bible: StoryBible,
        instruction: str | None = None,
        previous: Script | None = None,
    ) -> Script:
        chars = "\n".join(f"- {json.dumps(c)}" for c in bible.characters) or "(none)"
        recaps = "\n".join(truncate(r) for r in bible.recaps[-3:]) or "(this is episode 1)"
        user = (
            f"Series: {bible.title}\nStyle: {bible.style.name}\n"
            f"Characters:\n{chars}\nRecent episode recaps (latest last):\n{recaps}\n"
        )
        if previous is not None and instruction:
            user += (
                "\nRevise this existing script. Change ONLY what the instruction affects and keep "
                f"everything else identical.\nInstruction: {instruction}\n"
                f"Existing script:\n{json.dumps(previous.to_dict())}\n"
            )
        else:
            user += "\nWrite the next episode."
            if instruction:
                user += f"\nExtra direction: {instruction}"
        messages = [
            {"role": "system", "content": SCRIPT_SYSTEM},
            {"role": "user", "content": user},
        ]
        last = ""
        for _ in range(MAX_ATTEMPTS):
            raw = self._complete(messages, json_mode=True)
            try:
                return self._parse(raw)
            except ValueError as e:  # JSONDecodeError is a ValueError
                last = str(e)
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": f"Your reply was rejected: {last}. Reply again with corrected JSON only.",
                    },
                ]
        raise ScriptError(f"no valid script after {MAX_ATTEMPTS} attempts: {last}")
