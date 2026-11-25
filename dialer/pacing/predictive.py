from .base import PacingRequest
from .stats import max_inflight, posterior_mean, supply_estimate


class PredictivePacing:
    """Dial ahead of agents, using the best estimate we have. This is the *optimistic* half of the design: the
    Safety Controller re-derives its own, more pessimistic bound and may cut whatever is asked for here.

    Idea in one line: keep just enough calls ringing that the expected number of answers matches the number of agents
    expected to be free when they pick up, with a small allowance (pace_eps) for the variance."""
    name = "predictive"

    def __init__(self, cfg):
        self.cfg = cfg

    def propose(self, s) -> PacingRequest:
        cfg = self.cfg
        p = posterior_mean(s.ans_a_long, s.ans_n_long, cfg.prior_answer)
        supply, parts = supply_estimate(s.free, s.connected, s.wrap_soon, s.setup_mean, s.talk_mean, cfg.pace_connected_credit)
        S = int(round(supply))
        cap = int(cfg.max_ratio * max(S, 1))
        target = max_inflight(S, p, cfg.pace_eps, cap)
        n = max(0, target - s.pooled_demand)
        n = min(n, cfg.max_new_per_tick, s.pending_borrowers)
        why = (f"p_hat={p:.2f} supply={supply:.1f} (free {parts['free']} + wrap_soon {parts['wrap_soon']} + "
               f"connected_frees {parts['connected_frees']}) -> can hold {target} calls in flight at <= {cfg.pace_eps:.1%} "
               f"expected abandon; {s.pooled_demand} already in flight -> dial {n}")
        return PacingRequest(self.name, n, pooled=True,
                             explain={"why": why, "p_hat": round(p, 3), "supply": round(supply, 2), "target": target,
                                      "in_flight": s.pooled_demand, "setup_s": round(s.setup_mean, 1), "talk_s": round(s.talk_mean, 1)})
