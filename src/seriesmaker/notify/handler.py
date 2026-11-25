"""S3-triggered Lambda: send a finished reel to the owner for review."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import unquote

import boto3

from seriesmaker.models import EpisodeState
from seriesmaker.store import Store
from seriesmaker.telegram import TelegramClient

log = logging.getLogger(__name__)

_KEY = re.compile(r"^episodes/([^/]+)/(\d+)/final\.mp4$")
TELEGRAM_URL_LIMIT = 20 * 1024 * 1024
PRESIGN_SECONDS = 6 * 3600


@dataclass
class Deps:
    store: Store
    tg: TelegramClient
    owner_chat_id: int
    presign: Callable[[str, str], str]
    size: Callable[[str, str], int]


def _buttons(series_id: str, number: int) -> list[list[tuple[str, str]]]:
    return [
        [("Approve", f"approve:{series_id}:{number}"), ("Regenerate", f"regen:{series_id}:{number}")],
        [("Edit", f"edit:{series_id}:{number}"), ("Reject", f"reject:{series_id}:{number}")],
    ]


def _handle_record(record: dict, deps: Deps) -> None:
    bucket = record["s3"]["bucket"]["name"]
    key = unquote(record["s3"]["object"]["key"].replace("+", " "))
    m = _KEY.match(key)
    if not m:
        log.info("ignoring key outside episodes/<series>/<n>/final.mp4")
        return
    series_id, number = m.group(1), int(m.group(2))
    ep = deps.store.get_episode(series_id, number)
    if ep is None or ep.state != EpisodeState.IN_REVIEW:
        log.info("skipping %s/%s: not in review", series_id, number)
        return
    title = (ep.script or {}).get("title") or f"Episode {number}"
    caption = f"{title} (episode {number})"
    url = deps.presign(bucket, key)
    buttons = _buttons(series_id, number)
    if deps.size(bucket, key) > TELEGRAM_URL_LIMIT:
        deps.tg.send_message(
            deps.owner_chat_id,
            f"{caption} is too large to preview inline. Watch it here: {url}",
            buttons,
        )
    else:
        deps.tg.send_video(deps.owner_chat_id, url, caption, buttons)


def handle(event: dict, deps: Deps) -> dict:
    for record in event.get("Records", []):
        try:
            _handle_record(record, deps)
        except Exception as e:
            # Re-raise so S3's async Lambda invoke retries; log type only.
            log.error("notify failed: %s", type(e).__name__)
            raise
    return {"ok": True}


def _ssm(name: str) -> str:
    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


_deps: Deps | None = None


def _build_deps() -> Deps:
    s3 = boto3.client("s3")
    return Deps(
        store=Store(os.environ["TABLE"]),
        tg=TelegramClient(
            _ssm(os.environ.get("TELEGRAM_TOKEN_PARAM", "/seriesmaker/telegram_token"))
        ),
        owner_chat_id=int(
            _ssm(os.environ.get("OWNER_CHAT_ID_PARAM", "/seriesmaker/owner_chat_id"))
        ),
        presign=lambda b, k: s3.generate_presigned_url(
            "get_object", Params={"Bucket": b, "Key": k}, ExpiresIn=PRESIGN_SECONDS
        ),
        size=lambda b, k: s3.head_object(Bucket=b, Key=k)["ContentLength"],
    )


def lambda_handler(event: dict, context) -> dict:
    global _deps
    if _deps is None:
        _deps = _build_deps()
    return handle(event, _deps)
