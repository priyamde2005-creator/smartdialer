"""Scenario runner: progressive vs predictive under different answer rates, talk times, provider latency and failures.

    python -m sim.simulate            # all scenarios (~1-2 min)
    python -m sim.simulate --quick    # shorter runs
"""
import csv
import json
import os
import sys

from sim.harness import Harness

OUT = os.path.join(os.path.dirname(__file__), "out")


def D_answer(t):          # scenario D: answer rate swings 20% -> 60% -> 30%
    s = t - 1000
    return 0.2 if s < 500 else (0.6 if s < 1000 else 0.3)


def D_talk(t):
    s = t - 1000
    return 120 if s < 500 else (60 if s < 1000 else 150)


SCENARIOS = [("A", 0.2, 120), ("B", 0.5, 90), ("C", 0.7, 180), ("D (changing)", D_answer, D_talk)]


def fmt(r):
    return (f"{r['utilization']:>6.1%} {r['occupancy']:>6.1%} {r['initiated']:>6} {r['answered']:>6} {r['connected']:>6} "
            f"{r['abandoned']:>5} {r['abandon_rate']:>6.2%} {r['dials_per_connect']:>5.1f}")


HEAD = "  mode          util  occup  inited  answrd  connctd  aband  ab.rate  dials/conn"


def run_one(name, mode, ar, talk, secs, providers="A", events=None, agents=100, save=True):
    h = Harness(mode, n_agents=agents, answer_rate=ar, talk=talk, providers=providers, n_borrowers=agents * 600)
    h.run(secs, events)
    r = h.report()
    if save:
        os.makedirs(OUT, exist_ok=True)
        fn = os.path.join(OUT, f"{name.split()[0]}_{mode}_{providers}.csv".replace("/", "-"))
        with open(fn, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "logged_in", "connected", "free", "pooled_in_flight", "requested", "approved", "action", "abandon_rate"])
            w.writerows(h.series)
    return h, r


def main(quick=False):
    secs = 600 if quick else 1500
    print(f"== Scenarios (100 agents, {secs}s of virtual time, clean provider A) ==")
    print(HEAD)
    samples = None
    for name, ar, talk in SCENARIOS:
        for mode in ("progressive", "predictive"):
            h, r = run_one(name, mode, ar, talk, secs)
            print(f"{name:<13}{mode:<12}{fmt(r)}")
            if mode == "predictive":
                s = r["safety"]
                print(f"{'':<13}  safety decisions: {s}  top reasons: {r['reasons']}")
                if name == "A":
                    samples = h.db.conn().execute("SELECT at,requested,approved,action,explain,reasons FROM decisions WHERE engine='predictive' "
                                                  "AND action IN ('REDUCE','APPROVE') ORDER BY id LIMIT 1 OFFSET 400").fetchone()
        print()
    if samples:
        e = json.loads(samples["explain"])
        print("== Why did it dial that many calls? (one real decision from scenario A)")
        print(f"  pacing asked for {samples['requested']} -> safety {samples['action']} -> {samples['approved']} approved")
        print(f"  pacing: {e.get('why')}")
        print(f"  safety: ceiling={e.get('ceiling')} p_safe={e.get('p_safe')} supply_safe={e.get('supply_safe')} "
              f"eps={e.get('eps')} abandon_rate={e.get('abandon_rate')}  {json.loads(samples['reasons'])}\n")

    print("== Same scenario A, but the carrier misbehaves ==")
    print(HEAD)
    for label, prov, ev in [("slow+flaky B only", "B", None),
                            ("A dies 5-8 min in", "AB", {300: lambda h: h.pA.set_outage(True), 480: lambda h: h.pA.set_outage(False)})]:
        for mode in ("progressive", "predictive"):
            h, r = run_one("A", mode, 0.2, 120, secs, providers=prov, events=ev, save=False)
            print(f"{label:<19}{mode:<12}{fmt(r)}   safety={r['safety']}")
    print("\n== 40 of 100 agents vanish at t=500s (predictive, scenario B)")
    print(HEAD)
    h, r = run_one("B", "predictive", 0.5, 90, secs, events={500: lambda h: h.drop_agents(40)}, save=False)
    print(f"{'agent drop':<13}{'predictive':<12}{fmt(r)}   safety={r['safety']}")
    print(f"\nCSV time series written to {OUT}")


if __name__ == "__main__":
    main("--quick" in sys.argv)
