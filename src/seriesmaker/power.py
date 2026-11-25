"""EC2 instance power control."""

from datetime import datetime

import boto3


class InstanceControl:
    def __init__(self, instance_id: str):
        self._id = instance_id
        self._ec2 = boto3.client("ec2")

    def _describe(self) -> dict:
        resp = self._ec2.describe_instances(InstanceIds=[self._id])
        return resp["Reservations"][0]["Instances"][0]

    def state(self) -> str:
        return self._describe()["State"]["Name"]

    def launch_time(self) -> datetime:
        return self._describe()["LaunchTime"]

    def stop(self) -> None:
        self._ec2.stop_instances(InstanceIds=[self._id])

    def ensure_running(self) -> bool:
        """Start the instance if stopped. True only if this call started it."""
        if self.state() == "stopped":
            self._ec2.start_instances(InstanceIds=[self._id])
            return True
        return False
