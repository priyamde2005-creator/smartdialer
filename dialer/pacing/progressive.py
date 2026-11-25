from .base import PacingRequest


class ProgressivePacing:
    """One free agent -> one call. Never more calls than free agents (minus calls already in the pool)."""
    name = "progressive"

    def __init__(self, cfg):
        self.cfg = cfg

    def propose(self, s) -> PacingRequest:
        n = max(0, s.free - s.pooled_demand)
        n = min(n, s.pending_borrowers)
        return PacingRequest(self.name, n, pooled=False,
                             explain={"free_agents": s.free, "pooled_in_flight": s.pooled_demand, "pending_borrowers": s.pending_borrowers})
