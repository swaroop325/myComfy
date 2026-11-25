"""Worker loop: poll SQS, dispatch jobs, handle failures."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Mapping

import requests

from seriesmaker.models import EpisodeState, Job
from seriesmaker.worker import pipeline
from seriesmaker.worker.llm import LlmUnavailable

log = logging.getLogger("seriesmaker.worker")

HEARTBEAT_S = 60
VISIBILITY_S = 900  # keep comfortably above LLM_TIMEOUT_S (default 600)
RETRY_S = 60
# Must equal maxReceiveCount of the jobs queue redrive policy (terraform/app.tf).
MAX_RECEIVES = int(os.environ.get("MAX_RECEIVES", "3"))
DEFAULT_LLM_TIMEOUT_S = 600
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 60


def _run_episode(job, deps):
    return pipeline.run_episode_job(job, deps)


DEFAULT_HANDLERS = {
    "chat": lambda job, deps: pipeline.run_chat_job(job, deps),
    "episode": _run_episode,
    "edit": _run_episode,
}


def _notify(deps, job: Job, text: str) -> None:
    try:
        deps.telegram.send_message(deps.owner_chat_id or job.chat_id, text)
    except Exception as e:  # a failed notification must not mask the job outcome
        log.warning("owner notification failed: %s", type(e).__name__)


def _reset_generating(deps, job: Job) -> None:
    number = (job.payload or {}).get("number")
    if not job.series_id or number is None:
        return
    try:
        ep = deps.store.get_episode(job.series_id, number)
        if ep is not None and ep.state == EpisodeState.GENERATING:
            deps.store.set_state(job.series_id, number, EpisodeState.DRAFT)
    except Exception as e:
        log.warning("episode reset failed: %s", type(e).__name__)


def _heartbeat(queue, deps, receipt: str, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        try:
            deps.store.touch_activity()
        except Exception as e:
            log.warning("heartbeat failed: %s", type(e).__name__)
        try:
            queue.extend(receipt, VISIBILITY_S)
        except Exception as e:
            log.warning("visibility extend failed: %s", type(e).__name__)


def _retry_soon(queue, receipt: str) -> None:
    try:
        queue.extend(receipt, RETRY_S)
    except Exception as e:
        log.warning("visibility extend failed: %s", type(e).__name__)


def _is_permanent(status) -> bool:
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def _give_up(queue, deps, job: Job, receipt: str, count: int, reason: str) -> None:
    """Final attempt failed: reset the episode, tell the owner how to recover, drop the message.

    Deleting (instead of letting SQS move it to the DLQ) keeps the series recoverable
    with a plain /episode.
    """
    _reset_generating(deps, job)
    again = "Send your message again to retry." if job.type == "chat" else "Send /episode to try again"
    _notify(deps, job, f"I gave up on this one after {count} tries: {reason}. {again}")
    queue.delete(receipt)


def process_one(queue, deps, handlers=None, heartbeat_s: float = HEARTBEAT_S) -> bool:
    got = queue.receive(wait_s=20)
    if got is None:
        return False
    job, receipt = got
    count = getattr(got, "receive_count", 1)
    final = count >= MAX_RECEIVES
    handlers = handlers or DEFAULT_HANDLERS
    deps.store.touch_activity()
    stop = threading.Event()
    hb = threading.Thread(
        target=_heartbeat, args=(queue, deps, receipt, stop, heartbeat_s), daemon=True
    )
    hb.start()

    def stop_heartbeat() -> None:
        stop.set()
        hb.join(timeout=5)

    try:
        handler = handlers.get(job.type)
        if handler is None:
            _notify(deps, job, f"Dropping job with unknown type: {job.type}")
            queue.delete(receipt)
            return True
        try:
            handler(job, deps)
        except LlmUnavailable:
            stop_heartbeat()
            if final:
                _give_up(queue, deps, job, receipt, count, "LlmUnavailable")
                return True
            _notify(deps, job, "The model isn't ready yet, I'll retry shortly")
            _retry_soon(queue, receipt)
            return True
        except requests.HTTPError as e:
            stop_heartbeat()
            status = getattr(e.response, "status_code", None)
            log.error("job %s failed: HTTPError %s", job.type, status)
            if _is_permanent(status):
                _reset_generating(deps, job)
                _notify(deps, job, f"A request failed with HTTP {status}.")
                queue.delete(receipt)
            elif final:
                _give_up(queue, deps, job, receipt, count, f"HTTPError {status or 'unknown'}")
            else:
                # Episode stays GENERATING so /episode does not start a duplicate.
                _notify(deps, job, f"A request failed with HTTP {status if status else 'unknown'}.")
                _retry_soon(queue, receipt)
            return True
        except Exception as e:
            stop_heartbeat()
            log.error("job %s failed: %s (attempt %d/%d)", job.type, type(e).__name__,
                      count, MAX_RECEIVES)
            if final:
                _give_up(queue, deps, job, receipt, count, type(e).__name__)
                return True
            # Episode stays GENERATING so /episode does not start a duplicate.
            _notify(deps, job, f"Job failed: {type(e).__name__}. I'll retry.")
            _retry_soon(queue, receipt)
            return True
        except BaseException:
            # SIGTERM (SystemExit) / Ctrl-C: leave the message for redelivery.
            stop_heartbeat()
            abort_job(deps, job)
            raise
        queue.delete(receipt)
        return True
    finally:
        stop_heartbeat()
        try:
            deps.store.touch_activity()
        except Exception as e:
            log.warning("touch_activity failed: %s", type(e).__name__)


def abort_job(deps, job: Job) -> None:
    """Shutdown path: reset a GENERATING episode, tell the owner, keep the message."""
    _reset_generating(deps, job)
    _notify(deps, job, "Worker is shutting down; the job will be retried.")


def run_loop(queue, deps, sleeper=time.sleep, should_stop=lambda: False) -> None:
    delay = BACKOFF_MIN_S
    while not should_stop():
        try:
            process_one(queue, deps)
            delay = BACKOFF_MIN_S
        except Exception as e:
            log.error("loop error: %s", type(e).__name__)
            sleeper(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)


def build_deps(env: Mapping, ssm_client, s3_client) -> pipeline.Deps:
    from seriesmaker.store import Store
    from seriesmaker.telegram import TelegramClient
    from seriesmaker.worker.assemble import assemble_reel
    from seriesmaker.worker.comfy import ComfyClient
    from seriesmaker.worker.llm import LlmClient
    from seriesmaker.worker.tts import synthesize

    def param(key: str) -> str:
        return ssm_client.get_parameter(Name=env[key], WithDecryption=True)["Parameter"]["Value"]

    # LlmClient appends /v1/chat/completions itself.
    base = env.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return pipeline.Deps(
        store=Store(env["TABLE"]),
        telegram=TelegramClient(param("TELEGRAM_TOKEN_PARAM")),
        llm=LlmClient(
            base,
            env.get("LLM_MODEL", "llama3.3:70b"),
            timeout=int(env.get("LLM_TIMEOUT_S", DEFAULT_LLM_TIMEOUT_S)),
        ),
        bucket=env["BUCKET"],
        s3=s3_client,
        workdir=env.get("WORKDIR", "/var/lib/seriesmaker"),
        comfy=ComfyClient(env.get("COMFY_URL", "http://127.0.0.1:8188")),
        tts=synthesize,
        assemble=assemble_reel,
        owner_chat_id=int(param("OWNER_CHAT_ID_PARAM")),
    )


def startup(deps) -> None:
    """Mark the box as adopted: the reaper keeps a never-touched box only during the
    bootstrap grace, so the first activity record must exist as soon as the worker runs."""
    try:
        deps.store.touch_activity()
    except Exception as e:
        log.warning("startup touch_activity failed: %s", type(e).__name__)


def main() -> None:  # pragma: no cover - wiring only
    import boto3

    from seriesmaker.jobs import JobQueue

    logging.basicConfig(level=logging.INFO)

    def _term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _term)
    deps = build_deps(os.environ, boto3.client("ssm"), boto3.client("s3"))
    startup(deps)
    run_loop(JobQueue(os.environ["QUEUE_URL"]), deps)


if __name__ == "__main__":  # pragma: no cover
    main()
