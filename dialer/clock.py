import time


class RealClock:
    def now(self) -> float:
        return time.time()


class SimClock:
    """Virtual clock so simulations run hours of campaign time in seconds."""

    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt
