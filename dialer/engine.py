"""One dialer worker. Run N of them against the same database; everything they share goes through CAS/transactions."""
import json
import logging
from collections import Counter

from . import repo
from .allocator import Allocator
from .lifecycle import Lifecycle
from .recovery import Recovery

log = logging.getLogger("dialer")


class Dialer:
    def __init__(self, db, cfg, clock, router, pacing, safety, campaign_id="c1", worker_id="w1", fault=None, log_decisions=True):
        self.db, self.cfg, self.clock, self.router = db, cfg, clock, router
        self.pacing, self.safety, self.campaign_id, self.worker_id = pacing, safety, campaign_id, worker_id
        self.life = Lifecycle(db, cfg, clock, router, worker_id, fault)
        self.allocator = Allocator(db, cfg, clock, router, safety, self.life, worker_id, fault)
        self.recovery = Recovery(db, cfg, clock, router, self.life)
        self.log_decisions = log_decisions
        self.counts = Counter()          # safety decisions by action
        self.reasons = Counter()
        self.last_decision = None
        self._last_recover = -1e9

    def tick(self):
        now = self.clock.now()
        # 1. ingest what the carriers told us (stand-in for webhooks), then housekeeping
        for ev in self.router.poll_events():
            self.counts["event:" + self.life.handle_event(ev)] += 1
        self.life.tick_agents()
        self.life.match_held()
        if now - self._last_recover >= 1.0:
            self._last_recover = now
            self.recovery.run()
        # 2. look at the world once
        snap = repo.take_snapshot(self.db.conn(), self.cfg, self.campaign_id, now)
        # 3. shed ringing calls we can no longer cover
        predictive = self.pacing.name == "predictive"
        shed = self.safety.shed_target(snap, predictive)
        if shed:
            self.counts["shed"] += self.allocator.shed(self.campaign_id, shed)
        # 4. pacing proposes, safety disposes
        try:
            req = self.pacing.propose(snap)
        except Exception as e:           # a crashing predictive engine must degrade to progressive, never to "no safety"
            log.exception("pacing failed, falling back to progressive: %s", e)
            from .pacing.progressive import ProgressivePacing
            req = ProgressivePacing(self.cfg).propose(snap)
            self.counts["pacing_error"] += 1
        decision = self.safety.review(req, snap, now)
        self.counts[decision.action] += 1
        for r in decision.reasons:
            self.reasons[r.split("(")[0].split(" ")[0]] += 1
        self.last_decision = (req, decision)
        if self.log_decisions and (req.count or decision.approved):
            with self.db.txn() as c:
                c.execute("INSERT INTO decisions(at,campaign_id,engine,requested,approved,action,reasons,explain) VALUES(?,?,?,?,?,?,?,?)",
                          (now, self.campaign_id, req.engine, req.count, decision.approved, decision.action,
                           json.dumps(decision.reasons), json.dumps({**req.explain, **decision.explain})))
        # 5. only an Approval can reach the allocator
        started = self.allocator.allocate(decision.approval) if decision.approval else 0
        self.counts["calls_started"] += started
        return snap, req, decision, started
