"""Turns an Approval into real calls. It can't be used without one: no approval, no call."""
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import repo
from .models import AgentState as A, CallState as C
from .providers.base import ProviderError, ProviderTimeout


class UnsafeApproval(Exception):
    pass


class SimulatedCrash(BaseException):
    """Raised by fault hooks to emulate `kill -9`. BaseException so no `except Exception` can swallow it."""


@dataclass
class Allocation:
    call_id: str
    borrower_id: str
    phone: str
    agent_id: Optional[str]


class Allocator:
    def __init__(self, db, cfg, clock, router, verifier, lifecycle, worker_id="w1", fault: Optional[Callable] = None):
        self.db, self.cfg, self.clock, self.router = db, cfg, clock, router
        self.verifier, self.life, self.worker_id = verifier, lifecycle, worker_id
        self.fault = fault or (lambda point, **kw: None)

    # ------------------------------------------------------------------ step 1: reserve (one atomic transaction)
    def reserve(self, approval) -> List[Allocation]:
        now = self.clock.now()
        if not self.verifier.verify(approval, now):
            raise UnsafeApproval("missing, forged, or expired approval")
        out: List[Allocation] = []
        with self.db.txn() as c:
            # Re-read the world inside the write lock. The approval is a ceiling, not a delta, so two workers
            # holding approvals at the same time still can't exceed it.
            room = max(0, approval.max_pooled_demand - repo.pooled_demand(c, approval.campaign_id)) if approval.pooled else 0
            n_pooled = min(approval.pooled, room)
            borrowers = repo.claim_borrowers(c, approval.campaign_id, approval.bound + n_pooled, now)
            ids = [repo.new_call_id() for _ in borrowers]
            agents = repo.reserve_available_agents(c, approval.campaign_id, min(approval.bound, len(borrowers)), now, ids)
            n_bound = len(agents)
            n_pool_final = min(n_pooled, len(borrowers) - n_bound)
            used = n_bound + n_pool_final
            for b in borrowers[used:]:                      # claimed but not needed (agents vanished meanwhile)
                repo.settle_borrower(c, b["id"], now, "NOT_ATTEMPTED", self.cfg)
                c.execute("UPDATE borrowers SET next_attempt_at=? WHERE id=?", (now, b["id"]))
            for i, b in enumerate(borrowers[:used]):
                aid, cid = agents[i] if i < n_bound else (None, repo.new_call_id())
                repo.create_call(c, cid, approval.campaign_id, b["id"], aid, now, self.cfg.lease_s, self.worker_id)
                out.append(Allocation(cid, b["id"], b["phone"], aid))
        return out

    # ------------------------------------------------------------------ step 2: talk to the carrier (outside any txn)
    def dispatch(self, a: Allocation):
        now = self.clock.now()
        self.fault("after_reserve", alloc=a)
        provider = self.router.choose()
        if provider is None:
            self.life.fail_call(a.call_id, "NO_PROVIDER", cause="no-provider")
            return
        try:
            ref = provider.originate(a.call_id, a.phone)
        except ProviderTimeout:
            # Ambiguous. Do NOT retry blindly and do NOT release anything: the call may be live.
            self.router.record(provider, False)
            with self.db.txn() as c:
                repo.call_update(c, a.call_id, provider=provider.name, lease_until=now + self.cfg.reconcile_delay_s)
            return
        except ProviderError:
            self.router.record(provider, False)
            self.life.fail_call(a.call_id, "PROVIDER_REJECTED", cause="provider-rejected")
            return
        self.router.record(provider, True)
        self.fault("after_originate", alloc=a)            # crash window: carrier has the call, our DB doesn't know yet
        now = self.clock.now()
        with self.db.txn() as c:
            ok = repo.call_transition(c, a.call_id, C.INITIATED, now, "originated", provider=provider.name, provider_ref=ref,
                                      initiated_at=now, lease_until=now + self.cfg.max_ring_s + 5)
            if ok and a.agent_id:
                repo.agent_transition(c, a.agent_id, (A.RESERVED,), A.DIALING, now, "originated", call_id=a.call_id)

    def allocate(self, approval) -> int:
        allocs = self.reserve(approval)
        for a in allocs:
            self.dispatch(a)
        return len(allocs)

    # ------------------------------------------------------------------ shedding
    def shed(self, campaign_id, n) -> int:
        if n <= 0:
            return 0
        rows = self.db.conn().execute(
            "SELECT id FROM calls WHERE campaign_id=? AND bound=0 AND agent_id IS NULL AND state IN ('RESERVED','INITIATED','RINGING') "
            "ORDER BY created_at DESC LIMIT ?", (campaign_id, n)).fetchall()
        ids = [r["id"] for r in rows]
        self.life.cancel_calls(ids, "SHED", "shed-excess-demand")
        return len(ids)
