# SmartDialer

A small but working dialer prototype with two pacing modes (progressive and predictive), a Safety Controller that sits
between pacing and the telephone network, two mock carriers that misbehave in different ways, and a deterministic
simulator that I used to break it on purpose.

Python 3.10+ and the standard library. SQLite is the only datastore. `pytest` is the only dependency, and only for the tests.

```
pip install -r requirements.txt
python -m pytest -q              # 46 tests, about 35 seconds
python -m sim.failures           # the five failure cases from the brief, narrated
python -m sim.simulate --quick   # scenarios A-D, provider trouble, agent drop (about 1 minute)
python -m sim.simulate           # full run, about 4 minutes. Results are saved in sim/out/
python -m sim.loadtest           # decision cost, allocation contention, event ingestion
```

## How it fits together

```
Campaign -> Pacing Engine -> Safety Controller -> Call Allocator -> Provider Router -> Carrier A / Carrier B
            (a wish)         (approve / reduce /   (needs a signed     (circuit breaker
                              reject / fall back)   approval)          per carrier)
```

The pacing engine can only produce a `PacingRequest` ("I think we can start 15 more calls"). It does not import the
allocator, the safety controller or any provider. `tests/test_architecture.py` fails the build if it ever does.
The Safety Controller re-derives its own limit from the raw numbers and signs an `Approval`. The allocator refuses
anything that isn't a valid, unexpired, correctly signed approval. So there is nothing for a clever predictive
algorithm to switch off.

Where to look:

| What | Where |
|---|---|
| Progressive pacing | `dialer/pacing/progressive.py` |
| Predictive pacing and the maths | `dialer/pacing/predictive.py`, `dialer/pacing/stats.py` |
| Safety Controller | `dialer/safety.py` |
| Agent and call state machines | `dialer/models.py` (tables), `dialer/repo.py` (compare-and-set), `dialer/lifecycle.py` (events) |
| Allocator (reserve, then dial) | `dialer/allocator.py` |
| Crash recovery and reconciliation | `dialer/recovery.py` |
| Provider interface and two mocks | `dialer/providers/base.py`, `dialer/providers/mock.py` |
| Circuit breaker and routing | `dialer/health.py` |
| One worker's loop | `dialer/engine.py` |
| Simulator, failure cases, load test | `sim/` |
| Diagrams, state machines, failure walkthroughs, scale | `docs/ARCHITECTURE.md` |
| Decisions, final answer, what I'm unsure about | `docs/DECISIONS.md` |

## What the simulator shows

100 agents, 25 minutes of virtual time, clean carrier. "util" is the share of logged-in agent time spent connected to a
borrower. Abandon rate is abandoned / answered, counted pessimistically (see DECISIONS).

| Scenario | Mode | Util | Calls started | Connected | Abandoned | Abandon rate | Dials per connect |
|---|---|---|---|---|---|---|---|
| A: 20% answer, 120s talk | progressive | 47.4% | 3188 | 626 | 0 | 0% | 5.1 |
| | predictive | 61.3% | 4381 | 798 | 14 | 1.7% | 5.5 |
| B: 50%, 90s | progressive | 61.1% | 2131 | 1074 | 0 | 0% | 2.0 |
| | predictive | 72.6% | 2585 | 1276 | 23 | 1.8% | 2.0 |
| C: 70%, 180s | progressive | 80.6% | 1042 | 726 | 0 | 0% | 1.4 |
| | predictive | 82.9% | 1094 | 741 | 21 | 2.8% | 1.5 |
| D: changing (20/120 -> 60/60 -> 30/150) | progressive | 53.6% | 2696 | 914 | 0 | 0% | 2.9 |
| | predictive | 62.8% | 3139 | 1046 | 22 | 2.1% | 3.0 |

Same scenario A with trouble added:

| Condition | Mode | Util | Abandon rate | What the Safety Controller did |
|---|---|---|---|---|
| Slow, flaky carrier B only | progressive | 43.5% | 0.2% | approved everything (nothing to cut) |
| | predictive | 52.9% | 2.5% | fell back to progressive on 984 of 1500 ticks |
| A dies for 3 minutes, B available | progressive | 48.4% | 0.7% | approved everything |
| | predictive | 53.1% | 2.8% | breaker opened, traffic moved to B, mostly progressive while degraded |

Things I'd say out loud in an interview rather than hide:

* Predictive wins by 11 to 14 points when answer rates are low or middling. At a 70% answer rate there is almost nothing
  to gain and the controller spends most of its time in progressive fallback. That is the right behaviour, not a bug.
* The safety layer is deliberately more cautious than the pacing layer, so a large share of decisions are "reduce" or
  "fall back". The abandon rate is the price of the extra utilization, and it is capped, not zero. A strictly zero-abandon
  dialer is the progressive one.
* The "40 agents vanish" run reports a 5.5% abandon rate. Almost all of that is borrowers who were mid-conversation
  with an agent whose connection dropped (31 calls). The dialer cannot prevent that, so it is counted but reported
  separately by reason in `python -m sim.failures`.

## Running it for real

There is no real telecom integration. `TelecomProvider` (in `dialer/providers/base.py`) is five methods: `originate`
(idempotent on our call id), `cancel`, `hangup`, `lookup`, `poll_events`. A Plivo adapter is one class that wraps their
REST API for the first four and exposes the webhook handler as `Lifecycle.handle_event`. I did not build it.

## Known limits

* SQLite means one writer at a time. That is fine for a prototype and for explaining the design; see
  `docs/ARCHITECTURE.md` for what changes on Postgres and at 10k+ agents.
* Time is virtual in the simulator. The concurrency tests use real threads on a real file.
* Talk time is modelled as memoryless when estimating when agents free up. Real talk times are not, and the
  estimate is conservative on purpose, but it is an approximation.
* No calling-hours or DND checks, no agent UI, no auth. They belong in the borrower-selection query and are easy to add there.
