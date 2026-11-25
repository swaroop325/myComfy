"""Scheduled Lambda (every 5 min) that stops the GPU instance when it is idle or stuck.

Decision order for a running instance:
1. keep-awake HOLD active (scripts/keep_awake.sh) -> keep.
2. no activity ever recorded (worker never adopted the box): keep during the
   bootstrap grace after launch, then stop and tell the owner.
3. baseline = max(last_activity, launch_time). Queued/in-flight jobs keep the box up
   only while the baseline is fresher than stuck_after_min; past that the queue is
   "not draining": stop and tell the owner once.
4. otherwise stop after idle_minutes past the baseline.

A stopped instance with queued jobs is restarted, at most once per RESTART_COOLDOWN.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from seriesmaker.jobs import JobQueue
from seriesmaker.power import InstanceControl
from seriesmaker.store import Store

log = logging.getLogger(__name__)

RESTART_COOLDOWN = timedelta(minutes=30)
NEVER_STARTED_MSG = (
    "Worker never started — stopped the GPU box to avoid charges; see runbook troubleshooting"
)
STUCK_MSG = "Queue not draining; stopped the GPU box. See runbook troubleshooting."


@dataclass
class Deps:
    queue: JobQueue
    power: InstanceControl
    store: Store
    idle_minutes: int
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    bootstrap_grace_min: int = 90
    stuck_after_min: int = 20
    notify: Callable[[str], None] | None = None


def _notify(deps: Deps, text: str) -> None:
    if deps.notify is None:
        return
    try:
        deps.notify(text)
    except Exception as e:  # noqa: BLE001 - a failed notice must not keep the box up
        log.warning("owner notice failed: %s", type(e).__name__)


def _mins(d: timedelta) -> int:
    return int(d.total_seconds() // 60)


def _stopped_with_jobs(deps: Deps, depth: int, now: datetime) -> dict:
    last = deps.store.get_marker("LAST_RESTART")
    if last is not None and now.timestamp() - last < RESTART_COOLDOWN.total_seconds():
        log.info("restart cooldown: %d job(s) queued, last restart %ds ago",
                 depth, int(now.timestamp() - last))
        return {"action": "kept", "reason": "restart cooldown (stopped, jobs queued)"}
    deps.store.set_marker("LAST_RESTART", int(now.timestamp()))
    # A job arrived while the box was stopping; nothing else would restart it.
    deps.power.ensure_running()
    return {"action": "started", "reason": f"{depth} job(s) queued on stopped instance"}


def handle(deps: Deps) -> dict:
    now = deps.now()
    state = deps.power.state()
    depth = deps.queue.depth()
    if state == "stopped" and depth > 0:
        return _stopped_with_jobs(deps, depth, now)
    if state != "running":
        return {"action": "kept", "reason": f"instance is {state}"}
    if deps.store.hold_active(now.timestamp()):
        return {"action": "kept", "reason": "keep-awake hold active"}

    last = deps.store.last_activity()
    launched = deps.power.launch_time()
    if last is None:
        # First boot: user_data provisioning runs for a long time before the worker
        # ever starts. Stopping now would kill the bootstrap for good.
        age = now - launched
        if age < timedelta(minutes=deps.bootstrap_grace_min):
            return {"action": "kept", "reason": "not adopted yet"}
        deps.power.stop()
        _notify(deps, NEVER_STARTED_MSG)
        return {"action": "stopped", "reason": f"worker never started ({_mins(age)}m since launch)"}

    # Latest of activity and launch: a freshly started box with stale activity stays up.
    baseline = max(last, launched)
    idle = now - baseline
    if depth > 0:
        if idle < timedelta(minutes=deps.stuck_after_min):
            return {"action": "kept", "reason": "jobs in queue or in flight"}
        deps.power.stop()
        notice = deps.store.get_marker("STUCK_NOTICE")
        if notice is None or notice < last.timestamp():  # once per stuck episode
            _notify(deps, STUCK_MSG)
            deps.store.set_marker("STUCK_NOTICE", int(now.timestamp()))
        return {"action": "stopped", "reason": f"queue not draining ({_mins(idle)}m no activity)"}
    if idle < timedelta(minutes=deps.idle_minutes):
        return {"action": "kept", "reason": f"active {_mins(idle)}m ago"}
    deps.power.stop()
    return {"action": "stopped", "reason": f"idle {_mins(idle)}m"}


def _owner_notifier() -> Callable[[str], None]:
    """Resolve Telegram credentials lazily: only when there is something to say."""

    def send(text: str) -> None:
        import boto3

        from seriesmaker.telegram import TelegramClient

        ssm = boto3.client("ssm")

        def p(name: str) -> str:
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]

        token = p(os.environ.get("TELEGRAM_TOKEN_PARAM", "/seriesmaker/telegram_token"))
        owner = int(p(os.environ.get("OWNER_CHAT_ID_PARAM", "/seriesmaker/owner_chat_id")))
        TelegramClient(token, timeout=10).send_message(owner, text)

    return send


def lambda_handler(event, context) -> dict:
    deps = Deps(
        queue=JobQueue(os.environ["QUEUE_URL"]),
        power=InstanceControl(os.environ["INSTANCE_ID"]),
        store=Store(os.environ["TABLE"]),
        idle_minutes=int(os.environ.get("IDLE_MINUTES", "15")),
        bootstrap_grace_min=int(os.environ.get("BOOTSTRAP_GRACE_MIN", "90")),
        stuck_after_min=int(os.environ.get("STUCK_AFTER_MIN", "20")),
        notify=_owner_notifier(),
    )
    result = handle(deps)
    log.info("reaper: %s (%s)", result["action"], result["reason"])
    return result
