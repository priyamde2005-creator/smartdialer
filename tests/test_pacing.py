from dialer.config import Config
from dialer.pacing.predictive import PredictivePacing
from dialer.pacing.progressive import ProgressivePacing
from dialer.pacing.stats import expected_overflow, max_inflight
from tests.test_safety import snap


def test_progressive_is_one_call_per_free_agent():
    assert ProgressivePacing(Config()).propose(snap(free=17)).count == 17
    assert ProgressivePacing(Config()).propose(snap(free=17, pooled_unanswered=10)).count == 7
    assert ProgressivePacing(Config()).propose(snap(free=17, pending_borrowers=4)).count == 4


def test_predictive_explains_itself():
    r = PredictivePacing(Config()).propose(snap())
    assert r.count > 0 and "p_hat" in r.explain and "supply" in r.explain and "dial" in r.explain["why"]


def test_lower_answer_rate_means_more_calls_in_flight():
    f = lambda p: max_inflight(10, p, 0.015, 200)
    assert f(0.2) > f(0.4) > f(0.8) >= 10


def test_more_tolerance_means_more_calls():
    assert max_inflight(10, 0.3, 0.05, 200) >= max_inflight(10, 0.3, 0.005, 200)


def test_overflow_math_sanity():
    assert expected_overflow(10, 0.5, 10) == 0
    assert abs(expected_overflow(2, 1.0, 0) - 2) < 0.01
    assert expected_overflow(40, 0.5, 10) > expected_overflow(40, 0.5, 20)


def test_no_borrowers_means_no_request():
    assert PredictivePacing(Config()).propose(snap(pending_borrowers=0)).count == 0
