from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from seriesmaker.models import (
    Episode,
    EpisodeExists,
    EpisodeState,
    InvalidTransition,
    StoryBible,
    StyleProfile,
    transition,
)

UPDATE_TTL_SECONDS = 24 * 3600


class Store:
    """Single-table DynamoDB store; keys are pk/sk strings."""

    def __init__(self, table_name: str):
        self._t = boto3.resource("dynamodb").Table(table_name)

    # messages
    def put_message(self, chat_id: int, role: str, text: str) -> None:
        self._t.put_item(
            Item={
                "pk": f"CHAT#{chat_id}",
                "sk": f"MSG#{time.time_ns():020d}-{secrets.token_hex(2)}",
                "role": role,
                "text": text,
            }
        )

    def recent_messages(self, chat_id: int, limit: int = 30) -> list[dict]:
        resp = self._t.query(
            KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
            ExpressionAttributeValues={":p": f"CHAT#{chat_id}", ":s": "MSG#"},
            ScanIndexForward=False,
            Limit=limit,
        )
        items = list(reversed(resp["Items"]))
        return [{"role": i["role"], "text": i["text"]} for i in items]

    # bible
    def get_bible(self, series_id: str) -> StoryBible | None:
        item = self._t.get_item(Key={"pk": f"SERIES#{series_id}", "sk": "BIBLE"}).get("Item")
        if not item:
            return None
        d = json.loads(item["data"])
        d["style"] = StyleProfile(**d["style"])
        return StoryBible(**d)

    def put_bible(self, b: StoryBible) -> None:
        self._t.put_item(
            Item={
                "pk": f"SERIES#{b.series_id}",
                "sk": "BIBLE",
                "data": json.dumps(asdict(b)),
            }
        )

    # episodes
    @staticmethod
    def _ep_key(series_id: str, number: int) -> dict:
        return {"pk": f"SERIES#{series_id}", "sk": f"EP#{number:04d}"}

    @staticmethod
    def _to_episode(item: dict) -> Episode:
        script = item.get("script")
        return Episode(
            series_id=item["series_id"],
            number=int(item["number"]),
            state=EpisodeState(item["state"]),
            script=json.loads(script) if script else None,
            s3_prefix=item["s3_prefix"],
            updated_at=int(item["updated_at"]) if item.get("updated_at") is not None else None,
            generation=int(item.get("generation", 0)),
        )

    def create_episode(self, series_id: str, number: int) -> Episode:
        now = int(time.time())
        ep = Episode(
            series_id, number, EpisodeState.DRAFT, None, f"{series_id}/ep{number:04d}/", now
        )
        try:
            self._t.put_item(
                Item={
                    **self._ep_key(series_id, number),
                    "series_id": series_id,
                    "number": number,
                    "state": ep.state.value,
                    "s3_prefix": ep.s3_prefix,
                    "updated_at": now,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise EpisodeExists(f"{series_id}/{number}") from e
            raise
        return ep

    def latest_episode(self, series_id: str) -> Episode | None:
        resp = self._t.query(
            KeyConditionExpression="pk = :p AND begins_with(sk, :s)",
            ExpressionAttributeValues={":p": f"SERIES#{series_id}", ":s": "EP#"},
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp["Items"]
        return self._to_episode(items[0]) if items else None

    def next_episode_number(self, series_id: str) -> int:
        latest = self.latest_episode(series_id)
        return latest.number + 1 if latest else 1

    def set_script(self, series_id: str, number: int, script: dict) -> None:
        try:
            self._t.update_item(
                Key=self._ep_key(series_id, number),
                UpdateExpression="SET #script = :s",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#script": "script"},
                ExpressionAttributeValues={":s": json.dumps(script)},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise InvalidTransition(f"no episode {series_id}/{number}") from e
            raise

    def get_episode(self, series_id: str, number: int) -> Episode | None:
        item = self._t.get_item(Key=self._ep_key(series_id, number)).get("Item")
        return self._to_episode(item) if item else None

    def set_state(self, series_id: str, number: int, target: EpisodeState) -> Episode:
        ep = self.get_episode(series_id, number)
        if ep is None:
            raise InvalidTransition(f"no episode {series_id}/{number}")
        transition(ep.state, target)
        try:
            resp = self._t.update_item(
                Key=self._ep_key(series_id, number),
                UpdateExpression="SET #s = :t, updated_at = :now",
                ConditionExpression="#s = :cur",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={
                    ":t": target.value,
                    ":cur": ep.state.value,
                    ":now": int(time.time()),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise InvalidTransition(f"stale state for {series_id}/{number}") from e
            raise
        return self._to_episode(resp["Attributes"])

    def bump_generation(self, series_id: str, number: int) -> int:
        """Atomically increment the episode's generation counter; returns the new value."""
        try:
            resp = self._t.update_item(
                Key=self._ep_key(series_id, number),
                UpdateExpression="ADD #g :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#g": "generation"},
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise InvalidTransition(f"no episode {series_id}/{number}") from e
            raise
        return int(resp["Attributes"]["generation"])

    # dedupe
    def seen_update(self, update_id: int) -> bool:
        try:
            self._t.put_item(
                Item={
                    "pk": f"UPDATE#{update_id}",
                    "sk": "SEEN",
                    "ttl": int(time.time()) + UPDATE_TTL_SECONDS,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            return False
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return True
            raise

    # activity
    def touch_activity(self) -> None:
        self._t.put_item(
            Item={
                "pk": "META",
                "sk": "ACTIVITY",
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def last_activity(self) -> datetime | None:
        item = self._t.get_item(Key={"pk": "META", "sk": "ACTIVITY"}).get("Item")
        return datetime.fromisoformat(item["at"]) if item else None

    # META/<NAME> epoch markers: keep-awake HOLD and reaper bookkeeping
    def set_marker(self, name: str, epoch: int) -> None:
        self._t.put_item(Item={"pk": "META", "sk": name, "at": int(epoch)})

    def get_marker(self, name: str) -> int | None:
        item = self._t.get_item(Key={"pk": "META", "sk": name}).get("Item")
        return int(item["at"]) if item and item.get("at") is not None else None

    def set_hold(self, until_epoch: int) -> None:
        """Keep the GPU box awake until until_epoch (scripts/keep_awake.sh writes the same item)."""
        self.set_marker("HOLD", until_epoch)

    def hold_active(self, now: float | datetime | None = None) -> bool:
        if now is None:
            now = time.time()
        elif isinstance(now, datetime):
            now = now.timestamp()
        until = self.get_marker("HOLD")
        return until is not None and until > now

    # publish lease
    def publish_lease_until(self, series_id: str, number: int) -> int | None:
        item = self._t.get_item(Key=self._ep_key(series_id, number)).get("Item")
        until = item.get("publish_lease_until") if item else None
        return int(until) if until is not None else None

    def acquire_publish_lease(self, series_id: str, number: int, lease_s: int = 900) -> None:
        """Claim the right to attempt a publish; expires on its own after lease_s."""
        now = int(time.time())
        try:
            self._t.update_item(
                Key=self._ep_key(series_id, number),
                UpdateExpression="SET publish_lease_until = :u",
                ConditionExpression=(
                    "attribute_exists(pk) AND "
                    "(attribute_not_exists(publish_lease_until) OR publish_lease_until < :now)"
                ),
                ExpressionAttributeValues={":u": now + lease_s, ":now": now},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                item = self._t.get_item(Key=self._ep_key(series_id, number)).get("Item")
                until = item.get("publish_lease_until") if item else None
                if until is None:
                    raise InvalidTransition("publish in progress") from e
                hhmm = datetime.fromtimestamp(int(until), timezone.utc).strftime("%H:%M")
                raise InvalidTransition(
                    f"publish in progress, try again after {hhmm} UTC"
                ) from e
            raise

    def release_publish_lease(self, series_id: str, number: int) -> None:
        """Free the lease after a handled failure so Retry works immediately."""
        self._t.update_item(
            Key=self._ep_key(series_id, number),
            UpdateExpression="REMOVE publish_lease_until",
        )

    # The bible is one JSON string attribute, so append_recap uses optimistic
    # concurrency: re-read and write conditionally on the exact string read,
    # retrying on conflict. Concurrent bible edits are never clobbered.
    def append_recap(self, series_id: str, recap: str, attempts: int = 8) -> None:
        key = {"pk": f"SERIES#{series_id}", "sk": "BIBLE"}
        for _ in range(attempts):
            item = self._t.get_item(Key=key, ConsistentRead=True).get("Item")
            if not item:
                return
            d = json.loads(item["data"])
            d.setdefault("recaps", []).append(recap)
            try:
                self._t.update_item(
                    Key=key,
                    UpdateExpression="SET #d = :new",
                    ConditionExpression="#d = :old",
                    ExpressionAttributeNames={"#d": "data"},
                    ExpressionAttributeValues={":new": json.dumps(d), ":old": item["data"]},
                )
                return
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise InvalidTransition("recap update conflict")
