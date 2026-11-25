from dialer.config import Config
from sim.harness import Harness


def run(mode, ar, talk, secs=600, n=60, **kw):
    h = Harness(mode, n_agents=n, answer_rate=ar, talk=talk, **kw)
    h.run(secs)
    return h, h.report()


def test_progressive_never_exceeds_agents_and_never_abandons():
    h, r = run("progressive", 0.3, 120, 300)
    assert r["abandoned"] == 0 and not r["violations"]
    # at every instant, bound in-flight calls <= agent count
    assert h.db.conn().execute("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM agent_transitions WHERE to_state='DIALING' GROUP BY CAST(at AS INT))").fetchone()[0] <= 60


def test_predictive_beats_progressive_on_utilization_and_stays_inside_abandon_limit():
    _, p = run("progressive", 0.2, 120, 900)
    _, q = run("predictive", 0.2, 120, 900)
    assert q["utilization"] > p["utilization"] + 0.05
    assert q["abandon_rate"] <= Config().abandon_limit + 0.01
    assert not q["violations"]


def test_changing_conditions_do_not_break_the_abandon_limit():
    ar = lambda t: 0.2 if (t - 1000) < 300 else 0.7     # answer rate triples mid-run
    h, r = run("predictive", ar, 100, 900)
    assert r["abandon_rate"] <= 0.05 and not r["violations"]


def test_multi_provider_with_flaky_b_stays_consistent():
    h, r = run("predictive", 0.4, 90, 600, providers="AB")
    assert not r["violations"] and r["connected"] > 0


def test_decisions_are_logged_with_reasons():
    h, _ = run("predictive", 0.3, 120, 200)
    row = h.db.conn().execute("SELECT * FROM decisions WHERE engine='predictive' AND action IN ('REDUCE','APPROVE') LIMIT 1").fetchone()
    assert row is not None and "p_hat" in row["explain"]
