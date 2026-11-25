"""Crash recovery + reconciliation. Any worker can run this; it only acts on calls whose lease has expired, and every
action is a compare-and-set, so two reapers (or a reaper and a slow original worker) can't both win."""
import logging

from . import repo
from .lifecycle import Lifecycle
from .models import CallState as C
from .providers.base import ProviderError, ProviderEvent, ProviderTimeout

log = logging.getLogger("dialer")


class Recovery:
    def __init__(self, db, cfg, clock, router, life: Lifecycle):
        self.db, self.cfg, self.clock, self.router, self.life = db, cfg, clock, router, life

    def run(self) -> int:
        now = self.clock.now()
        rows = self.db.conn().execute(
            "SELECT id FROM calls WHERE state IN ('RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED') AND lease_until<? "
            "AND NOT (state='ANSWERED' AND agent_id IS NULL) LIMIT 500", (now,)).fetchall()
        for r in rows:
            self.reconcile(r["id"])
        self.sweep_leaks()
        return len(rows)

    def _lookup(self, call):
        names = [call["provider"]] if call["provider"] else [p.name for p in self.router.providers]
        failed = False
        for n in names:
            p = self.router.get(n)
            try:
                info = p.lookup(call["id"])
                self.router.record(p, True)
                if info is not None:
                    return info, n, False
            except (ProviderTimeout, ProviderError):
                self.router.record(p, False)
                failed = True
        return None, None, failed

    def reconcile(self, call_id):
        now = self.clock.now()
        with self.db.txn() as c:
            call = repo.get_call(c, call_id)
        if call is None or call["state"] not in (C.RESERVED, C.INITIATED, C.RINGING, C.ANSWERED, C.CONNECTED):
            return
        info, pname, unreachable = self._lookup(call)
        if info is None and unreachable:
            live = call["state"] in (C.ANSWERED, C.CONNECTED)      # a human is (probably) on the line
            stuck_for = now - (call["state_since"] or call["created_at"])
            give_up = stuck_for > (self.cfg.max_call_s if live else self.cfg.max_unreconciled_s)
            if give_up:
                log.info("force-failing %s: provider unreachable for %.0fs", call_id, stuck_for)
                self.life.fail_call(call_id, "PROVIDER_UNREACHABLE", result="UNKNOWN", cause="reaper-gave-up")
            else:
                with self.db.txn() as c:
                    repo.call_update(c, call_id, lease_until=now + self.cfg.reconcile_delay_s)
            return
        if info is None:
            # Carrier has never heard of it: the crash happened before originate (or originate never landed).
            if call["state"] in (C.RESERVED, C.INITIATED):
                self.life.fail_call(call_id, "NOT_FOUND_AT_PROVIDER", cause="reaper-not-found")
            else:
                self.life.fail_call(call_id, "LOST_AT_PROVIDER", result="UNKNOWN", cause="reaper-lost")
            return
        # The carrier knows the call. Adopt it: adopt the provider, then replay the status as events.
        if call["state"] == C.RESERVED:
            with self.db.txn() as c:
                repo.call_transition(c, call_id, C.INITIATED, now, "reaper-adopted", provider=pname,
                                     initiated_at=call["created_at"], lease_until=now + self.cfg.max_ring_s + 5)
                if call["agent_id"]:
                    repo.agent_transition(c, call["agent_id"], ("RESERVED",), "DIALING", now, "reaper-adopted", call_id=call_id)
        if info.status == "RINGING":
            if now - (call["initiated_at"] or call["created_at"]) > self.cfg.max_ring_s:
                if not self._provider_down(pname):
                    self.life.run_effects([("cancel", pname, call_id)])
                self.life.fail_call(call_id, "NO_ANSWER", result="NO_ANSWER", cause="ring-timeout")
            else:
                self.life.handle_event(ProviderEvent(pname, call_id, "RINGING", occurred_at=now))
                with self.db.txn() as c:
                    repo.call_update(c, call_id, lease_until=now + 5)
            return
        order = {"ANSWERED": ["RINGING", "ANSWERED"], "COMPLETED": ["COMPLETED"], "FAILED": ["FAILED"]}[info.status]
        for t in order:
            self.life.handle_event(ProviderEvent(pname, call_id, t, info.reason if t == "FAILED" else "", now))
        with self.db.txn() as c:
            repo.call_update(c, call_id, lease_until=now + self.cfg.connected_lease_s)

    def _provider_down(self, name):
        return self.router.health[name].current_state(self.clock.now()) == "OPEN"

    def sweep_leaks(self):
        """Defensive: agents/borrowers stuck RESERVED/DIALING with no live call behind them go back to the pool."""
        now = self.clock.now()
        with self.db.txn() as c:
            for r in c.execute("SELECT a.id FROM agents a WHERE a.state IN ('RESERVED','DIALING') AND a.state_since<? AND NOT EXISTS "
                               "(SELECT 1 FROM calls k WHERE k.agent_id=a.id AND k.state IN ('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED'))",
                               (now - self.cfg.lease_s,)).fetchall():
                repo.release_agent(c, r["id"], now, "leak-sweep")
            c.execute("UPDATE borrowers SET state='PENDING',version=version+1 WHERE state='RESERVED' AND NOT EXISTS "
                      "(SELECT 1 FROM calls k WHERE k.borrower_id=borrowers.id AND k.state IN ('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED'))")
