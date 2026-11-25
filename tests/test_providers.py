import pytest

from dialer.clock import SimClock
from dialer.providers.base import ProviderError, ProviderTimeout
from dialer.providers.mock import World, make_provider_a, make_provider_b


def drain(p, clock, seconds=400):
    ev = []
    for _ in range(seconds * 2):
        clock.advance(0.5)
        ev += p.poll_events(clock.now())
    return ev


def test_originate_is_idempotent():
    clk = SimClock()
    a = make_provider_a(World(0.5, 60), clk)
    a.originate("c1", "+911")
    a.originate("c1", "+911")
    assert a.originate_count == 1


def test_provider_a_is_clean_and_b_is_messy():
    clk_a, clk_b = SimClock(), SimClock()
    a, b = make_provider_a(World(0.8, 20, 3), clk_a), make_provider_b(World(0.8, 20, 3), clk_b)
    fails = {"A": 0, "B": 0}
    for name, p in (("A", a), ("B", b)):
        for i in range(300):
            try:
                p.originate(f"{name}{i}", "+91")
            except (ProviderError, ProviderTimeout):
                fails[name] += 1
    assert fails["A"] < fails["B"]
    ea, eb = drain(a, clk_a), drain(b, clk_b)
    key = lambda e: (e.call_id, e.type)
    assert len({key(e) for e in ea}) == len(ea)                      # A: no duplicates
    assert len({key(e) for e in eb}) < len(eb)                       # B: duplicates
    order = {"RINGING": 0, "ANSWERED": 1, "COMPLETED": 2}
    last, reordered = {}, 0
    for e in eb:
        if e.type in order:
            if e.call_id in last and order[e.type] < last[e.call_id]:
                reordered += 1
            last[e.call_id] = max(order[e.type], last.get(e.call_id, 0))
    assert reordered > 0                                              # B: out-of-order delivery


def test_outage_times_out_and_lookup_of_unknown_is_none():
    clk = SimClock()
    a = make_provider_a(World(0.5, 60), clk)
    assert a.lookup("nope") is None
    a.set_outage(True)
    with pytest.raises(ProviderTimeout):
        a.originate("x", "+91")
    with pytest.raises(ProviderTimeout):
        a.lookup("x")


def test_ambiguous_timeout_may_have_placed_the_call():
    clk = SimClock()
    p = make_provider_b(World(0.5, 60), clk)
    p.timeout_rate, p.reject_rate, p.timeout_places_call = 1.0, 0.0, 1.0
    with pytest.raises(ProviderTimeout):
        p.originate("x", "+91")
    assert p.lookup("x") is not None          # ...which is why the dialer reconciles instead of blindly retrying
