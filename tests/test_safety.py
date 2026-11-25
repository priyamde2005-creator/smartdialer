import threading

import pytest

from dialer.allocator import UnsafeApproval
from dialer.config import Config
from dialer.pacing.base import PacingRequest
from dialer.safety import APPROVE, FALLBACK, REDUCE, REJECT, Approval, SafetyController
from dialer.snapshot import Snapshot
from sim.harness import Harness


def snap(**kw):
    base = dict(now=1000.0, campaign_id="c1", active=True, agents={}, free=20, connected=60, wrap_up=5, wrap_soon=3, logged_in=100,
                pooled_unanswered=0, held=0, bound_inflight=0, pending_borrowers=5000, ans_n_long=200, ans_a_long=60,
                ans_n_short=30, ans_a_short=9, abandon_n=200, abandon_a=0, talk_mean=120.0, setup_mean=12.0, recent_offline=0)
    base.update(kw)
    return Snapshot(**base)


class FakeRouter:
    def __init__(self, usable=True, degraded=False, quota=None):
        self.u, self.d, self.q = usable, degraded, quota

    def usable(self): return self.u
    def degraded(self): return self.d
    def new_call_quota(self): return self.q


def ctl(**router):
    return SafetyController(Config(), FakeRouter(**router))


def pooled(n):
    return PacingRequest("predictive", n, True)


def test_safety_reduces_an_aggressive_request():
    d = ctl().review(pooled(500), snap(), 1000.0)
    assert d.action == REDUCE and 0 < d.approved < 500 and d.approval.pooled == d.approved


def test_safety_never_lets_total_pooled_exceed_its_own_ceiling():
    s = ctl()
    for demand in (0, 10, 30, 60, 200):
        d = s.review(pooled(500), snap(pooled_unanswered=demand), 1000.0)
        ceiling = d.explain["ceiling"]
        assert demand + d.approved <= max(ceiling, demand)


def test_cold_start_falls_back_to_progressive():
    d = ctl().review(pooled(50), snap(ans_n_long=5, ans_a_long=2), 1000.0)
    assert d.action == FALLBACK and d.approval.pooled == 0 and d.approval.bound <= 20 and d.reasons[0].startswith("COLD_START")


@pytest.mark.parametrize("kw,reason", [
    (dict(abandon_a=7), "ABANDON_BUDGET_EXHAUSTED"),
    (dict(recent_offline=30), "AGENT_AVAILABILITY_DROP"),
    (dict(setup_mean=40.0), "SLOW_CALL_SETUP"),
])
def test_fallback_conditions(kw, reason):
    d = ctl().review(pooled(50), snap(**kw), 1000.0)
    assert d.action == FALLBACK and any(reason in r for r in d.reasons)
    assert d.approval.pooled == 0          # only reserved-agent calls


def test_degraded_provider_forces_progressive():
    d = ctl(degraded=True).review(pooled(50), snap(), 1000.0)
    assert d.action == FALLBACK and "PROVIDER_DEGRADED" in d.reasons


def test_provider_down_rejects():
    d = ctl(usable=False).review(pooled(50), snap(), 1000.0)
    assert d.action == REJECT and d.approval is None and d.reasons == ["PROVIDER_DOWN"]


def test_probe_quota_limits_calls_while_provider_is_half_open():
    d = ctl(quota=2).review(PacingRequest("progressive", 10, False), snap(), 1000.0)
    assert d.approved == 2


def test_progressive_never_exceeds_free_agents():
    d = ctl().review(PacingRequest("progressive", 999, False), snap(free=12), 1000.0)
    assert d.approved == 12 and d.approval.bound == 12


def test_answer_rate_jump_shrinks_the_ceiling():
    """The nasty direction: borrowers suddenly answer far more than the long-run history says."""
    s = ctl()
    calm = s.safe_ceiling(snap(ans_a_long=40, ans_a_short=6))[0]
    jump = s.safe_ceiling(snap(ans_a_long=40, ans_a_short=24))[0]      # last 30 calls: 80% answered
    assert jump < calm


def test_abandon_budget_use_shrinks_the_ceiling():
    s = ctl()
    assert s.safe_ceiling(snap(abandon_a=3))[0] < s.safe_ceiling(snap(abandon_a=0))[0]


def test_rate_limit_caps_burst():
    s = SafetyController(Config(cps_limit=5), FakeRouter())
    d = s.review(PacingRequest("progressive", 50, False), snap(free=50), 1000.0)
    assert d.approved <= 10


# ---------------------------------------------------------------- the allocator cannot be reached without an approval
def test_allocator_refuses_forged_expired_or_missing_approvals():
    h = Harness("predictive", n_agents=5, n_borrowers=50)
    a = h.dialer.allocator
    now = h.clock.now()
    forged = Approval("c1", 5, 0, 0, now, now + 60, "n", "deadbeef")
    with pytest.raises(UnsafeApproval):
        a.reserve(forged)
    with pytest.raises((UnsafeApproval, AttributeError)):
        a.reserve(None)
    real = h.dialer.safety._issue("c1", 1, 0, 0, now)
    h.clock.advance(60)
    with pytest.raises(UnsafeApproval):
        a.reserve(real)                                  # expired
    assert h.db.conn().execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_tampering_with_an_approval_invalidates_it():
    h = Harness("predictive", n_agents=5, n_borrowers=50)
    now = h.clock.now()
    ok = h.dialer.safety._issue("c1", 1, 0, 0, now)
    from dataclasses import replace
    bigger = replace(ok, bound=100)
    assert h.dialer.safety.verify(ok, now) and not h.dialer.safety.verify(bigger, now)


def test_ceiling_holds_when_several_workers_act_on_the_same_stale_view():
    """Three workers each hold a fresh approval for 20 pooled calls, ceiling 20. Total in flight must still be <= 20."""
    h = Harness("predictive", n_agents=50, n_borrowers=500, workers=3)
    now = h.clock.now()
    appr = [d.safety._issue("c1", 0, 20, 20, now) for d in h.dialers]
    barrier = threading.Barrier(3)
    made = []

    def go(d, a):
        barrier.wait()
        made.append(len(d.allocator.reserve(a)))

    ts = [threading.Thread(target=go, args=(d, a)) for d, a in zip(h.dialers, appr)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(made) == 20
