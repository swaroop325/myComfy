from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from typing import Any

from botocore.exceptions import ClientError

from seriesmaker.models import Episode, EpisodeState, Job, StoryBible, StyleProfile
from seriesmaker.store import Store
from seriesmaker.telegram import TelegramClient, TelegramError
from seriesmaker.worker.llm import LlmClient, Scene, Script, iter_json_objects, truncate

# NOTE: the bot Lambda imports this module and has no Pillow; assemble/profiles/tts
# are imported lazily inside run_episode_job helpers.

log = logging.getLogger(__name__)

# Minimal default; Task 8's profiles.py owns the real values.
ANIMATION = StyleProfile(
    name="animation", checkpoint="", lora=None, prompt_prefix="", seed=1234, motion="pan_zoom"
)


@dataclass
class Deps:
    store: Store
    telegram: TelegramClient
    llm: LlmClient
    bucket: str = ""
    s3: Any = None
    workdir: str = "/tmp/seriesmaker"
    comfy: Any = None
    tts: Any = None
    assemble: Any = None
    owner_chat_id: int = 0


CHAT_SYSTEM = (
    "You are a story-development assistant helping the owner design a faceless short-video series. "
    "Ask about genre, characters and tone, and propose a story. Only when the owner explicitly "
    "confirms the story, end your reply with a fenced ```json block containing exactly "
    '{"story_bible": {"title": str, "characters": [{"name": str, "look": str}], '
    '"style": "animation" | "realistic"}}. Never emit that block before confirmation.'
)

_EMPTY_FENCE = re.compile(r"```(?:json)?\s*```")
CHAT_ROLES = {"user", "assistant"}
TELEGRAM_CHUNK = 4000  # Telegram's hard limit is 4096 characters per message
GENERATION_SEED_STEP = 1000  # Regenerate N shifts every scene seed by N * 1000


def series_id_for(chat_id: int) -> str:
    return f"series-{chat_id}"


def _extract_bible(reply: str) -> tuple[dict | None, str]:
    """Return (story_bible dict or None, reply text with the bible JSON removed)."""
    if not isinstance(reply, str):
        return None, reply
    for obj, start, end in iter_json_objects(reply):
        if isinstance(obj, dict) and isinstance(obj.get("story_bible"), dict):
            visible = reply[:start] + reply[end:]
            return obj["story_bible"], _EMPTY_FENCE.sub("", visible).strip()
    return None, reply


def _merge_characters(old: list[dict], new: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    nameless: list[dict] = []
    for c in [*old, *new]:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            if c not in nameless:
                nameless.append(c)
        else:
            merged[str(name)] = {**merged.get(str(name), {}), **c}
    return [*merged.values(), *nameless]


def split_message(text: str, limit: int = TELEGRAM_CHUNK) -> list[str]:
    """Split text into <= limit chunks, preferring paragraph, then line, then word breaks."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = rest.rfind(sep, 0, limit + 1)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit
        head = rest[:cut].rstrip()
        if head:
            chunks.append(head)
        rest = rest[cut:].lstrip()
    if rest.strip():
        chunks.append(rest)
    return chunks


def run_chat_job(job: Job, deps: Deps) -> None:
    """The bot Lambda has already stored the owner's message in the Store.

    LlmUnavailable is deliberately not caught: the worker loop notifies the owner
    and lets SQS redeliver.
    """
    store = deps.store
    system = CHAT_SYSTEM
    if job.series_id:
        bible = store.get_bible(job.series_id)
        if bible:
            recaps = [truncate(r) for r in bible.recaps[-3:]]
            system += (
                f"\n\nExisting series: {bible.title}\nCharacters: {json.dumps(bible.characters)}"
                f"\nRecaps: {json.dumps(recaps)}"
            )
    messages = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["text"]}
        for m in store.recent_messages(job.chat_id)
        if m["role"] in CHAT_ROLES
    ]
    reply = deps.llm.chat(messages)
    if not isinstance(reply, str) or not reply.strip():
        deps.telegram.send_message(job.chat_id, "LLM returned an empty reply, please try again.")
        return

    spec, visible = _extract_bible(reply)
    title = None
    if spec is not None:
        sid = job.series_id or series_id_for(job.chat_id)
        existing = store.get_bible(sid)
        style = replace(ANIMATION, name="realistic") if str(spec.get("style", "")).lower() == "realistic" else ANIMATION
        raw_chars = spec.get("characters")
        chars = list(raw_chars) if isinstance(raw_chars, list) else []
        title = str(spec.get("title", "Untitled"))
        store.put_bible(
            StoryBible(
                series_id=sid,
                title=title,
                style=style,
                characters=_merge_characters(existing.characters, chars) if existing else chars,
                recaps=list(existing.recaps) if existing else [],
            )
        )
    if not visible.strip():
        visible = (
            f"Story saved: {title}. Send /episode to start the first episode."
            if title is not None
            else "Got it."
        )
    # Store BEFORE sending, and never let a failed send retry the job: a retry would
    # re-run the LLM and duplicate this reply in the history.
    store.put_message(job.chat_id, "assistant", visible)
    for chunk in split_message(visible):
        try:
            deps.telegram.send_message(job.chat_id, chunk)
        except TelegramError as e:
            log.warning("chat reply not sent: %s", type(e).__name__)
            return


# ---------------------------------------------------------------- episodes

MIN_DURATION_S, MAX_DURATION_S = 30.0, 60.0
MIN_SCENES, MAX_SCENES = 6, 10  # mirrors worker.llm


def _scene_from_dict(d: dict) -> Scene:
    return Scene(str(d.get("narration", "")), str(d.get("image_prompt", "")), str(d.get("caption", "")))


def _script_from_dict(d: dict) -> Script:
    return Script(str(d.get("title", "")), [_scene_from_dict(s) for s in d.get("scenes", [])])


def _profile_for(bible: StoryBible) -> StyleProfile:
    st = bible.style
    if st.checkpoint:
        return st
    from seriesmaker.worker.profiles import PROFILES

    return PROFILES.get(st.name, st)


def _sig(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _fetch_if_sig(deps: Deps, key: str, sig: str, dest: str) -> bool:
    """Download key to dest iff its S3 user metadata 'sig' equals sig; else False."""
    try:
        head = deps.s3.head_object(Bucket=deps.bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404", "NotFound"):
            return False
        raise
    if head.get("Metadata", {}).get("sig") != sig:
        return False
    body = deps.s3.get_object(Bucket=deps.bucket, Key=key)["Body"].read()
    with open(dest, "wb") as f:
        f.write(body)
    return True


def _delete_stale_scenes(deps: Deps, prefix: str, count: int) -> None:
    """Best-effort cleanup after final.mp4 landed: errors are logged, never raised."""
    try:
        resp = deps.s3.list_objects_v2(Bucket=deps.bucket, Prefix=prefix)
        for o in resp.get("Contents", []):
            m = re.match(r"(\d+)\.(png|wav)$", o["Key"][len(prefix):])
            if m and int(m.group(1)) >= count:
                deps.s3.delete_object(Bucket=deps.bucket, Key=o["Key"])
    except Exception as e:  # noqa: BLE001 - the episode is already in review
        log.warning("stale scene cleanup failed: %s", type(e).__name__)


def _claim(deps: Deps, series_id: str, number: int) -> Episode | None:
    """Return the episode in GENERATING, or None if its state forbids generation."""
    store = deps.store
    ep = store.get_episode(series_id, number)
    if ep is None:
        store.create_episode(series_id, number)
        store.set_state(series_id, number, EpisodeState.GENERATING)
    elif ep.state == EpisodeState.DRAFT:
        store.set_state(series_id, number, EpisodeState.GENERATING)
    elif ep.state != EpisodeState.GENERATING:
        return None
    return store.get_episode(series_id, number)


def run_episode_job(job: Job, deps: Deps) -> None:
    """Generate (or edit/regenerate) an episode and leave it IN_REVIEW with final.mp4 in S3.

    Exceptions propagate (the worker loop resets state and notifies); temp files are always removed.
    """
    series_id = job.series_id or series_id_for(job.chat_id)
    number = int(job.payload["number"])
    instruction = job.payload.get("instruction") or None
    store = deps.store
    from seriesmaker.worker.assemble import SceneAsset
    from seriesmaker.worker.tts import voice_model, wav_duration

    voice = voice_model()
    bible = store.get_bible(series_id)
    if bible is None:
        deps.telegram.send_message(deps.owner_chat_id, f"No story bible for {series_id}; cannot generate.")
        return
    ep = _claim(deps, series_id, number)
    if ep is None:
        cur = store.get_episode(series_id, number)
        state = cur.state.value if cur else "missing"
        deps.telegram.send_message(deps.owner_chat_id, f"Episode {number} is {state}, nothing to do")
        return

    # Regenerate taps bump the generation so every scene gets a new seed; edits and
    # first runs keep the current one (edits then reuse unchanged scenes).
    if job.type == "episode" and job.payload.get("regen"):
        generation = store.bump_generation(series_id, number)
    else:
        generation = ep.generation

    existing = _script_from_dict(ep.script) if ep.script else None
    is_edit = job.type == "edit" and existing is not None
    if is_edit:
        script = deps.llm.write_script(bible, instruction, existing)
    elif existing is not None and not instruction and MIN_SCENES <= len(existing.scenes) <= MAX_SCENES:
        script = existing  # plain regenerate from the stored script
    elif existing is not None and not instruction:
        log.warning("stored script for %s/%s has %d scenes; writing a fresh one",
                    series_id, number, len(existing.scenes))
        script = deps.llm.write_script(bible, None, None)
    else:
        script = deps.llm.write_script(bible, instruction, None)
    store.set_script(series_id, number, script.to_dict())

    profile = _profile_for(bible)
    base = f"episodes/{series_id}/{number}"
    os.makedirs(deps.workdir, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=deps.workdir, prefix="ep-")
    try:
        assets: list[SceneAsset] = []
        for i, scene in enumerate(script.scenes):
            img_path, wav_path = os.path.join(tmp, f"{i:02d}.png"), os.path.join(tmp, f"{i:02d}.wav")
            img_key, wav_key = f"{base}/scenes/{i:02d}.png", f"{base}/scenes/{i:02d}.wav"
            seed = profile.seed + i + generation * GENERATION_SEED_STEP
            img_sig = _sig([profile.name, profile.checkpoint, profile.lora, profile.prompt_prefix,
                            seed, scene.image_prompt, generation])
            wav_sig = _sig([scene.narration, voice])
            # Reuse only on edit jobs, and only when S3's own signature matches; the DB script
            # is never trusted for reuse (a failed earlier run may have left stale objects).
            if is_edit and _fetch_if_sig(deps, img_key, img_sig, img_path):
                pass
            else:
                img = deps.comfy.render_image(profile, profile.prompt_prefix + scene.image_prompt, seed)
                with open(img_path, "wb") as f:
                    f.write(img)
                deps.s3.put_object(Bucket=deps.bucket, Key=img_key, Body=img, ContentType="image/png",
                                   Metadata={"sig": img_sig})
            if is_edit and _fetch_if_sig(deps, wav_key, wav_sig, wav_path):
                duration = wav_duration(wav_path)
            else:
                duration = deps.tts(scene.narration, wav_path)
                with open(wav_path, "rb") as f:
                    deps.s3.put_object(Bucket=deps.bucket, Key=wav_key, Body=f.read(),
                                       ContentType="audio/wav", Metadata={"sig": wav_sig})
            assets.append(SceneAsset(img_path, wav_path, float(duration), scene.caption))

        # Thumbnail from the first scene PNG, before the expensive assemble, so Pillow
        # problems surface early.
        from PIL import Image

        thumb_path = os.path.join(tmp, "thumb.jpg")
        with Image.open(assets[0].image_path) as im:
            im.convert("RGB").save(thumb_path, "JPEG", quality=85)

        total = sum(a.duration_s for a in assets)
        if not MIN_DURATION_S <= total <= MAX_DURATION_S:
            log.warning("episode %s/%s duration %.1fs outside %d-%ds", series_id, number, total,
                        MIN_DURATION_S, MAX_DURATION_S)

        music = os.path.join(deps.workdir, "music", "default.mp3")
        final_path = os.path.join(tmp, "final.mp4")
        deps.assemble(assets, music if os.path.exists(music) else None, final_path, motion=profile.motion)

        # Order matters: the notify Lambda fires on final.mp4, so everything it needs
        # (script.json, thumb) is uploaded first and the state is IN_REVIEW before final.mp4 lands.
        put = deps.s3.put_object
        put(Bucket=deps.bucket, Key=f"{base}/script.json", ContentType="application/json",
            Body=json.dumps({**script.to_dict(), "duration_s": round(total, 2)}).encode())
        with open(thumb_path, "rb") as f:
            put(Bucket=deps.bucket, Key=f"{base}/thumb.jpg", Body=f.read(), ContentType="image/jpeg")
        store.set_state(series_id, number, EpisodeState.IN_REVIEW)
        try:
            with open(final_path, "rb") as f:
                put(Bucket=deps.bucket, Key=f"{base}/final.mp4", Body=f.read(),
                    ContentType="video/mp4")
        except BaseException:
            # No reel landed: back to GENERATING so the worker's failure path can reset it.
            try:
                store.set_state(series_id, number, EpisodeState.GENERATING)
            except Exception as e:  # noqa: BLE001
                log.warning("revert to generating failed: %s", type(e).__name__)
            raise
        _delete_stale_scenes(deps, f"{base}/scenes/", len(script.scenes))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
