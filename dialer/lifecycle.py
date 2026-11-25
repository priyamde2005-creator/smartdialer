"""Everything that moves a call or an agent. One provider event == one DB transaction, so a worker that dies
mid-event leaves nothing half-applied, and a redelivered event is simply processed again."""
import logging

from . import repo
from .models import AgentState as A, CallState as C, TERMINAL_CALL_STATES, call_transition_allowed
from .providers.base import ProviderEvent

log = logging.getLogger("dialer")
TARGET = {"RINGING": C.RINGING, "ANSWERED": C.ANSWERED, "COMPLETED": C.COMPLETED, "FAILED": C.FAILED}


class Lifecycle:
    def __init__(self, db, cfg, clock, router, worker_id="w1", fault=None):
        self.db, self.cfg, self.clock, self.router, self.worker_id = db, cfg, clock, router, worker_id
        self.fault = fault or (lambda point, **kw: None)     # test hook: raise SimulatedCrash at a named point

    # ------------------------------------------------------------ provider events
    def handle_event(self, ev: ProviderEvent) -> str:
        now = self.clock.now()
        effects = []
        with self.db.txn() as c:
            outcome = self._apply(c, ev, now, effects)
            self.fault("before_commit", ev=ev)
            if outcome != "DUPLICATE":
                c.execute("UPDATE provider_events SET outcome=? WHERE call_id=? AND event_type=?", (outcome, ev.call_id, ev.type))
        self.fault("after_commit", ev=ev)
        self.run_effects(effects)
        return outcome

    def _apply(self, c, ev, now, effects) -> str:
        k = c.execute("INSERT OR IGNORE INTO provider_events(call_id,event_type,provider,received_at) VALUES(?,?,?,?)",
                      (ev.call_id, ev.type, ev.provider, now)).rowcount
        if k == 0:
            return "DUPLICATE"             # exact same event already applied (or applied by a reconcile)
        call = repo.get_call(c, ev.call_id)
        if call is None:
            return "UNKNOWN_CALL"
        state = call["state"]
        if state in TERMINAL_CALL_STATES:
            if ev.type == "ANSWERED" and state != C.COMPLETED and not call["abandoned"]:
                # We already gave up on this call (cancelled / failed / shed) but a human just picked up.
                # Hang up and count it as an abandoned answered call: that is the honest compliance number.
                repo.call_update(c, ev.call_id, abandoned=1, outcome="ANSWERED", outcome_at=now,
                                 answered_at=call["answered_at"] or now)
                effects.append(("hangup", ev.provider, ev.call_id))
                return "ORPHAN_ANSWER_HANGUP"
            return "IGNORED_TERMINAL"
        target = TARGET.get(ev.type)
        if target is None or not call_transition_allowed(state, target):
            return "IGNORED_STALE"         # e.g. RINGING arriving after ANSWERED
        if ev.type == "RINGING":
            if call["initiated_at"] and ev.provider in self.router.health:
                self.router.health[ev.provider].record_setup(now - call["initiated_at"])
            repo.call_transition(c, ev.call_id, C.RINGING, now, "provider:RINGING", provider=ev.provider,
                                 lease_until=now + self.cfg.max_ring_s + 5)
            return "APPLIED"
        if ev.type == "ANSWERED":
            repo.call_transition(c, ev.call_id, C.ANSWERED, now, "provider:ANSWERED", provider=ev.provider,
                                 answered_at=now, outcome="ANSWERED", outcome_at=now,
                                 lease_until=now + self.cfg.connected_lease_s)
            self._bind_or_hold(c, repo.get_call(c, ev.call_id), now)
            return "APPLIED"
        if ev.type == "COMPLETED":
            return self._on_completed(c, call, now, ev.provider)
        return self._finish_failed(c, call, now, ev.reason or "ERROR", "provider:FAILED")

    # ------------------------------------------------------------ binding agents to answered calls
    def _bind_or_hold(self, c, call, now):
        cid, aid = call["id"], call["agent_id"]
        bound = None
        if aid and repo.agent_transition(c, aid, (A.DIALING, A.RESERVED), A.CONNECTED, now, "answered", call_id=cid):
            bound = aid
        elif not aid:
            bound = repo.claim_free_agent(c, call["campaign_id"], now, cid)
            if bound:
                repo.agent_transition(c, bound, (A.RESERVED,), A.CONNECTED, now, "answered", call_id=cid)
                repo.call_update(c, cid, agent_id=bound)
        if bound:
            repo.call_transition(c, cid, C.CONNECTED, now, "bridged", lease_until=now + self.cfg.connected_lease_s)
        else:
            repo.call_update(c, cid, hold_deadline=now + self.cfg.hold_s)   # waits in the hold queue

    def match_held(self):
        """Pair answered-but-unbridged calls with free agents (FIFO); abandon the ones that waited too long."""
        now = self.clock.now()
        effects = []
        with self.db.txn() as c:
            rows = c.execute("SELECT * FROM calls WHERE state='ANSWERED' AND agent_id IS NULL ORDER BY answered_at").fetchall()
            for call in rows:
                aid = repo.claim_free_agent(c, call["campaign_id"], now, call["id"])
                if aid:
                    repo.agent_transition(c, aid, (A.RESERVED,), A.CONNECTED, now, "bridged-from-hold", call_id=call["id"])
                    repo.call_update(c, call["id"], agent_id=aid)
                    repo.call_transition(c, call["id"], C.CONNECTED, now, "bridged-from-hold",
                                         lease_until=now + self.cfg.connected_lease_s)
                elif call["hold_deadline"] is not None and now >= call["hold_deadline"]:
                    repo.call_transition(c, call["id"], C.ABANDONED, now, "hold-timeout", abandoned=1, ended_at=now,
                                         end_reason="ABANDONED_NO_AGENT")
                    repo.settle_borrower(c, call["borrower_id"], now, "ABANDONED", self.cfg)
                    effects.append(("hangup", call["provider"], call["id"]))
        self.run_effects(effects)

    # ------------------------------------------------------------ completion / failure
    def _on_completed(self, c, call, now, provider):
        cid, state, aid = call["id"], call["state"], call["agent_id"]
        if state == C.CONNECTED:
            repo.call_transition(c, cid, C.COMPLETED, now, "provider:COMPLETED", ended_at=now, end_reason="COMPLETED")
            if aid:
                repo.agent_transition(c, aid, (A.CONNECTED,), A.WRAP_UP, now, "call-completed",
                                      wrap_until=now + self.cfg.wrap_up_s)
            repo.settle_borrower(c, call["borrower_id"], now, "CONTACTED", self.cfg)
        elif state == C.ANSWERED:          # caller hung up while waiting for an agent
            repo.call_transition(c, cid, C.ABANDONED, now, "hangup-on-hold", abandoned=1, ended_at=now,
                                 end_reason="CALLER_HUNG_UP_ON_HOLD")
            repo.settle_borrower(c, call["borrower_id"], now, "ABANDONED", self.cfg)
        else:
            # COMPLETED but we never saw ANSWERED. The call connected at the carrier but we never bridged an
            # agent, so we can't claim a successful contact. Count it as abandoned (pessimistic = safe).
            repo.call_transition(c, cid, C.COMPLETED, now, "provider:COMPLETED(unobserved-answer)", ended_at=now,
                                 end_reason="COMPLETED_UNOBSERVED_ANSWER", abandoned=1, outcome="ANSWERED",
                                 outcome_at=now, answered_at=call["answered_at"] or now)
            if aid:
                repo.release_agent(c, aid, now, "call-ended-unbridged")
            repo.settle_borrower(c, call["borrower_id"], now, "UNKNOWN", self.cfg)
        return "APPLIED"

    def _finish_failed(self, c, call, now, reason, cause, result=None):
        cid, state, aid = call["id"], call["state"], call["agent_id"]
        if state in (C.ANSWERED, C.CONNECTED):                      # dropped mid-call
            repo.call_transition(c, cid, C.FAILED, now, cause, ended_at=now, end_reason="DROPPED:" + reason,
                                 abandoned=1 if state == C.ANSWERED else call["abandoned"])
            if aid:
                repo.agent_transition(c, aid, (A.CONNECTED,), A.WRAP_UP, now, "call-dropped",
                                      wrap_until=now + self.cfg.wrap_up_s)
            repo.settle_borrower(c, call["borrower_id"], now, "UNKNOWN", self.cfg)
            return "APPLIED"
        noans = reason in ("NO_ANSWER", "BUSY")
        fields = dict(ended_at=now, end_reason=reason)
        if noans:
            fields.update(outcome="NO_ANSWER", outcome_at=now)
        repo.call_transition(c, cid, C.FAILED, now, cause, **fields)
        if aid:
            repo.release_agent(c, aid, now, "call-failed")
        repo.settle_borrower(c, call["borrower_id"], now, result or ("NO_ANSWER" if noans else "NOT_ATTEMPTED"), self.cfg)
        return "APPLIED"

    def fail_call(self, call_id, reason, result="NOT_ATTEMPTED", cause="internal"):
        """Internal failure path (provider rejected, reaper gave up, ...). Idempotent."""
        now = self.clock.now()
        with self.db.txn() as c:
            call = repo.get_call(c, call_id)
            if call is None or call["state"] in TERMINAL_CALL_STATES:
                return False
            self._finish_failed(c, call, now, reason, cause, result)
        return True

    def cancel_calls(self, call_ids, reason, cause):
        """Cancel not-yet-answered calls (shedding, agent loss). Borrowers go back to the queue untouched."""
        now = self.clock.now()
        effects = []
        with self.db.txn() as c:
            for cid in call_ids:
                call = repo.get_call(c, cid)
                if call is None or call["state"] not in (C.RESERVED, C.INITIATED, C.RINGING):
                    continue
                repo.call_transition(c, cid, C.CANCELLED, now, cause, ended_at=now, end_reason=reason)
                if call["agent_id"]:
                    repo.release_agent(c, call["agent_id"], now, cause)
                repo.settle_borrower(c, call["borrower_id"], now, "NOT_ATTEMPTED", self.cfg)
                if call["provider"]:
                    effects.append(("cancel", call["provider"], cid))
        self.run_effects(effects)

    # ------------------------------------------------------------ agents
    def agent_offline(self, agent_id, cause="disconnect"):
        """Agent vanished (logout, websocket drop, heartbeat timeout). Resolve whatever call they were holding."""
        now = self.clock.now()
        effects = []
        with self.db.txn() as c:
            ag = repo.get_agent(c, agent_id)
            if ag is None or ag["state"] == A.OFFLINE:
                return False
            call = repo.get_call(c, ag["current_call_id"]) if ag["current_call_id"] else None
            repo.agent_transition(c, agent_id, tuple(s for s in (A.AVAILABLE, A.RESERVED, A.DIALING, A.CONNECTED, A.WRAP_UP, A.PAUSED)),
                                  A.OFFLINE, now, cause)
            if call is not None and call["state"] not in TERMINAL_CALL_STATES:
                if call["state"] in (C.RESERVED, C.INITIATED, C.RINGING):
                    repo.call_transition(c, call["id"], C.CANCELLED, now, "agent-lost", ended_at=now, end_reason="AGENT_LOST")
                    repo.settle_borrower(c, call["borrower_id"], now, "NOT_ATTEMPTED", self.cfg)
                    if call["provider"]:
                        effects.append(("cancel", call["provider"], call["id"]))
                else:   # a human was already on the line and the agent is gone
                    repo.call_transition(c, call["id"], C.FAILED, now, "agent-lost", ended_at=now,
                                         end_reason="AGENT_LOST_MID_CALL", abandoned=1)
                    repo.settle_borrower(c, call["borrower_id"], now, "ABANDONED", self.cfg)
                    effects.append(("hangup", call["provider"], call["id"]))
        self.run_effects(effects)
        return True

    def tick_agents(self, hb_exempt=()):
        """WRAP_UP -> AVAILABLE when the timer expires; heartbeat-timeout -> OFFLINE."""
        now = self.clock.now()
        with self.db.txn() as c:
            for r in c.execute("SELECT id,pause_requested FROM agents WHERE state='WRAP_UP' AND wrap_until<=?", (now,)).fetchall():
                repo.release_agent(c, r["id"], now, "wrap-up-done")
        stale = self.db.conn().execute(
            "SELECT id FROM agents WHERE state NOT IN ('OFFLINE') AND last_heartbeat<?", (now - self.cfg.heartbeat_timeout_s,)).fetchall()
        for r in stale:
            self.agent_offline(r["id"], "heartbeat-timeout")

    # ------------------------------------------------------------ side effects (after commit, at-least-once, idempotent)
    def run_effects(self, effects):
        for kind, pname, cid in effects:
            try:
                p = self.router.get(pname) if pname else None
                if p is None:
                    continue
                (p.cancel if kind == "cancel" else p.hangup)(cid)
            except Exception as e:      # provider down: reaper/lookup will mop up
                log.warning("effect %s %s failed: %s", kind, cid, e)
