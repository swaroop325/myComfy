"""Publish an approved episode (manual hand-off via Telegram, or Instagram Reels)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import boto3
import requests

from seriesmaker.models import EpisodeState, InvalidTransition
from seriesmaker.store import Store
from seriesmaker.telegram import TelegramClient

log = logging.getLogger(__name__)

# One base for every Graph call (media, media_publish, status polling, token refresh).
# Default: Instagram API with Instagram Login. Facebook-Login tokens need
# INSTAGRAM_GRAPH_BASE=https://graph.facebook.com (Terraform var instagram_graph_base).
GRAPH_BASE = os.environ.get("INSTAGRAM_GRAPH_BASE", "https://graph.instagram.com").rstrip("/")
GRAPH_VERSION = "v21.0"
REFRESH_AFTER = timedelta(days=30)
POLL_INTERVAL = 5
POLL_BUDGET_S = 600
RECAP_MAX = 600
MANUAL_CAPTION = "Upload this manually to Instagram"
# Telegram fetches URL media only up to 20 MB; sendDocument by URL cannot fetch mp4 at all.
TELEGRAM_URL_LIMIT = 20 * 1024 * 1024
MANUAL_PRESIGN_S = 6 * 3600


def _graph(path: str) -> str:
    return f"{GRAPH_BASE}/{GRAPH_VERSION}/{path}"


class InstagramError(Exception):
    pass


@dataclass
class Deps:
    store: Store
    tg: TelegramClient
    owner_chat_id: int
    presign: Callable[[str, int, int], str]  # series, number, expires_seconds
    param: Callable[[str], str | None]  # instagram_enabled|instagram_token|instagram_user_id
    put_token: Callable[[str], None]
    token_modified: Callable[[], datetime | None]
    http: Any = requests
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    size: Callable[[str, int], int] | None = None  # final.mp4 bytes; None -> unknown


def _status(e: Exception) -> str:
    code = getattr(e, "status_code", None)
    if code is None:
        code = getattr(getattr(e, "response", None), "status_code", None)
    return str(code) if code is not None else "n/a"


def refresh_token(deps: Deps, token: str) -> str:
    """Renew the long-lived token and persist it. Returns the new token."""
    resp = deps.http.get(
        f"{GRAPH_BASE}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    )
    resp.raise_for_status()
    new = resp.json()["access_token"]
    deps.put_token(new)
    return new


def _instagram_post(deps: Deps, token: str, ig_user: str, series_id: str, number: int,
                    caption: str, deadline: float) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    video_url = deps.presign(series_id, number, 3600)
    r = deps.http.post(
        _graph(f"{ig_user}/media"),
        data={"media_type": "REELS", "video_url": video_url, "caption": caption},
        headers=auth,
        timeout=60,
    )
    r.raise_for_status()
    container = r.json()["id"]
    while True:
        if deps.clock() >= deadline:
            raise InstagramError("container processing timed out")
        s = deps.http.get(
            _graph(container),
            params={"fields": "status_code"},
            headers=auth,
            timeout=30,
        )
        s.raise_for_status()
        code = s.json().get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {code}")
        deps.sleep(POLL_INTERVAL)
    # From here on the post may have gone through: any failure is ambiguous,
    # except a 4xx rejection (Instagram refused it).
    try:
        p = deps.http.post(
            _graph(f"{ig_user}/media_publish"),
            data={"creation_id": container},
            headers=auth,
            timeout=60,
        )
    except Exception as e:
        e.ambiguous = True
        raise
    code = getattr(p, "status_code", 200)
    if 400 <= code < 500:
        p.raise_for_status()
        raise InstagramError(f"publish rejected: {code}")
    try:
        p.raise_for_status()
        if not p.json()["id"]:
            raise InstagramError("empty publish response")
    except Exception as e:
        e.ambiguous = True
        raise


def _recap_text(script: dict | None) -> str:
    script = script or {}
    parts = [script.get("title", "")] + [
        s.get("narration", "") for s in script.get("scenes", [])
    ]
    return " ".join(p for p in parts if p)[:RECAP_MAX]


def _safe_send(deps: Deps, text: str, buttons=None) -> None:
    try:
        deps.tg.send_message(deps.owner_chat_id, text, buttons)
    except Exception as e:  # noqa: BLE001
        log.warning("owner notice not sent: %s", type(e).__name__)


def _deadline(deps: Deps, context) -> float:
    deadline = deps.clock() + POLL_BUDGET_S
    if context is not None:
        remaining = context.get_remaining_time_in_millis() / 1000 - 30
        deadline = min(deadline, deps.clock() + remaining)
    return deadline


def _send_manual(deps: Deps, series_id: str, number: int) -> None:
    """Hand the reel to the owner. Never sendDocument: Telegram cannot fetch an mp4
    document by URL. Small files go as a video; larger ones as a download link."""
    size = deps.size(series_id, number) if deps.size else None
    url = deps.presign(series_id, number, MANUAL_PRESIGN_S)
    if size is not None and size <= TELEGRAM_URL_LIMIT:
        deps.tg.send_video(deps.owner_chat_id, url, MANUAL_CAPTION)
        return
    too_big = f" ({size / 1048576:.0f} MB, over Telegram's 20 MB limit)" if size else ""
    deps.tg.send_message(
        deps.owner_chat_id,
        f"{MANUAL_CAPTION}. The reel is too large to send here{too_big}; download it: {url}\n"
        "The link expires within 6 hours, sooner if the publish Lambda's temporary "
        "credentials expire first, so download it now.",
    )


def _post(deps: Deps, series_id: str, number: int, ep, context) -> str:
    """Do the actual posting; returns the mode. Raises on any failure."""
    token = deps.param("instagram_token")
    ig_user = deps.param("instagram_user_id")
    enabled = (deps.param("instagram_enabled") or "").strip().lower() == "true"
    if not (enabled and token and ig_user):
        _send_manual(deps, series_id, number)
        log.info("episode %s/%s posted in manual mode", series_id, number)
        return "manual"
    title = (ep.script or {}).get("title") or f"Episode {number}"
    modified = deps.token_modified()
    if modified is not None and datetime.now(timezone.utc) - modified > REFRESH_AFTER:
        try:
            token = refresh_token(deps, token)
        except Exception as e:  # noqa: BLE001
            log.warning("token refresh failed: %s", type(e).__name__)
    _instagram_post(deps, token, ig_user, series_id, number, title, _deadline(deps, context))
    return "instagram"


def publish_episode(series_id: str, number: int, deps: Deps | None = None,
                    retry: bool = False, context=None) -> str:
    """Post an approved episode. Returns "manual" or "instagram"; "" after a
    notified failure (episode stays APPROVED, owner gets a Retry button)."""
    deps = deps or _get_deps()
    if retry:
        ep = deps.store.get_episode(series_id, number)
        if ep is None or ep.state != EpisodeState.APPROVED:
            raise InvalidTransition(f"{series_id}/{number} is not awaiting retry")
    else:
        # Double-tap guard: a second approve raises before anything is posted.
        ep = deps.store.set_state(series_id, number, EpisodeState.APPROVED)

    try:
        # Two quick Retry taps: the second is refused while the lease is held.
        deps.store.acquire_publish_lease(series_id, number)
        mode = _post(deps, series_id, number, ep, context)
    except InvalidTransition:
        raise
    except Exception as e:  # noqa: BLE001 - never leak tokens/URLs
        name, code = type(e).__name__, _status(e)
        log.error("publish failed: %s (%s)", name, code)
        ambiguous = getattr(e, "ambiguous", False)
        if not ambiguous:  # ambiguous: keep the lease so a reflexive Retry is refused
            try:
                deps.store.release_publish_lease(series_id, number)
            except Exception as le:  # noqa: BLE001
                log.warning("lease release failed: %s", type(le).__name__)
        warn = ""
        if ambiguous:
            warn = " Check Instagram before retrying - the post may have gone through."
            try:
                until = deps.store.publish_lease_until(series_id, number)
            except Exception as le:  # noqa: BLE001
                log.warning("lease read failed: %s", type(le).__name__)
                until = None
            if until:
                hhmm = datetime.fromtimestamp(until, timezone.utc).strftime("%H:%M")
                warn += f" Retry is possible after {hhmm} UTC."
        _safe_send(
            deps,
            f"Publishing episode {number} failed: {name} (status {code}).{warn} "
            "The episode is still approved.",
            [[("Retry", f"retry:{series_id}:{number}")]],
        )
        return ""

    try:
        deps.store.set_state(series_id, number, EpisodeState.POSTED)
    except Exception as e:  # noqa: BLE001
        log.error("POSTED update failed after publish: %s", type(e).__name__)
        _safe_send(
            deps,
            "Posted to Instagram, but I couldn't update the episode status "
            "- do NOT retry."
            if mode == "instagram"
            else "Sent for manual upload, but I couldn't update the episode status "
            "- do NOT retry.",
        )
        return mode
    try:
        recap = _recap_text(ep.script)
        if recap:
            deps.store.append_recap(series_id, recap)
    except Exception as e:  # noqa: BLE001
        log.error("recap append failed: %s", type(e).__name__)
    _safe_send(
        deps, "Posted to Instagram ✅" if mode == "instagram" else "Sent for manual upload ✅"
    )
    return mode


def lambda_handler(event: dict, context) -> dict:
    """Async-invoked by the bot. Never raises (a raise would retry and could double-post)."""
    deps = None
    try:
        series_id = event["series_id"]
        number = int(event["number"])
        retry = bool(event.get("retry", False))
        deps = _get_deps()
        return {"status": publish_episode(series_id, number, deps, retry=retry, context=context)}
    except InvalidTransition as e_:
        state = "unknown"
        try:
            ep = deps.store.get_episode(series_id, number)
            state = ep.state.value if ep else "missing"
        except Exception:  # noqa: BLE001
            pass
        if deps:
            detail = str(e_) if "publish in progress" in str(e_) else ""
            _safe_send(
                deps,
                detail or f"That episode can't be posted right now (state: {state})",
            )
        return {"status": "refused"}
    except Exception as e:  # noqa: BLE001
        log.error("publish lambda error: %s", type(e).__name__)
        if deps:
            _safe_send(deps, f"Publishing failed: {type(e).__name__}")
        return {"status": "error"}


def _ssm(name: str, decrypt: bool = True) -> dict | None:
    try:
        return boto3.client("ssm").get_parameter(Name=name, WithDecryption=decrypt)["Parameter"]
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "ParameterNotFound":
            return None
        raise


def _build_deps() -> Deps:
    s3 = boto3.client("s3")
    bucket = os.environ["BUCKET"]
    names = {
        "instagram_token": os.environ.get("INSTAGRAM_TOKEN_PARAM", "/seriesmaker/instagram_token"),
        "instagram_user_id": os.environ.get(
            "INSTAGRAM_USER_ID_PARAM", "/seriesmaker/instagram_user_id"
        ),
        "instagram_enabled": os.environ.get(
            "INSTAGRAM_ENABLED_PARAM", "/seriesmaker/instagram_enabled"
        ),
    }

    def param(key: str) -> str | None:
        p = _ssm(names[key])
        return p["Value"] if p else None

    def token_modified() -> datetime | None:
        p = _ssm(names["instagram_token"])
        return p.get("LastModifiedDate") if p else None

    def put_token(value: str) -> None:
        boto3.client("ssm").put_parameter(
            Name=names["instagram_token"], Value=value, Type="SecureString", Overwrite=True
        )

    tg_token = _ssm(os.environ.get("TELEGRAM_TOKEN_PARAM", "/seriesmaker/telegram_token"))
    owner = _ssm(os.environ.get("OWNER_CHAT_ID_PARAM", "/seriesmaker/owner_chat_id"))
    return Deps(
        store=Store(os.environ["TABLE"]),
        tg=TelegramClient(tg_token["Value"]),
        owner_chat_id=int(owner["Value"]),
        presign=lambda s, n, exp: s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": f"episodes/{s}/{n}/final.mp4"},
            ExpiresIn=exp,
        ),
        size=lambda s, n: s3.head_object(Bucket=bucket, Key=f"episodes/{s}/{n}/final.mp4")[
            "ContentLength"
        ],
        param=param,
        put_token=put_token,
        token_modified=token_modified,
    )


_deps: Deps | None = None


def _get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = _build_deps()
    return _deps
