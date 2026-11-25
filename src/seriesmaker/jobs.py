"""SQS job queue wrapper."""

import boto3

from seriesmaker.models import Job


class Received(tuple):
    """(job, receipt) pair that also carries SQS's ApproximateReceiveCount.

    Still unpacks as ``job, receipt = got`` so existing callers keep working.
    """

    receive_count: int

    def __new__(cls, job: Job, receipt: str, receive_count: int = 1):
        obj = super().__new__(cls, (job, receipt))
        obj.receive_count = receive_count
        return obj


class JobQueue:
    def __init__(self, queue_url: str):
        self._url = queue_url
        self._sqs = boto3.client("sqs")

    def send(self, job: Job) -> None:
        self._sqs.send_message(QueueUrl=self._url, MessageBody=job.to_json())

    def receive(self, wait_s: int = 20) -> Received | None:
        """Long-poll for one job. Returns Received(job, receipt) or None.

        An undecodable body is a poison message: it is deleted and skipped.
        """
        resp = self._sqs.receive_message(
            QueueUrl=self._url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_s,
            AttributeNames=["ApproximateReceiveCount"],
        )
        msgs = resp.get("Messages") or []
        if not msgs:
            return None
        m = msgs[0]
        try:
            count = int((m.get("Attributes") or {}).get("ApproximateReceiveCount", 1))
        except (TypeError, ValueError):
            count = 1
        try:
            return Received(Job.from_json(m["Body"]), m["ReceiptHandle"], count)
        except (ValueError, TypeError, KeyError):
            self.delete(m["ReceiptHandle"])
            return None

    def delete(self, receipt: str) -> None:
        self._sqs.delete_message(QueueUrl=self._url, ReceiptHandle=receipt)

    def extend(self, receipt: str, seconds: int) -> None:
        self._sqs.change_message_visibility(
            QueueUrl=self._url, ReceiptHandle=receipt, VisibilityTimeout=seconds
        )

    def depth(self) -> int:
        """Visible plus in-flight messages."""
        attrs = self._sqs.get_queue_attributes(
            QueueUrl=self._url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return int(attrs["ApproximateNumberOfMessages"]) + int(
            attrs["ApproximateNumberOfMessagesNotVisible"]
        )
