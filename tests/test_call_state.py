from sim.failures import crash_after_answered, duplicate_events, out_of_order


def test_duplicate_events_do_not_create_duplicate_transitions():
    r = duplicate_events()
    assert r["results"].count("APPLIED") == 3
    assert len(r["transitions"]) == len(set(r["transitions"]))
    assert r["transitions"][-1] == ("CONNECTED", "COMPLETED") and r["agent"] == "WRAP_UP"
    assert not r["violations"]


def test_out_of_order_events_end_in_a_sane_state():
    r = out_of_order()
    a = r["COMPLETED,ANSWERED,RINGING"]
    assert a["state"] == "COMPLETED" and a["agent"] == "AVAILABLE" and a["results"][1:] == ["IGNORED_TERMINAL"] * 2
    assert a["abandoned"] == 1                       # we can't prove an agent was bridged, so we count it against ourselves
    b = r["ANSWERED,RINGING,COMPLETED"]
    assert b["state"] == "COMPLETED" and b["results"][1] == "IGNORED_STALE" and b["agent"] == "WRAP_UP" and b["abandoned"] == 0
    c = r["FAILED,ANSWERED"]
    assert c["state"] == "FAILED" and c["results"][1] == "ORPHAN_ANSWER_HANGUP" and c["abandoned"] == 1
    assert all(not v["violations"] for v in r.values())


def test_crash_right_after_answered_leaves_nothing_half_applied():
    r = crash_after_answered()
    assert r["state_after_crash"] == ("RINGING", "DIALING")
    assert r["redelivered"] == "APPLIED" and r["final"] == "COMPLETED" and r["agent"] == "WRAP_UP"
    assert not r["violations"]
