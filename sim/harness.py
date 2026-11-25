"""Deterministic simulation harness (virtual clock). Used by the scenarios, the failure demos and the tests."""
import os
import random
import tempfile

from dialer import repo
from dialer.clock import SimClock
from dialer.config import Config
from dialer.db import Database
from dialer.engine import Dialer
from dialer.health import ProviderRouter
from dialer.pacing.predictive import PredictivePacing
from dialer.pacing.progressive import ProgressivePacing
from dialer.providers.mock import World, make_provider_a, make_provider_b
from dialer.safety import SafetyController


class Harness:
    def __init__(self, mode="predictive", n_agents=100, answer_rate=0.3, talk=120.0, providers="A", n_borrowers=None,
                 seed=1, cfg=None, workers=1, fault=None, path=None, campaign="c1"):
        self.cfg = cfg or Config()
        self.clock = SimClock()
        self.path = path or os.path.join(tempfile.mkdtemp(prefix="sd-"), "dialer.db")
        self.db = Database(self.path, fast=True)
        self.world = World(answer_rate, talk, seed)
        self.providers = []
        if "A" in providers:
            self.providers.append(make_provider_a(self.world, self.clock, seed + 10))
        if "B" in providers:
            self.providers.append(make_provider_b(self.world, self.clock, seed + 20))
        self.pA = next((p for p in self.providers if p.name == "A"), None)
        self.pB = next((p for p in self.providers if p.name == "B"), None)
        self.campaign, self.mode, self.n_agents = campaign, mode, n_agents
        self.silent = set()
        self.agent_ids = [f"a{i:05d}" for i in range(n_agents)]
        now = self.clock.now()
        rng = random.Random(seed)
        with self.db.txn() as c:
            c.execute("INSERT INTO campaigns(id,active) VALUES(?,1)", (campaign,))
            for a in self.agent_ids:
                repo.add_agent(c, a, campaign, now)
            nb = n_borrowers or n_agents * 400
            repo.add_borrowers(c, campaign, [(f"b{i:07d}", f"+91{rng.randrange(10**9, 10**10)}", rng.randrange(0, 3), self.cfg.max_attempts)
                                             for i in range(nb)])
        self.dialers = []
        for w in range(workers):
            router = ProviderRouter(self.providers, self.cfg, self.clock)
            pacing = PredictivePacing(self.cfg) if mode == "predictive" else ProgressivePacing(self.cfg)
            self.dialers.append(Dialer(self.db, self.cfg, self.clock, router, pacing, SafetyController(self.cfg, router),
                                       campaign, f"w{w + 1}", fault=fault))
        self.dialer = self.dialers[0]
        self.router = self.dialer.router
        # accumulators
        self.t_logged = self.t_connected = self.t_wrap = 0.0
        self.series = []
        self.ticks = 0
        self.sum_requested = self.sum_approved = 0

    # ---------------------------------------------------------------- driving
    def heartbeat(self):
        now = self.clock.now()
        with self.db.txn() as c:
            if not self.silent:
                c.execute("UPDATE agents SET last_heartbeat=? WHERE state!='OFFLINE'", (now,))
            else:
                ph = ",".join("?" * len(self.silent))
                c.execute(f"UPDATE agents SET last_heartbeat=? WHERE state!='OFFLINE' AND id NOT IN ({ph})", (now, *self.silent))

    def step(self, dt=1.0, worker=None):
        self.clock.advance(dt)
        self.heartbeat()
        snap = req = dec = None
        for d in (self.dialers if worker is None else [self.dialers[worker]]):
            snap, req, dec, _ = d.tick()
        self.ticks += 1
        self.t_logged += snap.logged_in * dt
        self.t_connected += snap.connected * dt
        self.t_wrap += snap.wrap_up * dt
        self.sum_requested += req.count
        self.sum_approved += dec.approved
        if self.ticks % 10 == 0:
            self.series.append((round(self.clock.now() - 1000), snap.logged_in, snap.connected, snap.free, snap.pooled_demand,
                                req.count, dec.approved, dec.action, round(self.abandon_rate(), 4)))
        return snap, req, dec

    def run(self, seconds, events=None, dt=1.0):
        t0 = self.clock.now()
        events = sorted((events or {}).items())
        i = 0
        while self.clock.now() - t0 < seconds:
            while i < len(events) and self.clock.now() - t0 >= events[i][0]:
                events[i][1](self)
                i += 1
            self.step(dt)

    def restart_worker(self, i=0, fault=None):
        """Simulate a crashed worker coming back as a brand new process (no in-memory state survives)."""
        router = ProviderRouter(self.providers, self.cfg, self.clock)
        pacing = PredictivePacing(self.cfg) if self.mode == "predictive" else ProgressivePacing(self.cfg)
        self.dialers[i] = Dialer(self.db, self.cfg, self.clock, router, pacing, SafetyController(self.cfg, router),
                                 self.campaign, f"w{i + 1}r", fault=fault)
        if i == 0:
            self.dialer, self.router = self.dialers[0], router
        return self.dialers[i]

    def make_call(self, bound=True, provider="A"):
        """Test helper: put one call into INITIATED the same way the allocator would, without going through pacing."""
        from dialer.models import AgentState as A, CallState as C
        now = self.clock.now()
        with self.db.txn() as c:
            b = repo.claim_borrowers(c, self.campaign, 1, now)[0]
            cid = repo.new_call_id()
            aid = repo.reserve_available_agents(c, self.campaign, 1, now, [cid])[0][0] if bound else None
            repo.create_call(c, cid, self.campaign, b["id"], aid, now, self.cfg.lease_s, "test")
            repo.call_transition(c, cid, C.INITIATED, now, "test", provider=provider, initiated_at=now, lease_until=now + 50)
            if aid:
                repo.agent_transition(c, aid, (A.RESERVED,), A.DIALING, now, "test", call_id=cid)
        return cid, aid

    def call(self, cid):
        return dict(self.db.conn().execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone())

    def agent(self, aid):
        return dict(self.db.conn().execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())

    def transitions(self, cid):
        return [(r["from_state"], r["to_state"]) for r in
                self.db.conn().execute("SELECT * FROM call_transitions WHERE call_id=? ORDER BY id", (cid,))]

    def send(self, cid, typ, reason="", provider="A"):
        from dialer.providers.base import ProviderEvent
        return self.dialer.life.handle_event(ProviderEvent(provider, cid, typ, reason, self.clock.now()))

    # ---------------------------------------------------------------- chaos helpers
    def drop_agents(self, n, silent=False):
        rows = self.db.conn().execute("SELECT id FROM agents WHERE state!='OFFLINE' ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        for r in rows:
            if silent:
                self.silent.add(r["id"])
            else:
                self.dialer.life.agent_offline(r["id"], "disconnect")
        return [r["id"] for r in rows]

    # ---------------------------------------------------------------- reporting
    def abandon_rate(self):
        c = self.db.conn()
        a, n = c.execute("SELECT COALESCE(SUM(abandoned),0), COUNT(*) FROM calls WHERE outcome='ANSWERED'").fetchone()
        return a / n if n else 0.0

    def report(self):
        c = self.db.conn()
        q = lambda s: c.execute(s).fetchone()[0]
        answered = q("SELECT COUNT(*) FROM calls WHERE outcome='ANSWERED'")
        ab = q("SELECT COALESCE(SUM(abandoned),0) FROM calls WHERE outcome='ANSWERED'")
        connected = q("SELECT COUNT(DISTINCT call_id) FROM call_transitions WHERE to_state='CONNECTED'")
        initiated = q("SELECT COUNT(*) FROM calls WHERE initiated_at IS NOT NULL")
        d = self.dialer
        return {
            "utilization": self.t_connected / self.t_logged if self.t_logged else 0,
            "occupancy": (self.t_connected + self.t_wrap) / self.t_logged if self.t_logged else 0,
            "initiated": initiated, "answered": answered, "connected": connected, "abandoned": ab,
            "abandon_rate": ab / answered if answered else 0,
            "dials_per_connect": initiated / connected if connected else 0,
            "avg_requested": self.sum_requested / max(self.ticks, 1), "avg_approved": self.sum_approved / max(self.ticks, 1),
            "safety": {k: v for k, v in d.counts.items() if k in ("APPROVE", "REDUCE", "REJECT", "FALLBACK_PROGRESSIVE", "shed")},
            "reasons": dict(d.reasons.most_common(4)),
            "violations": repo.check_invariants(c),
        }
