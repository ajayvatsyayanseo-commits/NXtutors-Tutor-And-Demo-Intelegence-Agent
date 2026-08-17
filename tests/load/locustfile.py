"""Load profiles for the ingress endpoint.

Run against a deployed environment, never against production:

    uv run locust -f tests/load/locustfile.py \\
        --host https://<ingress-host> \\
        --users 50 --spawn-rate 5 --run-time 5m \\
        SteadyParent BurstArrival DuplicateStorm

Each class is one of the profiles §36 asks for. They are separate classes
rather than one weighted user because the interesting numbers are *per
profile*: a duplicate storm that looks fine mixed into steady traffic is what
hides an idempotency regression.

What to read afterwards, and the thresholds the SLOs are set from
(docs/production-control-matrix.md, control 15):

    ApproximateAgeOfOldestMessage   queue wait, reported separately from
                                    processing time — the number that tells you
                                    concurrency is too low
    DatabaseConnections             on the RDS Proxy, against the pool ceiling
    Throttles                       on the match worker; non-zero means
                                    reserved concurrency is the binding limit
    StageCandidateSqlMs p95         the hot query
    LlmCostMicros                   cost per completed match

Concurrency is chosen from *this* evidence, not from what AWS permits: the
match worker's reserved concurrency has to be low enough that
`concurrency × pool_size` stays under the proxy's connection ceiling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import time
import uuid

try:
    from locust import HttpUser, between, constant_pacing, events, task
except ImportError as exc:  # pragma: no cover - locust is not a runtime dependency
    raise SystemExit(
        "locust is not installed. It is deliberately not a project dependency — "
        "install it only where you run load tests:  uv pip install locust"
    ) from exc

SIGNING_KEY = os.getenv("TMM_INGRESS_SIGNING_KEY", "")
PATH = "/ingress"

SUBJECTS = ("maths", "physics", "chemistry", "biology", "english", "science")
CLASSES = ("class 8", "class 9", "class 10", "class 11", "class 12")
BOARDS = ("cbse", "icse", "state board")
LOCALITIES = ("sector 57 gurgaon", "sector 14 gurgaon", "dwarka delhi", "noida sector 62")


def _message() -> str:
    return (
        f"need {random.choice(CLASSES)} {random.choice(BOARDS)} "
        f"{random.choice(SUBJECTS)} tutor near {random.choice(LOCALITIES)}, "
        "home tuition, after 6pm"
    )


def _signed_headers(body: bytes) -> dict[str, str]:
    """Mirror `security/signing.py`. A load test that skips auth measures 401s."""
    timestamp = str(int(time.time()))
    payload = b"POST\n" + PATH.encode() + b"\n" + timestamp.encode() + b"\n" + body
    signature = hmac.new(SIGNING_KEY.encode(), payload, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Nxt-Timestamp": timestamp,
        "X-Nxt-Signature": signature,
        "X-Nxt-Agent": "load-test",
        "X-Trace-Id": uuid.uuid4().hex,
    }


def _envelope(conversation: str, message_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "event_id": message_id,
            "conversation_id": conversation,
            "provider_message_id": message_id,
            "text": text,
        }
    ).encode()


class _Base(HttpUser):
    abstract = True

    def _post(self, conversation: str, message_id: str, text: str, name: str) -> None:
        body = _envelope(conversation, message_id, text)
        with self.client.post(
            PATH,
            data=body,
            headers=_signed_headers(body),
            name=name,
            catch_response=True,
        ) as response:
            # 202 accepted and 429 rate-limited are both *correct* answers under
            # load. Counting a 429 as a failure would make the graph say the
            # service broke when in fact a control worked.
            if response.status_code in (202, 429):
                response.success()
            else:
                response.failure(f"unexpected {response.status_code}")


class SteadyParent(_Base):
    """Normal traffic: distinct conversations, a few turns each."""

    weight = 6
    wait_time = between(3, 12)

    def on_start(self) -> None:
        self.conversation = f"load:{uuid.uuid4().hex[:12]}"
        self.turn = 0

    @task
    def send_turn(self) -> None:
        self.turn += 1
        self._post(self.conversation, f"{self.conversation}:{self.turn}", _message(), "steady")


class BurstArrival(_Base):
    """Many distinct conversations arriving at once — a campaign send."""

    weight = 2
    wait_time = constant_pacing(1.0)

    @task
    def send_new_conversation(self) -> None:
        conversation = f"burst:{uuid.uuid4().hex[:12]}"
        self._post(conversation, f"{conversation}:1", _message(), "burst")


class HotConversation(_Base):
    """One conversation, messages faster than a human types.

    This is the profile that exercises FIFO ordering and the per-conversation
    rate limit. Expect a high share of 429s; that is the control working.
    """

    weight = 1
    wait_time = constant_pacing(0.2)

    def on_start(self) -> None:
        self.conversation = "load:hot-conversation"
        self.turn = 0

    @task
    def hammer(self) -> None:
        self.turn += 1
        self._post(self.conversation, f"hot:{self.turn}", _message(), "hot_conversation")


class DuplicateStorm(_Base):
    """The same provider_message_id, over and over.

    Meta redelivers; SQS is at-least-once. The service must answer once. After
    the run, assert `match_decision` holds exactly one row for the conversation
    — the count is the test, the latency is incidental.
    """

    weight = 1
    wait_time = constant_pacing(0.5)

    def on_start(self) -> None:
        self.conversation = f"dupe:{uuid.uuid4().hex[:12]}"
        self.message_id = f"{self.conversation}:fixed"
        self.text = _message()

    @task
    def redeliver(self) -> None:
        self._post(self.conversation, self.message_id, self.text, "duplicate")


class MalformedTraffic(_Base):
    """Garbage input. Must be rejected cheaply and never reach a worker."""

    weight = 1
    wait_time = between(1, 3)

    @task
    def oversized(self) -> None:
        body = json.dumps({"text": "x" * 200_000, "conversation_id": "load:big"}).encode()
        with self.client.post(
            PATH,
            data=body,
            headers=_signed_headers(body),
            name="malformed:oversized",
            catch_response=True,
        ) as response:
            if response.status_code in (401, 413, 422, 429):
                response.success()
            else:
                response.failure(f"oversized body returned {response.status_code}")

    @task
    def unsigned(self) -> None:
        body = _envelope("load:unsigned", "u1", _message())
        with self.client.post(
            PATH,
            data=body,
            headers={"Content-Type": "application/json"},
            name="malformed:unsigned",
            catch_response=True,
        ) as response:
            if response.status_code == 401:
                response.success()
            else:
                response.failure(f"unsigned request returned {response.status_code}")


@events.test_start.add_listener
def _warn_without_key(environment: object, **kwargs: object) -> None:  # pragma: no cover
    if not SIGNING_KEY:
        print(
            "WARNING: TMM_INGRESS_SIGNING_KEY is unset. Every request will be "
            "rejected with 401 and the run will measure the auth path only."
        )
