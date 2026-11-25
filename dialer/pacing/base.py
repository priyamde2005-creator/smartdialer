from dataclasses import dataclass, field


@dataclass(frozen=True)
class PacingRequest:
    """What a pacing engine is allowed to produce: a *wish*. It cannot place a call."""
    engine: str
    count: int
    pooled: bool            # True: calls dialed without an agent (predictive). False: one reserved agent per call.
    explain: dict = field(default_factory=dict)
