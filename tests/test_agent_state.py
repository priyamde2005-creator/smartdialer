import threading

import pytest

from dialer import repo
from dialer.models import AGENT_TRANSITIONS, ALL_AGENT_STATES
from sim.harness import Harness


def test_every_illegal_agent_transition_is_rejected():
    h = Harness("progressive", n_agents=1, n_borrowers=5)
    aid = h.agent_ids[0]
    for frm in ALL_AGENT_STATES:
        for to in ALL_AGENT_STATES:
            with h.db.txn() as c:
                c.execute("UPDATE agents SET state=? WHERE id=?", (frm, aid))
                if (frm, to) in AGENT_TRANSITIONS:
                    assert repo.agent_transition(c, aid, (frm,), to, 1.0, call_id="x")
                elif frm != to:
                    with pytest.raises(repo.IllegalTransition):
                        repo.agent_transition(c, aid, (frm,), to, 1.0)


def test_two_workers_cannot_reserve_the_same_agent():
    """Both workers read the same AVAILABLE agent at (almost) the same time; the CAS lets exactly one win."""
    h = Harness("progressive", n_agents=1, n_borrowers=5)
    aid = h.agent_ids[0]
    n_workers = 16
    seen = h.db.conn().execute("SELECT version FROM agents WHERE id=?", (aid,)).fetchone()[0]   # everyone saw version 0
    barrier, wins = threading.Barrier(n_workers), []

    def worker(i):
        barrier.wait()
        with h.db.txn() as c:
            wins.append(repo.agent_transition(c, aid, ("AVAILABLE",), "RESERVED", 1.0, "race", call_id=f"call-{i}", expect_version=seen))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1 and wins.count(False) == n_workers - 1


def test_many_workers_never_double_allocate_agents():
    h = Harness("progressive", n_agents=60, n_borrowers=10)
    got, lock = [], threading.Lock()

    def worker():
        while True:
            with h.db.txn() as c:
                r = repo.reserve_available_agents(c, "c1", 7, 1.0, [repo.new_call_id() for _ in range(7)])
            if not r:
                return
            with lock:
                got.extend(a for a, _ in r)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 60 and len(set(got)) == 60


def test_db_backstop_blocks_two_active_calls_for_one_agent():
    """Even if application logic were buggy, the partial unique index refuses a second active call on one agent."""
    import sqlite3
    h = Harness("progressive", n_agents=2, n_borrowers=10)
    cid, aid = h.make_call()
    with pytest.raises(sqlite3.IntegrityError):
        with h.db.txn() as c:
            b = repo.claim_borrowers(c, "c1", 1, 0)[0]
            repo.create_call(c, "dup", "c1", b["id"], aid, 1.0, 10, "test")


def test_pause_requested_during_call_applies_after_wrap_up():
    h = Harness("progressive", n_agents=1, n_borrowers=5)
    cid, aid = h.make_call()
    h.send(cid, "ANSWERED")
    with h.db.txn() as c:
        c.execute("UPDATE agents SET pause_requested=1 WHERE id=?", (aid,))
    h.send(cid, "COMPLETED")
    h.clock.advance(h.cfg.wrap_up_s + 1)
    h.heartbeat()
    h.dialer.life.tick_agents()
    assert h.agent(aid)["state"] == "PAUSED"
