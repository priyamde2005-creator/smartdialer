from dataclasses import dataclass


@dataclass
class Config:
    # --- timing
    lease_s: float = 10.0              # how long a RESERVED call may sit before the reaper looks at it
    reconcile_delay_s: float = 3.0     # after an ambiguous originate (timeout) wait this long, then ask provider
    max_ring_s: float = 45.0           # hard stop for ringing
    connected_lease_s: float = 120.0   # silence on a connected call -> poll the provider
    max_unreconciled_s: float = 30.0   # provider unreachable this long -> force-fail the call
    max_call_s: float = 3600.0         # a connected call is never force-failed just because the carrier is unreachable
    wrap_up_s: float = 20.0
    heartbeat_timeout_s: float = 6.0
    hold_s: float = 2.0                # max time an answered call waits for an agent before it is abandoned
    approval_ttl_s: float = 2.0
    abandon_callback_s: float = 60.0
    retry_delay_s: float = 120.0
    provider_backoff_s: float = 5.0
    max_attempts: int = 4
    # --- pacing (optimistic)
    pace_eps: float = 0.015            # target expected-abandon fraction used by the pacing engine
    pace_connected_credit: float = 1.0
    max_ratio: float = 5.0             # in-flight calls never above ratio * supply
    max_new_per_tick: int = 400
    # --- safety (pessimistic, independent inputs)
    safe_eps: float = 0.015
    safe_connected_credit: float = 0.8
    abandon_limit: float = 0.03        # regulatory-style ceiling: hitting it forces progressive mode
    abandon_target: float = 0.02       # our own stricter target: overdial headroom shrinks to zero as we approach it
    min_samples: int = 30
    churn_fraction: float = 0.15       # >15% of logged-in agents vanished in last 10s -> progressive
    max_setup_s: float = 25.0
    cps_limit: float = 200.0           # calls-per-second ceiling toward telecom
    # --- priors / windows
    prior_answer: tuple = (3.0, 7.0)
    prior_talk_s: float = 120.0
    prior_setup_s: float = 12.0
    long_window: int = 200
    short_window: int = 30
    # --- provider health
    health_horizon_s: float = 60.0
    health_min_samples: int = 8
    open_threshold: float = 0.4
    degraded_threshold: float = 0.15
    breaker_cooldown_s: float = 10.0
    probe_quota: int = 3
