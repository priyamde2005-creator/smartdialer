"""Small, boring statistics. No ML: a Beta-posterior answer rate and a binomial overflow calculation."""
import math
from functools import lru_cache


def posterior_mean(a, n, prior):
    return (a + prior[0]) / (n + prior[0] + prior[1])


def wilson_upper(a, n, z=1.64):
    if n == 0:
        return 1.0
    p = a / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + m) / d)


@lru_cache(maxsize=100_000)
def _overflow(n, p_milli, s):
    p = p_milli / 1000.0
    if n <= s or p <= 0:
        return 0.0
    if p >= 1:
        return float(n - s)
    if n <= 300:
        lp, lq, lg = math.log(p), math.log1p(-p), math.lgamma(n + 1)
        tot = 0.0
        for k in range(s + 1, n + 1):
            tot += (k - s) * math.exp(lg - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq)
        return tot
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    z = (s - mu) / sd
    pdf = math.exp(-z * z / 2) / math.sqrt(2 * math.pi)
    tail = 0.5 * math.erfc(z / math.sqrt(2))
    return max(0.0, sd * (pdf - z * tail))


def expected_overflow(n: int, p: float, s: int) -> float:
    """E[max(0, X - s)] with X ~ Binomial(n, p): expected number of answered calls with no agent to take them."""
    return _overflow(n, int(round(p * 1000)), s)


def max_inflight(supply: int, p: float, eps: float, cap: int) -> int:
    """Largest n <= cap such that expected abandoned / expected answered <= eps, when `supply` agents can take calls."""
    s = max(0, int(supply))
    if cap <= s or p <= 0:
        return max(cap, s) if p <= 0 else s
    ok = lambda n: expected_overflow(n, p, s) <= eps * n * p
    lo, hi = s, cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def supply_estimate(free, connected, wrap_soon, setup_s, talk_mean_s, connected_credit):
    """Agents expected to be free when a call dialed now gets answered (~setup_s from now).
    Talk time is treated as memoryless: P(connected agent finishes within H) = 1 - exp(-H/mean)."""
    hazard = 1 - math.exp(-setup_s / max(talk_mean_s, 1.0))
    exp_conn = connected * hazard * connected_credit
    return free + wrap_soon + exp_conn, {"free": free, "wrap_soon": wrap_soon, "connected_frees": round(exp_conn, 2)}
