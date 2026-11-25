"""Telegram webhook Lambda: auth, dedupe, route, enqueue, wake the GPU box."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3

from seriesmaker.jobs import JobQueue
from seriesmaker.models import EpisodeExists, EpisodeState, InvalidTransition, Job
from seriesmaker.power import InstanceControl
from seriesmaker.store import Store
from seriesmaker.worker.pipeline import series_id_for
from seriesmaker.telegram import TelegramClient, parse_update, verify_webhook

log = logging.getLogger(__name__)

PENDING_EDIT_ROLE = "pending_edit"
PENDING_EDIT_TTL = 30 * 60
# A GENERATING episode untouched for this long is presumed abandoned (worker died).
STALE_GENERATING_S = 60 * 60
_now = time.time
OK = {"statusCode": 200, "body": "ok"}


@dataclass
class Deps:
    store: Store
    queue: JobQueue
    power: InstanceControl
    tg: TelegramClient
    owner_chat_id: int
    secret: str
    lambda_client: Any = None
    publish_function: str | None = None


def _safe_send(fn, *args, **kwargs) -> None:
    """Telegram failures must never cause a 500 (Telegram would retry).

    Log only the exception type: messages could embed the bot token URL.
    """
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram call failed: %s", type(e).__name__)


def _wake(deps: Deps, chat_id: int) -> None:
    try:
        started = deps.power.ensure_running()
        stopping = (not started) and deps.power.state() == "stopping"
    except Exception as e:  # noqa: BLE001
        log.error("ensure_running failed: %s", type(e).__name__)
        return
    if started:
        _safe_send(deps.tg.send_message, chat_id, "Waking up, a few minutes")
    elif stopping:
        _safe_send(
            deps.tg.send_message,
            chat_id,
            "Shutting down, I'll restart in a few minutes and answer then",
        )


def _find_pending_edit(deps: Deps, chat_id: int) -> tuple[str, int] | None:
    """Most recent unconsumed pending edit among the last 10 messages."""
    for m in reversed(deps.store.recent_messages(chat_id, limit=10)):
        if m["role"] == "user":
            return None  # a later user message already consumed it
        if m["role"] != PENDING_EDIT_ROLE:
            continue
        try:
            series_id, number, ts = m["text"].rsplit(":", 2)
            if _now() - float(ts) > PENDING_EDIT_TTL:
                return None
            return series_id, int(number)
        except (ValueError, TypeError):
            return None
    return None


def _claim_generation(deps: Deps, chat_id: int, series_id: str, number: int) -> bool:
    """Move the episode to GENERATING; a double tap raises and is rejected."""
    try:
        deps.store.set_state(series_id, number, EpisodeState.GENERATING)
        return True
    except InvalidTransition:
        ep = deps.store.get_episode(series_id, number)
        why = f"state: {ep.state.value}" if ep else "not found"
        _safe_send(
            deps.tg.send_message,
            chat_id,
            f"That episode can't be regenerated right now ({why})",
        )
        return False


COMMANDS = ("/episode", "/retry")


def _command(text: str) -> str | None:
    """'/episode' or '/retry' (optionally '@botname'), else None."""
    t = text.strip()
    for c in COMMANDS:
        if t == c or (t.startswith(c + "@") and " " not in t):
            return c
    return None


def _is_stale(ep, now: float) -> bool:
    return ep.updated_at is None or now - ep.updated_at > STALE_GENERATING_S


def _start_generation(deps: Deps, chat_id: int, sid: str, number: int, msg: str) -> None:
    """Claim the episode (DRAFT -> GENERATING) before enqueueing so a quick second
    /episode is blocked instead of queueing a duplicate job."""
    try:
        deps.store.set_state(sid, number, EpisodeState.GENERATING)
    except InvalidTransition:
        _safe_send(deps.tg.send_message, chat_id, f"Episode {number} is already being made.")
        return
    deps.queue.send(Job("episode", chat_id, sid, {"number": number, "instruction": None}))
    _safe_send(deps.tg.send_message, chat_id, msg)
    _wake(deps, chat_id)


def _handle_episode_command(deps: Deps, chat_id: int) -> None:
    send = deps.tg.send_message
    sid = series_id_for(chat_id)
    if deps.store.get_bible(sid) is None:
        _safe_send(send, chat_id, "No story yet. Tell me a topic and we'll brainstorm one first.")
        return
    latest = deps.store.latest_episode(sid)
    if latest and latest.state == EpisodeState.DRAFT:
        # A failed (given-up) episode: retry the same number.
        _start_generation(deps, chat_id, sid, latest.number, f"Retrying episode {latest.number}")
        return
    if latest and latest.state == EpisodeState.GENERATING and _is_stale(latest, _now()):
        try:
            deps.store.set_state(sid, latest.number, EpisodeState.DRAFT)
        except InvalidTransition:
            _safe_send(send, chat_id, f"Episode {latest.number} changed state, try again.")
            return
        _start_generation(deps, chat_id, sid, latest.number, f"Retrying episode {latest.number}")
        return
    if latest and latest.state not in (EpisodeState.POSTED, EpisodeState.REJECTED):
        hint = " If posting failed, send /retry." if latest.state == EpisodeState.APPROVED else ""
        _safe_send(
            send,
            chat_id,
            f"Episode {latest.number} is still {latest.state.value}. "
            f"Approve/reject it before starting the next one.{hint}",
        )
        return
    number = deps.store.next_episode_number(sid)
    try:
        deps.store.create_episode(sid, number)
    except EpisodeExists:
        _safe_send(send, chat_id, f"Episode {number} already exists, try again in a moment.")
        return
    _start_generation(deps, chat_id, sid, number, f"Starting episode {number}")


def _handle_retry_command(deps: Deps, chat_id: int) -> None:
    """Re-run publishing for the latest APPROVED episode (e.g. the publish Lambda was killed)."""
    latest = deps.store.latest_episode(series_id_for(chat_id))
    if latest is None or latest.state != EpisodeState.APPROVED:
        why = f"latest episode is {latest.state.value}" if latest else "no episodes yet"
        _safe_send(deps.tg.send_message, chat_id, f"Nothing to retry ({why})")
        return
    _invoke_publish(deps, chat_id, latest.series_id, latest.number, True, "Retrying the post…")


def _handle_text(deps: Deps, chat_id: int, text: str) -> None:
    cmd = _command(text)
    if cmd == "/episode":
        _handle_episode_command(deps, chat_id)
        return
    if cmd == "/retry":
        _handle_retry_command(deps, chat_id)
        return
    pending = _find_pending_edit(deps, chat_id)
    deps.store.put_message(chat_id, "user", text)
    if pending:
        series_id, number = pending
        if not _claim_generation(deps, chat_id, series_id, number):
            return
        job = Job("edit", chat_id, series_id, {"number": number, "instruction": text})
    else:
        # series_id lets the chat prompt include the story bible and recaps.
        job = Job("chat", chat_id, series_id_for(chat_id), {"text": text})
    deps.queue.send(job)
    _wake(deps, chat_id)


def _not_ready(deps: Deps, chat_id: int, series_id: str, number: int, verb: str) -> bool:
    """Reply and return True unless the episode is in review."""
    ep = deps.store.get_episode(series_id, number)
    if ep is None:
        _safe_send(deps.tg.send_message, chat_id, f"Episode {number} not found")
        return True
    if ep.state != EpisodeState.IN_REVIEW:
        _safe_send(
            deps.tg.send_message,
            chat_id,
            f"That episode can't be {verb} right now (state: {ep.state.value})",
        )
        return True
    return False


def _invoke_publish(
    deps: Deps, chat_id: int, series_id: str, number: int, retry: bool, ok_msg: str
) -> None:
    """Fire-and-forget the publish Lambda (posting can take ~10 minutes)."""
    if not deps.publish_function or deps.lambda_client is None:
        _safe_send(deps.tg.send_message, chat_id, "Publishing is not set up yet")
        return
    deps.lambda_client.invoke(
        FunctionName=deps.publish_function,
        InvocationType="Event",
        Payload=json.dumps({"series_id": series_id, "number": number, "retry": retry}).encode(),
    )
    _safe_send(deps.tg.send_message, chat_id, ok_msg)


def _handle_callback(deps: Deps, chat_id: int, data: str) -> None:
    try:
        action, series_id, num = data.split(":")
        number = int(num)
    except ValueError:
        return
    if action == "approve":
        if _not_ready(deps, chat_id, series_id, number, "approved"):
            return
        _invoke_publish(deps, chat_id, series_id, number, False, "Approved — posting now, I'll message you when it's done.")
    elif action == "retry":
        ep = deps.store.get_episode(series_id, number)
        if ep is None or ep.state != EpisodeState.APPROVED:
            why = f"state: {ep.state.value}" if ep else "not found"
            _safe_send(deps.tg.send_message, chat_id, f"That episode can't be retried right now ({why})")
            return
        _invoke_publish(deps, chat_id, series_id, number, True, "Retrying the post…")
    elif action == "reject":
        if _not_ready(deps, chat_id, series_id, number, "rejected"):
            return
        try:
            deps.store.set_state(series_id, number, EpisodeState.REJECTED)
            msg = f"Episode {number} rejected."
        except InvalidTransition:
            msg = "That episode can't be rejected right now"
        _safe_send(deps.tg.send_message, chat_id, msg)
    elif action == "regen":
        if not _claim_generation(deps, chat_id, series_id, number):
            return
        # regen: the worker bumps the episode's generation so the visuals change.
        deps.queue.send(
            Job("episode", chat_id, series_id,
                {"number": number, "instruction": None, "regen": True})
        )
        _wake(deps, chat_id)
    elif action == "edit":
        deps.store.put_message(
            chat_id, PENDING_EDIT_ROLE, f"{series_id}:{number}:{_now()}"
        )
        _safe_send(deps.tg.send_message, chat_id, "What should change?")


def _parse(event: dict):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    body = json.loads(raw)
    if not isinstance(body, dict):
        return None
    return parse_update(body)


def handle(event: dict, deps: Deps) -> dict:
    if not verify_webhook(event.get("headers") or {}, deps.secret):
        return {"statusCode": 403, "body": "forbidden"}
    try:
        update = _parse(event)
    except (KeyError, ValueError, TypeError, AttributeError):
        update = None
    if update is None or update.chat_id != deps.owner_chat_id:
        return OK
    try:
        if deps.store.seen_update(update.update_id):
            return OK
        if update.callback_id:
            _safe_send(deps.tg.answer_callback, update.callback_id)
        if update.callback_data:
            _handle_callback(deps, update.chat_id, update.callback_data)
        elif update.text:
            _handle_text(deps, update.chat_id, update.text)
    except Exception as e:  # noqa: BLE001 - never 500 to Telegram after auth
        log.error("update handling failed: %s", type(e).__name__)
        _safe_send(deps.tg.send_message, update.chat_id, "Something went wrong, try again")
    return OK


_deps: Deps | None = None


def _ssm(name: str) -> str:
    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _build_deps() -> Deps:
    return Deps(
        store=Store(os.environ["TABLE"]),
        queue=JobQueue(os.environ["QUEUE_URL"]),
        power=InstanceControl(os.environ["INSTANCE_ID"]),
        # Short timeout: the webhook must answer well within API Gateway's 30 s.
        tg=TelegramClient(_ssm(os.environ["TELEGRAM_TOKEN_PARAM"]), timeout=5),
        owner_chat_id=int(_ssm(os.environ["OWNER_CHAT_ID_PARAM"])),
        secret=_ssm(os.environ["WEBHOOK_SECRET_PARAM"]),
        lambda_client=boto3.client("lambda"),
        publish_function=os.environ.get("PUBLISH_FUNCTION"),
    )


def lambda_handler(event: dict, context) -> dict:
    global _deps
    if _deps is None:  # resolved once per cold start
        _deps = _build_deps()
    return handle(event, _deps)
