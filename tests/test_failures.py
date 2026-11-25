from sim.failures import agent_drop, provider_outage, total_outage, worker_crash


def test_worker_crash_after_originate_is_adopted_or_cleaned_up():
    r = worker_crash("after_originate")
    assert not r["violations"]
    assert r["first_call"]["state"] in ("CONNECTED", "COMPLETED", "FAILED", "RINGING")
    assert r["first_call"]["state"] != "RESERVED"      # nobody is left in limbo


def test_worker_crash_before_originate_releases_everything():
    r = worker_crash("after_reserve")
    assert not r["violations"]
    assert r["first_call"]["state"] == "FAILED" and r["first_call"]["end_reason"] == "NOT_FOUND_AT_PROVIDER"
    assert r["borrower"]["state"] == "PENDING" and r["borrower"]["attempts"] == 0   # their retry budget is untouched


def test_provider_outage_fails_over_and_recovers():
    r = provider_outage()
    assert r["breaker_during"] == "OPEN" and r["breaker_after"] == "CLOSED"
    assert r["b_calls"] > 0 and not r["violations"]
    assert r["stuck"] == 0


def test_total_outage_stops_dialing_then_resumes():
    r = total_outage()
    # only breaker probe traffic (probe_quota per cooldown) may go out while both carriers are down
    assert r["started_while_all_down"] <= 15 and "PROVIDER_DOWN" in r["reasons"]
    assert r["started_after"] > 20 and not r["violations"]


def test_agent_drop_reaction_time():
    fast = agent_drop(silent=False)
    slow = agent_drop(silent=True)
    assert fast["dialer_saw_drop_after_s"] <= 2 and fast["demand_within_ceiling_after_s"] <= 3
    assert slow["dialer_saw_drop_after_s"] <= 10                       # bounded by the heartbeat timeout
    assert fast["abandon_reasons"].get("ABANDONED_NO_AGENT", 0) <= 3   # the dialer's own abandons stay small
    assert not fast["violations"] and not slow["violations"]
