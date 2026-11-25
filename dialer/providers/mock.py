"""Two mock carriers with deliberately different personalities, plus a `World` that plays the borrowers."""
import heapq
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .base import ProviderCallInfo, ProviderError, ProviderEvent, ProviderTimeout, TelecomProvider


@dataclass
class CallPlan:
    answers: bool
    ring_s: float          # RINGING -> ANSWERED, or RINGING -> NO_ANSWER/BUSY
    talk_s: float
    fail_reason: str = "NO_ANSWER"


class World:
    """What borrowers do. Rates can be functions of time so scenario D ('changing') is easy."""

    def __init__(self, answer_rate, talk_mean, seed=1):
        self.answer_rate = answer_rate if callable(answer_rate) else (lambda t, v=answer_rate: v)
        self.talk_mean = talk_mean if callable(talk_mean) else (lambda t, v=talk_mean: v)
        self.rng = random.Random(seed)

    def plan(self, now: float) -> CallPlan:
        r = self.rng
        if r.random() < self.answer_rate(now):
            return CallPlan(True, r.uniform(4, 18), r.gammavariate(2.0, self.talk_mean(now) / 2.0))
        if r.random() < 0.2:
            return CallPlan(False, r.uniform(2, 6), 0, "BUSY")
        return CallPlan(False, r.uniform(25, 30), 0, "NO_ANSWER")


@dataclass
class _Call:
    plan: CallPlan
    t0: float
    timeline: List[tuple]     # (occurs_at, type, reason)
    cancelled: bool = False


class MockProvider(TelecomProvider):
    def __init__(self, name, world: World, clock, *, setup=(0.3, 1.0), reject_rate=0.0, timeout_rate=0.0,
                 timeout_places_call=0.5, dup_rate=0.0, jitter=0.0, drop_rate=0.0, seed=7):
        self.name = name
        self.world, self.clock = world, clock
        self.setup, self.reject_rate, self.timeout_rate = setup, reject_rate, timeout_rate
        self.timeout_places_call, self.dup_rate, self.jitter, self.drop_rate = timeout_places_call, dup_rate, jitter, drop_rate
        self.rng = random.Random(seed)
        self.calls: Dict[str, _Call] = {}
        self.outbox: list = []       # heap of (deliver_at, seq, ProviderEvent, occurs_at)
        self.seq = 0
        # chaos switches the simulator can flip at runtime
        self.outage = False          # originate/lookup raise ProviderTimeout; events stop flowing
        self.originate_count = 0

    # ---- chaos
    def set_outage(self, on: bool):
        self.outage = on

    # ---- interface
    def originate(self, call_id, phone):
        if self.outage:
            raise ProviderTimeout(f"{self.name} outage")
        now = self.clock.now()
        if call_id in self.calls:                      # idempotent
            return "ref-" + call_id
        if self.rng.random() < self.reject_rate:
            raise ProviderError(f"{self.name} rejected")
        will_timeout = self.rng.random() < self.timeout_rate
        if will_timeout and self.rng.random() > self.timeout_places_call:
            raise ProviderTimeout(f"{self.name} timeout (call NOT placed)")
        self._place(call_id, now)
        if will_timeout:
            raise ProviderTimeout(f"{self.name} timeout (call WAS placed)")
        return "ref-" + call_id

    def _place(self, call_id, now):
        self.originate_count += 1
        plan = self.world.plan(now)
        t = now + self.rng.uniform(*self.setup)
        tl = [(t, "RINGING", "")]
        t += plan.ring_s
        if plan.answers:
            tl += [(t, "ANSWERED", ""), (t + plan.talk_s, "COMPLETED", "")]
        else:
            tl.append((t, "FAILED", plan.fail_reason))
        self.calls[call_id] = _Call(plan, now, tl)
        for occurs, typ, reason in tl:
            self._emit(call_id, typ, reason, occurs)

    def _emit(self, call_id, typ, reason, occurs):
        if self.rng.random() < self.drop_rate:
            return
        copies = 2 if self.rng.random() < self.dup_rate else 1
        for i in range(copies):
            deliver = occurs + (self.rng.uniform(0, self.jitter) if self.jitter else 0) + i * self.rng.uniform(0.2, 2.0)
            self.seq += 1
            heapq.heappush(self.outbox, (deliver, self.seq, ProviderEvent(self.name, call_id, typ, reason, occurs), occurs))

    def cancel(self, call_id):
        c = self.calls.get(call_id)
        if self.outage or c is None:
            return
        now = self.clock.now()
        answered_at = next((t for t, ty, _ in c.timeline if ty == "ANSWERED"), None)
        if answered_at is not None and answered_at <= now:
            return                                      # too late: borrower already picked up
        self._truncate(call_id, now)
        c.cancelled = True
        self._emit(call_id, "FAILED", "CANCELLED", now)

    def hangup(self, call_id):
        c = self.calls.get(call_id)
        if self.outage or c is None:
            return
        now = self.clock.now()
        self._truncate(call_id, now)
        c.cancelled = True
        self._emit(call_id, "COMPLETED", "HANGUP", now)

    def _truncate(self, call_id, now):
        """Drop events that have not happened yet (but keep ones that happened and are merely delayed in transit)."""
        self.outbox = [e for e in self.outbox if not (e[2].call_id == call_id and e[3] > now)]
        heapq.heapify(self.outbox)
        c = self.calls[call_id]
        c.timeline = [x for x in c.timeline if x[0] <= now]

    def lookup(self, call_id) -> Optional[ProviderCallInfo]:
        if self.outage:
            raise ProviderTimeout(f"{self.name} lookup timeout")
        c = self.calls.get(call_id)
        if c is None:
            return None
        now = self.clock.now()
        past = [x for x in c.timeline if x[0] <= now]
        if not past:
            return ProviderCallInfo("RINGING") if not c.cancelled else ProviderCallInfo("FAILED", "CANCELLED")
        _, typ, reason = past[-1]
        return ProviderCallInfo(typ, reason)

    def poll_events(self, now) -> List[ProviderEvent]:
        if self.outage:
            return []                  # events pile up at the carrier; they are delivered after recovery
        out = []
        while self.outbox and self.outbox[0][0] <= now:
            out.append(heapq.heappop(self.outbox)[2])
        return out


def make_provider_a(world, clock, seed=11):
    """Fast, reliable, clean events."""
    return MockProvider("A", world, clock, setup=(0.3, 0.9), reject_rate=0.01, seed=seed)


def make_provider_b(world, clock, seed=13):
    """Slower, flaky: timeouts (half of them actually placed the call), duplicate + reordered + occasionally lost events."""
    return MockProvider("B", world, clock, setup=(1.5, 4.0), reject_rate=0.03, timeout_rate=0.06,
                        timeout_places_call=0.5, dup_rate=0.12, jitter=4.0, drop_rate=0.02, seed=seed)
