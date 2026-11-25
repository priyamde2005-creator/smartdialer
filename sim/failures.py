"""The five failure cases from the brief, as runnable scenarios. `python -m sim.failures` prints what happens;
tests/test_failures.py asserts on the returned dicts."""
from collections import Counter

from dialer import repo
from dialer.allocator import SimulatedCrash
from dialer.models import CallState as C
from sim.harness import Harness


def say(msg=""):
    print(msg)


# ------------------------------------------------------------------------------------------------ 1. worker crash
def worker_crash(point="after_originate", verbose=False):
    """Agent reserved > borrower reserved > call initiated > worker crashes. What happens when the system comes back?"""
    fired = {"n": 0}

    def fault(p, **kw):
        if p == point and fired["n"] == 0:
            fired["n"] = 1
            raise SimulatedCrash(p)

    h = Harness("progressive", n_agents=5, answer_rate=0.9, talk=30, n_borrowers=200, fault=fault)
    try:
        h.step()
        raise AssertionError("crash hook never fired")
    except SimulatedCrash:
        pass
    c = h.db.conn()
    stuck = c.execute("SELECT id,state,agent_id FROM calls").fetchall()
    before = {"calls": [(r["state"]) for r in stuck],
              "agents": Counter(r["state"] for r in c.execute("SELECT state FROM agents")),
              "provider_has_call": [cid for cid in h.pA.calls]}
    if verbose:
        say(f"  [crash @{point}] DB right after the crash: calls={before['calls']} agents={dict(before['agents'])} "
            f"carrier holds {len(h.pA.calls)} call(s)")
    h.restart_worker(0)                       # new process, no memory of what the old one was doing
    for _ in range(int(h.cfg.lease_s) + 15):
        h.step()
    after_calls = Counter(r["state"] for r in c.execute("SELECT state FROM calls"))
    first = stuck[0]["id"]
    out = {"before": before, "first_call": h.call(first), "first_transitions": h.transitions(first),
           "after_calls": dict(after_calls), "violations": repo.check_invariants(c),
           "borrower": dict(c.execute("SELECT state,attempts FROM borrowers WHERE id=?", (h.call(first)["borrower_id"],)).fetchone())}
    if verbose:
        say(f"  after restart + reaper: first call {out['first_call']['state']} via {out['first_transitions']}")
        say(f"  borrower={out['borrower']} invariants violated: {out['violations'] or 'none'}")
    return out


# ------------------------------------------------------------------------------------------------ 2. provider outage
def provider_outage(verbose=False):
    """Provider A starts timing out. Existing calls? New calls? Retries? Pacing?"""
    h = Harness("predictive", n_agents=60, answer_rate=0.35, talk=90, providers="AB", n_borrowers=40000)
    h.run(240)                                # warm up on A
    base = h.report()
    a0, b0 = h.pA.originate_count, h.pB.originate_count
    live_on_a = h.db.conn().execute("SELECT COUNT(*) FROM calls WHERE provider='A' AND state IN ('RINGING','CONNECTED','ANSWERED')").fetchone()[0]
    h.pA.set_outage(True)
    timeline, states = [], []
    for sec in range(90):
        h.step()
        if sec % 10 == 9:
            d = h.dialer
            req, dec = d.last_decision
            timeline.append((sec + 1, h.pA.originate_count - a0, h.pB.originate_count - b0,
                             h.router.health["A"].current_state(h.clock.now()), dec.action, dec.reasons[:1]))
    breaker_during = h.router.health["A"].current_state(h.clock.now())
    h.pA.set_outage(False)
    h.run(60)
    out = {"live_on_a_at_outage": live_on_a, "timeline": timeline, "breaker_during": breaker_during,
           "breaker_after": h.router.health["A"].current_state(h.clock.now()),
           "a_calls_after": h.pA.originate_count - a0, "b_calls": h.pB.originate_count - b0,
           "violations": repo.check_invariants(h.db.conn()), "report": h.report(),
           "stuck": h.db.conn().execute("SELECT COUNT(*) FROM calls WHERE state IN ('RESERVED') AND lease_until<?", (h.clock.now() - 30,)).fetchone()[0]}
    if verbose:
        say(f"  {live_on_a} calls were live on A when it died")
        say("  t(s)  A-originates  B-originates  breaker(A)  decision")
        for t, a, b, st, act, rs in timeline:
            say(f"  {t:>4}  {a:>12}  {b:>12}  {st:<9}  {act} {rs}")
        say(f"  A recovered -> breaker {out['breaker_after']}; invariants: {out['violations'] or 'ok'}")
    return out


def total_outage(verbose=False):
    """Both providers down: nothing new is dialed; agents don't leak; everything recovers afterwards."""
    h = Harness("predictive", n_agents=40, answer_rate=0.35, talk=60, providers="AB", n_borrowers=30000)
    h.run(200)
    h.pA.set_outage(True)
    h.pB.set_outage(True)
    h.run(80)
    started_during = Counter()
    n0 = h.dialer.counts["calls_started"]
    h.run(40)
    during = h.dialer.counts["calls_started"] - n0
    reasons = dict(h.dialer.reasons.most_common(3))
    h.pA.set_outage(False)
    h.pB.set_outage(False)
    h.run(120)
    out = {"started_while_all_down": during, "reasons": reasons, "violations": repo.check_invariants(h.db.conn()),
           "started_after": h.dialer.counts["calls_started"] - n0 - during, "agents": dict(Counter(
               r["state"] for r in h.db.conn().execute("SELECT state FROM agents")))}
    if verbose:
        say(f"  new calls started while both providers were down: {during}; reasons: {reasons}")
        say(f"  after recovery: {out['started_after']} new calls, agents {out['agents']}, invariants: {out['violations'] or 'ok'}")
    return out


# ------------------------------------------------------------------------------------------------ 3. availability drop
def agent_drop(silent=False, verbose=False):
    """100 agents, 40 disappear within seconds. How quickly does the dialer react?"""
    h = Harness("predictive", n_agents=100, answer_rate=0.3, talk=120, n_borrowers=60000)
    h.run(300)
    c = h.db.conn()
    before = repo.take_snapshot(c, h.cfg, "c1", h.clock.now())
    ab0 = c.execute("SELECT COALESCE(SUM(abandoned),0) FROM calls").fetchone()[0]
    t_drop = h.clock.now()
    h.drop_agents(40, silent=silent)
    first_reaction = None
    cap_after = None
    for sec in range(1, 61):
        snap, req, dec = h.step()
        if first_reaction is None and (snap.logged_in <= 60):
            first_reaction = sec
        if cap_after is None and snap.logged_in <= 60:
            ceiling = h.dialer.safety.safe_ceiling(snap)[0]
            if snap.pooled_demand <= ceiling:
                cap_after = sec
    ab1 = c.execute("SELECT COALESCE(SUM(abandoned),0) FROM calls").fetchone()[0]
    why = dict(c.execute("SELECT end_reason,COUNT(*) FROM calls WHERE abandoned=1 AND ended_at>=? GROUP BY end_reason", (t_drop,)).fetchall())
    out = {"abandon_reasons": why, "dropped_mid_call": why.get("AGENT_LOST_MID_CALL", 0),
           "silent": silent, "logged_in_before": before.logged_in, "demand_before": before.pooled_demand,
           "dialer_saw_drop_after_s": first_reaction, "demand_within_ceiling_after_s": cap_after,
           "shed": h.dialer.counts["shed"], "abandons_in_60s": ab1 - ab0, "violations": repo.check_invariants(c),
           "last_decision": h.dialer.last_decision[1].action}
    if verbose:
        kind = "silent network drop (heartbeat timeout)" if silent else "explicit disconnect"
        say(f"  {kind}: dialer saw {before.logged_in}->60 agents after {first_reaction}s; in-flight calls back under the safe ceiling after "
            f"{cap_after}s; shed {out['shed']} ringing calls; abandons in the next 60s: {out['abandons_in_60s']} "
            f"(by reason: {why}; mid-call drops are unavoidable, the other buckets are the dialer's own)")
    return out


# ------------------------------------------------------------------------------------------------ 4. duplicates
def duplicate_events(verbose=False):
    h = Harness("progressive", n_agents=2, n_borrowers=20)
    cid, aid = h.make_call()
    res = [h.send(cid, t) for t in ("RINGING", "ANSWERED", "ANSWERED", "ANSWERED", "RINGING", "COMPLETED", "COMPLETED")]
    out = {"results": res, "transitions": h.transitions(cid), "agent": h.agent(aid)["state"],
           "agent_transitions": h.db.conn().execute("SELECT COUNT(*) FROM agent_transitions WHERE agent_id=?", (aid,)).fetchone()[0],
           "violations": repo.check_invariants(h.db.conn())}
    if verbose:
        say(f"  events -> {res}")
        say(f"  call transitions (exactly one per state): {out['transitions']}")
    return out


# ------------------------------------------------------------------------------------------------ 5. out of order
def out_of_order(verbose=False):
    outs = {}
    for name, seq in {"COMPLETED,ANSWERED,RINGING": ["COMPLETED", "ANSWERED", "RINGING"],
                      "ANSWERED,RINGING,COMPLETED": ["ANSWERED", "RINGING", "COMPLETED"],
                      "FAILED,ANSWERED": ["FAILED", "ANSWERED"]}.items():
        h = Harness("progressive", n_agents=2, n_borrowers=20)
        cid, aid = h.make_call()
        res = [h.send(cid, t, "NO_ANSWER" if t == "FAILED" else "") for t in seq]
        outs[name] = {"results": res, "state": h.call(cid)["state"], "end_reason": h.call(cid)["end_reason"],
                      "abandoned": h.call(cid)["abandoned"], "agent": h.agent(aid)["state"],
                      "transitions": h.transitions(cid), "violations": repo.check_invariants(h.db.conn())}
        if verbose:
            say(f"  {name:<30} -> {outs[name]['results']} final call={outs[name]['state']} agent={outs[name]['agent']} "
                f"abandoned_flag={outs[name]['abandoned']}")
    return outs


# ------------------------------------------------------------------------------------------------ 6. crash right after ANSWERED
def crash_after_answered(verbose=False):
    h = Harness("progressive", n_agents=2, n_borrowers=20)
    cid, aid = h.make_call()
    h.send(cid, "RINGING")
    crashed = {"n": 0}

    def fault(p, **kw):
        if p == "before_commit" and kw["ev"].type == "ANSWERED" and crashed["n"] == 0:
            crashed["n"] = 1
            raise SimulatedCrash(p)

    h.restart_worker(0, fault=fault)
    try:
        h.send(cid, "ANSWERED")
    except SimulatedCrash:
        pass
    mid = (h.call(cid)["state"], h.agent(aid)["state"])      # nothing half-applied
    h.restart_worker(0)
    r1 = h.send(cid, "ANSWERED")                             # the carrier redelivers
    r2 = h.send(cid, "COMPLETED")
    out = {"state_after_crash": mid, "redelivered": r1, "completed": r2, "final": h.call(cid)["state"], "agent": h.agent(aid)["state"],
           "transitions": h.transitions(cid), "violations": repo.check_invariants(h.db.conn())}
    if verbose:
        say(f"  after crash mid-ANSWERED: call/agent = {mid} (txn rolled back, nothing half-applied)")
        say(f"  redelivered ANSWERED -> {r1}; COMPLETED -> {r2}; final {out['final']} / agent {out['agent']}")
    return out


if __name__ == "__main__":
    steps = [("1a. Worker crash right after the carrier accepted the call", lambda: worker_crash("after_originate", True)),
             ("1b. Worker crash after reserving, before calling the carrier", lambda: worker_crash("after_reserve", True)),
             ("2a. Provider A outage (B available)", lambda: provider_outage(True)),
             ("2b. Both providers down", lambda: total_outage(True)),
             ("3a. 40 of 100 agents disconnect", lambda: agent_drop(False, True)),
             ("3b. 40 of 100 agents silently vanish", lambda: agent_drop(True, True)),
             ("4.  Duplicate events", lambda: duplicate_events(True)),
             ("5.  Out-of-order events", lambda: out_of_order(True)),
             ("6.  Worker dies right after ANSWERED", lambda: crash_after_answered(True))]
    for title, fn in steps:
        print(f"\n== {title}")
        fn()
