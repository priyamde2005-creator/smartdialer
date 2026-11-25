"""Basic load test. Honest about what it measures: one SQLite file, one machine.

    python -m sim.loadtest
"""
import random
import tempfile
import threading
import time
import os

from dialer import repo
from dialer.clock import SimClock
from dialer.config import Config
from dialer.db import Database
from dialer.pacing.predictive import PredictivePacing
from dialer.providers.base import ProviderEvent
from dialer.safety import SafetyController
from sim.harness import Harness
from tests.test_safety import FakeRouter, snap


def pacing_cost():
    print("1) Decision cost per tick (pacing + safety math, then the snapshot query) as the campaign grows")
    print(f"   {'agents':>8} {'pacing+safety ms':>18} {'snapshot SQL ms':>17}")
    cfg = Config()
    for n in (100, 1000, 10_000, 100_000):
        s = snap(free=int(n * .1), connected=int(n * .6), wrap_up=int(n * .05), wrap_soon=int(n * .02), logged_in=n,
                 pending_borrowers=1000, pooled_unanswered=int(n * .15))
        pac, saf = PredictivePacing(cfg), SafetyController(cfg, FakeRouter())
        t = time.perf_counter()
        for i in range(20):
            s2 = snap(free=s.free + i, connected=s.connected, wrap_soon=s.wrap_soon, logged_in=n, pending_borrowers=1000)  # defeat the cache
            saf.review(pac.propose(s2), s2, 1000.0)
        calc = (time.perf_counter() - t) / 20 * 1000
        if True:
            h = Harness("predictive", n_agents=n, n_borrowers=1000)
            t = time.perf_counter()
            for _ in range(5):
                repo.take_snapshot(h.db.conn(), cfg, "c1", 1000.0)
            sql = (time.perf_counter() - t) / 5 * 1000
        else:
            sql = float("nan")
        print(f"   {n:>8} {calc:>18.3f} {sql:>17.2f}")


def allocation_throughput(agents=5000, threads=4):
    print(f"\n2) Allocation under contention: {threads} threads, {agents} agents, each grabbing agents+borrowers in 25-call batches")
    h = Harness("progressive", n_agents=agents, n_borrowers=agents * 2)
    done, lock = [], threading.Lock()

    def worker(w):
        a = h.dialers[0].allocator
        mine = 0
        while True:
            appr = h.dialer.safety._issue("c1", 25, 0, 0, h.clock.now())
            got = a.reserve(appr)
            if not got:
                break
            mine += len(got)
        with lock:
            done.append(mine)

    t = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    dt = time.perf_counter() - t
    bad = repo.check_invariants(h.db.conn())
    print(f"   reserved {sum(done)} calls in {dt:.2f}s = {sum(done) / dt:,.0f}/s; per-thread {done}; double-allocations/invariant violations: {len(bad)}")


def event_ingestion(n_calls=3000, threads=4):
    print(f"\n3) Provider-event ingestion: {n_calls} calls x 3 events, 25% duplicated, shuffled, {threads} threads")
    h = Harness("predictive", n_agents=n_calls + 10, n_borrowers=n_calls + 10)
    now = h.clock.now()
    ids = []
    with h.db.txn() as c:
        for b in repo.claim_borrowers(c, "c1", n_calls, now):
            cid = repo.new_call_id()
            repo.create_call(c, cid, "c1", b["id"], None, now, 10, "load")
            repo.call_transition(c, cid, "INITIATED", now, "load", provider="A", initiated_at=now, lease_until=now + 999)
            ids.append(cid)
    rng = random.Random(5)
    evs = [ProviderEvent("A", cid, t) for cid in ids for t in ("RINGING", "ANSWERED", "COMPLETED")]
    evs += rng.sample(evs, len(evs) // 4)
    rng.shuffle(evs)
    chunks = [evs[i::threads] for i in range(threads)]
    outcomes, lock = {}, threading.Lock()

    def worker(ch):
        life = h.dialers[0].life
        local = {}
        for e in ch:
            o = life.handle_event(e)
            local[o] = local.get(o, 0) + 1
        with lock:
            for k, v in local.items():
                outcomes[k] = outcomes.get(k, 0) + v

    t = time.perf_counter()
    ts = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    [x.start() for x in ts]
    [x.join() for x in ts]
    dt = time.perf_counter() - t
    bad = repo.check_invariants(h.db.conn())
    print(f"   {len(evs)} events in {dt:.2f}s = {len(evs) / dt:,.0f} events/s; outcomes {outcomes}; invariant violations: {len(bad)}")


if __name__ == "__main__":
    pacing_cost()
    allocation_throughput()
    event_ingestion()
