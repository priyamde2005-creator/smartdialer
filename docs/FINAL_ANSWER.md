# Final question

**How would you build a SmartDialer that gets as much of the utilization benefit of predictive dialing as possible, while retaining the deterministic safety characteristics of progressive dialing?**

Make progressive dialing the safe core, and allow predictive overdial only as a small, budgeted extra on top of it.

1. **Recover what is safe first.** Dial for agents who are about to be free (in wrap-up with a timer, or near the end of a call). This needs no guess about answer rates, so it carries no abandon risk.
2. **Overdial only against worst-case numbers.** Use an upper bound on the answer rate and a lower bound on agents freeing up, sized so expected abandons stay below the legal limit.
3. **Treat the abandon rate as a budget.** As it gets used up, the allowed overdial shrinks to zero, and at the limit the system is simply progressive.
4. **Keep the safety layer separate and impossible to bypass.** Pacing can only make a request. A separate controller decides and signs an approval, and the allocator refuses anything unsigned. The fallback is always "one reserved agent per call".
5. **Fall back to progressive when unsure.** That means little history, a degraded carrier, agents disappearing, or slow call setup.
6. **Handle the leftover failure explicitly.** If a call is answered and no agent is free, it gets a short hold, then a counted abandon and a callback, never a silent drop.

In my simulator this gave 61% utilization against 47% for progressive at a 20% answer rate, with abandons around 2% (full 25-minute run, `python -m sim.simulate`). It is never zero, because zero abandons is exactly what progressive means.
