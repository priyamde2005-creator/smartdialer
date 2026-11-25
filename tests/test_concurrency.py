import threading

from sim.harness import Harness


def test_three_workers_running_concurrently_stay_consistent():
    """Real threads, one SQLite file, three dialer workers on the same campaign, ticking at the same instant."""
    h = Harness("predictive", n_agents=60, answer_rate=0.35, talk=60, workers=3, n_borrowers=30000)
    for _ in range(240):
        h.clock.advance(1.0)
        h.heartbeat()
        ts = [threading.Thread(target=d.tick) for d in h.dialers]
        [t.start() for t in ts]
        [t.join() for t in ts]
    r = h.report()
    assert not r["violations"], r["violations"]
    assert r["connected"] > 50
