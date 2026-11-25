# Decisions

Each one is: what I chose, why, what problem it solves, what it makes harder.

## 1. One SQLite file, no Redis, no Kafka, no queue

**Chose:** a single relational store holding agents, borrowers, calls, the transition log and the event ledger.
**Why:** the hard part of this problem is that agent state, call state and borrower state must change together. A
transaction gives me that for free. Redis plus a database would give me two sources of truth and a question the brief
asks directly ("the DB says AVAILABLE, the cache says RESERVED, who wins?") that I'd then have to answer.
**Solves:** atomic reservation, a single place to audit, crash safety by rollback, and a prototype anyone can run with no setup.
**Makes harder:** one writer at a time. It's the first thing to go (see ARCHITECTURE.md section 7). The code is written so
the move to Postgres is mechanical: every write is a CAS or a short transaction, and SQL is confined to `repo.py`.

## 2. Compare-and-set plus a lease on every reservation, not locks

**Chose:** `UPDATE ... WHERE state=? AND version=?` for every transition, with `lease_until` on reserved calls.
**Why:** locks held across a network call to a carrier are how you get a stuck system when a worker dies. A lease is
just a timestamp; anyone can read it, nobody has to release it.
**Solves:** two workers racing; workers dying mid-call; slow workers coming back after a reaper already acted (their CAS fails).
**Makes harder:** every transition has to handle "somebody else got there first". It returns False, not an exception.

## 3. Do not hold a transaction open while talking to the carrier

**Chose:** reserve (txn) -> originate (no txn) -> mark INITIATED (txn).
**Why:** carrier calls take seconds and sometimes hang. A transaction open that long blocks everyone.
**Solves:** throughput and lock contention.
**Makes harder:** there is now a crash window between "carrier has the call" and "we know it". That window is exactly
what `originate` idempotency (our call id is the key) and the reaper's `lookup` exist to close.

## 4. Events: one ledger row per (call, event type), forward-only states, terminal states absorb

**Chose:** dedup on `(call_id, event_type)` in the same transaction as the state change; a rank ladder for ordering.
**Why:** I can't trust carriers to deliver once or in order. Two independent defences (dedup, then rank) mean a bug in
one doesn't corrupt state.
**Solves:** duplicates, reordering, redelivery after a crash.
**Makes harder:** a COMPLETED that arrives before ANSWERED has to be interpreted. I count it as an abandoned answered
call. That makes the abandon metric pessimistic, which I'd rather have than optimistic in a compliance setting.

## 5. Predictive pacing and the Safety Controller are separate, and safety does its own maths

**Chose:** pacing returns a request; safety recomputes a ceiling from raw numbers with pessimistic inputs and signs an
approval; the allocator needs the signature; pacing imports none of that (and a test enforces it).
**Why:** the brief says correctness comes before cleverness. If the controller trusted the pacing engine's estimate, a
bad estimate would pass straight through. Because it uses different (worse-case) inputs, the two disagree when the
estimate is shaky, which is exactly when it matters.
**Solves:** "the predictive algorithm should not be able to switch the safety mechanism off".
**Makes harder:** two models of the same quantity to keep roughly in line. If safety is too cautious, predictive
adds little (scenario C). I tuned the safety tolerance until abandons sat around 2% rather than near the 3% limit.

## 6. Ceilings, not deltas

**Chose:** an approval says "at most N pooled calls in flight", and the allocator re-counts inside its transaction.
**Why:** several workers acting on the same stale snapshot would each add their delta and overshoot.
**Solves:** the multi-worker overdial. Covered by a threaded test.
**Makes harder:** a worker can get less than it was approved for. That's the point.

## 7. Late binding of agents for predictive calls

**Chose:** predictive calls are dialed with no agent. The agent is claimed (CAS) at the moment the borrower answers.
Progressive calls reserve an agent first.
**Why:** that is what predictive dialing is. Reserving at dial time would be progressive.
**Solves:** utilization.
**Makes harder:** a hold queue and an abandon path exist. They are bounded by `hold_s` and counted.

## 8. Ambiguous timeouts are not retried; they're reconciled

**Chose:** on a carrier timeout, keep the reservation, wait a few seconds, call `lookup(call_id)`.
**Why:** retrying could put a second phone call to a borrower. Lookup is authoritative.
**Makes harder:** a call can sit in limbo for a few seconds, and up to 30s if the carrier is unreachable. During that
time it counts against the in-flight ceiling, so a carrier outage reduces dialing sooner than the breaker does.

## 9. A virtual clock for everything time-based

**Chose:** every component asks a `Clock`. The simulator advances it by hand.
**Why:** failure scenarios involve leases, timeouts and 40-minute campaigns. With a real clock I'd be sleeping for an hour
per test or not testing them at all.
**Makes harder:** a real deployment needs the same code run against `RealClock`, which I have not stress-tested.

## What I'm least confident about

1. **The memoryless talk-time assumption.** I used it because it needs no per-agent state, and the pessimistic credit
   (80%) is meant to absorb the error. With very regular talk times (say always 90 seconds) the estimate of who frees up
   soon would be wrong in a way that hurts utilization more than safety. I'd replace it with an empirical
   remaining-time distribution per campaign.
2. **Window sizes and the abandon feedback.** The rolling window of 200 answered calls, a 2% target and a 3% hard limit
   produce a controller that sometimes oscillates between predictive and progressive (see the fallback counts in the
   scenario output). It never breaches the limit in my runs, but I tuned it by hand on the scenarios I wrote, and a
   regulator measures over days, not 200 calls. A proper version tracks a campaign-day budget.
3. **The reaper's 30s give-up for unreachable carriers.** It's a guess. Too short and we fail calls that are fine; too
   long and agents are reserved for nothing.
4. **Shedding.** Cancelling ringing calls rings a phone for nothing. I limited it to availability drops and large
   overshoot, but I haven't modelled the borrower-experience cost.
5. **Per-worker in-memory state** (token bucket, circuit breaker) is not shared between workers, which is
   fine for correctness (the database enforces the hard limits) but means N workers get N times the rate limit unless
   it's moved to shared state.

## If I had another week

* Postgres with `SKIP LOCKED` and a real two-process test (not threads) with `kill -9` at random points.
* A property-based test that generates random event orderings, duplications and crashes and asserts the invariants
  (`repo.check_invariants`) after each step. The pieces are there; it's an afternoon.
* An empirical talk-time model, and a campaign-day abandonment budget instead of the rolling window.
* A real Plivo adapter behind the existing interface.
* A pool-per-shard design with leader leases and fencing tokens, and per-pool counters instead of `COUNT(*)`.
* Calling-hours and DND filters in `claim_borrowers`.

---

# The final question

> How would you build a SmartDialer that gets as much of the utilization benefit of predictive dialing as possible,
> while retaining the deterministic safety characteristics of progressive dialing?

Predict the supply of agents, not just the demand from borrowers, and make the overdial a small, budgeted, independently
checked extra on top of a progressive core.

Progressive dialing is safe for one reason: every call has an agent before it is dialed, so there is nothing to
abandon. Most of the idle time that predictive dialing recovers comes from two places: the agent finishing a call
while the next one hasn't started ringing yet, and the long run of unanswered calls before one connects. You can
recover a good part of the first by dialing for agents who are about to be free (they are in wrap-up with a known
timer, or are late in a call) without gambling on the answer rate at all. For the second you do need to overdial, and
that is where the risk lives.

So I'd keep the deterministic part and bound the gamble:

1. Every call still goes through a controller that no pacing logic can bypass, and the fallback is always "one
   reserved agent per call".
2. Overdial only against a worst-case estimate (an upper bound on the answer rate, a lower bound on agents freeing
   up), sized so the expected abandon rate stays under a target below the legal limit.
3. Treat the abandon rate as a budget that the controller spends and watches. As it's used up, the allowed overdial
   shrinks to zero on its own, and at the limit the system is simply progressive.
4. Fail toward progressive on anything it can't be sure of: not enough history, a degraded carrier, agents
   disappearing, slow call setup.
5. Make the remaining failure (an answered call with no agent) a first-class, bounded path: a short hold, then a
   counted abandon and a callback, never a silent drop.

In the simulator that gets 61 percent utilization against 47 percent for progressive at a 20 percent answer rate, 73
against 61 at 50 percent, and about the same when almost everyone answers, with abandons at or below 3 percent in scenarios A to D. The price is that it is never zero. Zero is progressive.

