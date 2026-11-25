"""The Safety Controller. Sits between pacing and the allocator and is the ONLY thing that can mint an Approval.

Design rules:
  * It recomputes its own bound from the raw snapshot, with deliberately pessimistic inputs. It never trusts the
    pacing engine's numbers, only its *request*.
  * It issues signed, short-lived Approvals. The allocator refuses anything else. Pacing code has no reference to
    the controller, the allocator or the providers, so there is no switch to flip (tests/test_architecture.py).
  * Limits are expressed as ceilings on absolute state ("at most N pooled calls in flight"), not as deltas, so
    several workers can't add up to more than the ceiling.
"""
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from .pacing.stats import max_inflight, posterior_mean, supply_estimate, wilson_upper

APPROVE, REDUCE, REJECT, FALLBACK = "APPROVE", "REDUCE", "REJECT", "FALLBACK_PROGRESSIVE"


@dataclass(frozen=True)
class Approval:
    campaign_id: str
    bound: int                 # calls that each get a reserved agent up front (progressive semantics)
    pooled: int                # calls dialed without an agent (predictive), subject to the ceiling below
    max_pooled_demand: int     # ceiling on pooled in-flight calls, re-checked inside the allocation transaction
    issued_at: float
    valid_until: float
    nonce: str
    sig: str


@dataclass
class Decision:
    action: str
    requested: int
    approved: int
    reasons: List[str]
    approval: Optional[Approval] = None
    explain: dict = field(default_factory=dict)


class TokenBucket:
    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, None

    def available(self, now):
        if self.t is not None:
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        return int(self.tokens)

    def take(self, n):
        self.tokens -= n


class SafetyController:
    def __init__(self, cfg, router):
        self.cfg, self.router = cfg, router
        self._secret = os.urandom(32)
        self.bucket = TokenBucket(cfg.cps_limit, cfg.cps_limit * 2)

    # ---------------------------------------------------------------- signing
    def _sig(self, a_fields) -> str:
        return hmac.new(self._secret, "|".join(map(str, a_fields)).encode(), hashlib.sha256).hexdigest()

    def _issue(self, campaign_id, bound, pooled, ceiling, now) -> Approval:
        nonce = uuid.uuid4().hex
        valid = now + self.cfg.approval_ttl_s
        return Approval(campaign_id, bound, pooled, ceiling, now, valid, nonce,
                        self._sig((campaign_id, bound, pooled, ceiling, now, valid, nonce)))

    def verify(self, a: Approval, now) -> bool:
        good = hmac.compare_digest(a.sig, self._sig((a.campaign_id, a.bound, a.pooled, a.max_pooled_demand,
                                                      a.issued_at, a.valid_until, a.nonce)))
        return good and now <= a.valid_until

    # ---------------------------------------------------------------- independent, pessimistic bound
    def _abandon_rate(self, s):
        return s.abandon_a / max(s.abandon_n, 100)

    def safe_ceiling(self, s):
        """Max pooled calls that may be in flight, using pessimistic inputs. Returns (n, details)."""
        cfg = self.cfg
        p_long = wilson_upper(s.ans_a_long, s.ans_n_long, z=1.28)       # ~90% upper bound on the long-run rate
        p_short = wilson_upper(s.ans_a_short, s.ans_n_short, z=0.84)   # catches a recent jump the long window hasn't seen
        p_safe = max(p_long, p_short)                           # whichever looks worse
        supply, parts = supply_estimate(s.free, s.connected, s.wrap_soon, s.setup_mean, s.talk_mean, cfg.safe_connected_credit)
        r = self._abandon_rate(s)
        eps = cfg.safe_eps * max(0.0, 1 - r / cfg.abandon_target)  # shrinks to 0 as we approach our own (stricter) target
        S = int(supply)                                         # floor, not round
        cap = int(cfg.max_ratio * max(S, 1))
        n = max_inflight(S, min(p_safe, 1.0), eps, cap)
        return n, {"p_safe": round(p_safe, 3), "supply_safe": round(supply, 2), "eps": round(eps, 4), "abandon_rate": round(r, 4)}

    def fallback_reasons(self, s) -> List[str]:
        cfg, out = self.cfg, []
        if s.ans_n_long < cfg.min_samples:
            out.append("COLD_START(not enough call history)")
        if self.router.degraded():
            out.append("PROVIDER_DEGRADED")
        if self._abandon_rate(s) >= cfg.abandon_limit:
            out.append("ABANDON_BUDGET_EXHAUSTED")
        if s.logged_in and s.recent_offline / (s.logged_in + s.recent_offline) >= cfg.churn_fraction:
            out.append("AGENT_AVAILABILITY_DROP")
        if s.setup_mean > cfg.max_setup_s:
            out.append("SLOW_CALL_SETUP")
        return out

    # ---------------------------------------------------------------- the gate
    def review(self, req, s, now) -> Decision:
        cfg, reasons = self.cfg, []
        bound_room = max(0, s.free - s.pooled_demand)
        gate = None
        if not s.active:
            gate = "CAMPAIGN_INACTIVE"
        elif s.pending_borrowers == 0:
            gate = "NO_BORROWERS"
        elif not self.router.usable():
            gate = "PROVIDER_DOWN"
        tokens = self.bucket.available(now)
        if gate is None and tokens < 1:
            gate = "CPS_RATE_LIMIT"
        if gate:
            return Decision(REJECT, req.count, 0, [gate])
        quota = self.router.new_call_quota()
        cap_new = min(tokens, cfg.max_new_per_tick, s.pending_borrowers, quota if quota is not None else 10 ** 9)
        if quota is not None:
            reasons.append(f"PROBING_PROVIDER(quota {quota})")

        fb = self.fallback_reasons(s) if req.pooled else []
        if req.pooled and not fb:
            ceiling, d = self.safe_ceiling(s)
            room = max(0, ceiling - s.pooled_demand)
            n = min(req.count, room, cap_new)
            if n < req.count:
                reasons.append(f"capped by safe ceiling {ceiling} (p_safe={d['p_safe']}, supply_safe={d['supply_safe']}, "
                               f"eps={d['eps']}, abandon_rate={d['abandon_rate']})" if room < req.count else "RATE/QUOTA_LIMIT")
            action = APPROVE if n == req.count else (REDUCE if n > 0 else REJECT)
            if n == 0 and req.count > 0:
                reasons.append("NO_SAFE_CAPACITY")
            appr = self._issue(s.campaign_id, 0, n, ceiling, now) if n > 0 else None
            if n > 0:
                self.bucket.take(n)
            return Decision(action, req.count, n, reasons, appr, {"ceiling": ceiling, **d})

        # progressive semantics: either asked for, or forced by a fallback condition
        n = min(bound_room, cap_new)
        if not req.pooled:
            n = min(n, req.count)
        action = FALLBACK if fb else (APPROVE if n == req.count else (REDUCE if n > 0 else REJECT))
        reasons = fb + reasons
        appr = self._issue(s.campaign_id, n, 0, 0, now) if n > 0 else None
        if n > 0:
            self.bucket.take(n)
        return Decision(action, req.count, n, reasons, appr, {"bound_room": bound_room})

    # ---------------------------------------------------------------- shedding
    def shed_target(self, s, predictive: bool) -> int:
        """How many ringing pooled calls to cancel because the world got worse.
        Normal wobble in the ceiling is NOT a reason to cancel (that just makes phones ring for nothing; the abandon
        budget absorbs it). We shed only on an availability drop, or if demand is more than double the safe ceiling."""
        if s.pooled_unanswered == 0:
            return 0
        limit = self.safe_ceiling(s)[0] if predictive else s.free
        excess = s.pooled_demand - limit
        drop = s.recent_offline > 0
        if drop and excess >= max(3, int(0.3 * s.pooled_demand)):
            return min(excess, s.pooled_unanswered)
        if excess >= 5 * max(limit, 5):
            return min(excess - limit, s.pooled_unanswered)
        return 0
