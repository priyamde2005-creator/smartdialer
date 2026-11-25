from dataclasses import dataclass, field


@dataclass(frozen=True)
class Snapshot:
    """Read-only view of the campaign handed to pacing + safety. Pacing gets nothing else."""
    now: float
    campaign_id: str
    active: bool
    agents: dict
    free: int                 # AVAILABLE agents
    connected: int
    wrap_up: int
    wrap_soon: int            # WRAP_UP agents that finish within the setup horizon
    logged_in: int
    pooled_unanswered: int    # predictive calls still ringing (no agent attached)
    held: int                 # answered, waiting for an agent
    bound_inflight: int
    pending_borrowers: int
    ans_n_long: int
    ans_a_long: int
    ans_n_short: int
    ans_a_short: int
    abandon_n: int            # answered calls in the abandon window
    abandon_a: int            # of which abandoned
    talk_mean: float
    setup_mean: float
    recent_offline: int       # agents that dropped in the last 10s

    @property
    def pooled_demand(self) -> int:
        return self.pooled_unanswered + self.held
