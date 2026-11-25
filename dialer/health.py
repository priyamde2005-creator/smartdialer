"""Circuit breaker per provider + a router that picks among them. The dialer only talks to the router."""
from collections import deque

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"


class ProviderHealth:
    def __init__(self, cfg):
        self.cfg = cfg
        self.results = deque(maxlen=200)      # (t, ok)
        self.state = CLOSED
        self.opened_at = 0.0
        self.probes_left = 0
        self.setup_ewma = None

    def _window(self, now):
        h = self.cfg.health_horizon_s
        return [ok for t, ok in self.results if now - t <= h]

    def failure_rate(self, now):
        w = self._window(now)
        return (w.count(False) / len(w) if w else 0.0), len(w)

    def current_state(self, now):
        if self.state == OPEN and now - self.opened_at >= self.cfg.breaker_cooldown_s:
            self.state, self.probes_left = HALF_OPEN, self.cfg.probe_quota
        return self.state

    def allow(self, now) -> bool:
        s = self.current_state(now)
        if s == CLOSED:
            return True
        if s == HALF_OPEN and self.probes_left > 0:
            self.probes_left -= 1
            return True
        return False

    def record(self, ok: bool, now):
        s = self.current_state(now)
        if s == HALF_OPEN:
            if ok:
                self.state = CLOSED
                self.results.clear()
            else:
                self.state, self.opened_at = OPEN, now
            return
        self.results.append((now, ok))
        rate, n = self.failure_rate(now)
        if s == CLOSED and n >= self.cfg.health_min_samples and rate >= self.cfg.open_threshold:
            self.state, self.opened_at = OPEN, now

    def record_setup(self, seconds):
        self.setup_ewma = seconds if self.setup_ewma is None else 0.9 * self.setup_ewma + 0.1 * seconds

    def degraded(self, now) -> bool:
        s = self.current_state(now)
        if s != CLOSED:
            return True
        rate, n = self.failure_rate(now)
        if n >= self.cfg.health_min_samples // 2 and rate >= self.cfg.degraded_threshold:
            return True
        return self.setup_ewma is not None and self.setup_ewma > self.cfg.max_setup_s


class ProviderRouter:
    """Priority order, skipping providers whose breaker is open. Half-open providers only get probe traffic."""

    def __init__(self, providers, cfg, clock):
        self.providers = list(providers)
        self.by_name = {p.name: p for p in providers}
        self.health = {p.name: ProviderHealth(cfg) for p in providers}
        self.clock = clock

    def choose(self):
        now = self.clock.now()
        for p in self.providers:
            if self.health[p.name].allow(now):
                return p
        return None

    def get(self, name):
        return self.by_name[name]

    def record(self, provider, ok):
        self.health[provider.name].record(ok, self.clock.now())

    def usable(self) -> bool:
        now = self.clock.now()
        return any(self.health[p.name].current_state(now) != OPEN for p in self.providers)

    def _first_usable(self):
        now = self.clock.now()
        for p in self.providers:
            if self.health[p.name].current_state(now) != OPEN:
                return p
        return None

    def degraded(self) -> bool:
        p = self._first_usable()
        return p is None or self.health[p.name].degraded(self.clock.now())

    def new_call_quota(self):
        """None = unlimited. While the preferred provider is only being probed, cap new calls at the probe budget."""
        p = self._first_usable()
        if p is None:
            return 0
        h = self.health[p.name]
        return h.probes_left if h.current_state(self.clock.now()) == HALF_OPEN else None

    def poll_events(self):
        now = self.clock.now()
        out = []
        for p in self.providers:
            try:
                out.extend(p.poll_events(now))
            except Exception:
                pass
        return out
