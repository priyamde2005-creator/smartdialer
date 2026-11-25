"""All SQL lives here. Every function takes an open connection (inside db.txn()) so callers compose atomically."""
import uuid
from typing import Optional

from .models import (ACTIVE_CALL_STATES, AGENT_CALL_STATES, AGENT_TRANSITIONS, AgentState as A, BorrowerState as B,
                     CallState as C, SQL_ACTIVE, call_transition_allowed)
from .snapshot import Snapshot


class IllegalTransition(Exception):
    pass


# ------------------------------------------------------------------ agents
def add_agent(c, agent_id, campaign_id, now, state=A.AVAILABLE):
    c.execute("INSERT INTO agents(id,campaign_id,state,version,state_since,last_heartbeat,idle_since) VALUES(?,?,?,0,?,?,?)",
              (agent_id, campaign_id, state, now, now, now))


def get_agent(c, agent_id):
    return c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()


def agent_transition(c, agent_id, allowed_from, to, now, cause="", call_id=None, wrap_until=None,
                     expect_version: Optional[int] = None) -> bool:
    """Compare-and-set on (state, version). Returns False if somebody else got there first."""
    row = c.execute("SELECT state,version,current_call_id FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None or row["state"] not in allowed_from:
        return False
    if expect_version is not None and row["version"] != expect_version:
        return False
    frm = row["state"]
    if (frm, to) not in AGENT_TRANSITIONS:
        raise IllegalTransition(f"agent {frm}->{to}")
    if to in AGENT_CALL_STATES:
        cur_call = call_id if call_id is not None else row["current_call_id"]
    else:
        cur_call = None
    n = c.execute(
        "UPDATE agents SET state=?,version=version+1,state_since=?,current_call_id=?,wrap_until=?,"
        "idle_since=CASE WHEN ?='AVAILABLE' THEN ? ELSE idle_since END WHERE id=? AND version=?",
        (to, now, cur_call, wrap_until if to == A.WRAP_UP else None, to, now, agent_id, row["version"])).rowcount
    if n != 1:
        return False
    c.execute("INSERT INTO agent_transitions(agent_id,from_state,to_state,at,cause,call_id) VALUES(?,?,?,?,?,?)",
              (agent_id, frm, to, now, cause, cur_call or call_id))
    return True


def release_agent(c, agent_id, now, cause):
    """Send an agent back to the pool (or to PAUSED if they asked for a pause meanwhile)."""
    row = get_agent(c, agent_id)
    if row is None:
        return False
    to = A.PAUSED if row["pause_requested"] and row["state"] == A.WRAP_UP else A.AVAILABLE
    ok = agent_transition(c, agent_id, (A.RESERVED, A.DIALING, A.WRAP_UP), to, now, cause)
    if ok and row["pause_requested"]:
        c.execute("UPDATE agents SET pause_requested=0 WHERE id=?", (agent_id,))
    return ok


def reserve_available_agents(c, campaign_id, n, now, call_ids):
    """Pick the longest-idle AVAILABLE agents and CAS each to RESERVED. Returns [(agent_id, call_id)]."""
    if n <= 0:
        return []
    rows = c.execute("SELECT id,version FROM agents WHERE campaign_id=? AND state='AVAILABLE' "
                     "ORDER BY idle_since LIMIT ?", (campaign_id, n)).fetchall()
    out = []
    for r, cid in zip(rows, call_ids):
        if agent_transition(c, r["id"], (A.AVAILABLE,), A.RESERVED, now, "allocate", call_id=cid, expect_version=r["version"]):
            out.append((r["id"], cid))
    return out


def claim_free_agent(c, campaign_id, now, call_id) -> Optional[str]:
    """Late binding for predictive calls: grab one AVAILABLE agent at the moment the borrower answers."""
    r = c.execute("SELECT id,version FROM agents WHERE campaign_id=? AND state='AVAILABLE' ORDER BY idle_since LIMIT 1",
                  (campaign_id,)).fetchone()
    if r is None:
        return None
    if not agent_transition(c, r["id"], (A.AVAILABLE,), A.RESERVED, now, "bind-on-answer", call_id=call_id, expect_version=r["version"]):
        return None
    return r["id"]


# ------------------------------------------------------------------ borrowers
def add_borrowers(c, campaign_id, rows):
    c.executemany("INSERT INTO borrowers(id,campaign_id,phone,priority,max_attempts) VALUES(?,?,?,?,?)",
                  [(i, campaign_id, ph, pr, ma) for i, ph, pr, ma in rows])


def claim_borrowers(c, campaign_id, n, now):
    if n <= 0:
        return []
    rows = c.execute("SELECT id,phone,version FROM borrowers WHERE campaign_id=? AND state='PENDING' AND dnc=0 "
                     "AND next_attempt_at<=? ORDER BY priority DESC, next_attempt_at, id LIMIT ?",
                     (campaign_id, now, n)).fetchall()
    out = []
    for r in rows:
        k = c.execute("UPDATE borrowers SET state='RESERVED',version=version+1 WHERE id=? AND state='PENDING' AND version=?",
                      (r["id"], r["version"])).rowcount
        if k == 1:
            out.append(r)
    return out


def settle_borrower(c, borrower_id, now, result, cfg):
    """result: CONTACTED | NO_ANSWER | UNKNOWN | NOT_ATTEMPTED | ABANDONED"""
    b = c.execute("SELECT attempts,max_attempts FROM borrowers WHERE id=?", (borrower_id,)).fetchone()
    if b is None:
        return
    if result == "CONTACTED":
        c.execute("UPDATE borrowers SET state='DONE',version=version+1 WHERE id=?", (borrower_id,))
    elif result in ("NO_ANSWER", "UNKNOWN"):
        att = b["attempts"] + 1
        st = B.EXHAUSTED if att >= b["max_attempts"] else B.PENDING
        c.execute("UPDATE borrowers SET state=?,attempts=?,next_attempt_at=?,version=version+1 WHERE id=?",
                  (st, att, now + cfg.retry_delay_s, borrower_id))
    elif result == "ABANDONED":
        c.execute("UPDATE borrowers SET state='PENDING',next_attempt_at=?,version=version+1 WHERE id=?",
                  (now + cfg.abandon_callback_s, borrower_id))
    else:  # NOT_ATTEMPTED: our fault (provider/agent/shed) -> do not burn one of their attempts
        c.execute("UPDATE borrowers SET state='PENDING',next_attempt_at=?,version=version+1 WHERE id=?",
                  (now + cfg.provider_backoff_s, borrower_id))


# ------------------------------------------------------------------ calls
CALL_FIELDS = {"agent_id", "provider", "provider_ref", "lease_until", "initiated_at", "answered_at", "outcome",
               "outcome_at", "ended_at", "end_reason", "hold_deadline", "abandoned", "owner"}


def new_call_id():
    return "call-" + uuid.uuid4().hex[:12]


def create_call(c, call_id, campaign_id, borrower_id, agent_id, now, lease_s, owner):
    c.execute("INSERT INTO calls(id,campaign_id,borrower_id,agent_id,state,version,bound,owner,lease_until,created_at,state_since) "
              "VALUES(?,?,?,?,?,0,?,?,?,?,?)",
              (call_id, campaign_id, borrower_id, agent_id, C.RESERVED, 1 if agent_id else 0, owner, now + lease_s, now, now))
    c.execute("INSERT INTO call_transitions(call_id,from_state,to_state,at,cause) VALUES(?,?,?,?,?)",
              (call_id, C.QUEUED, C.RESERVED, now, "allocate"))


def get_call(c, call_id):
    return c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()


def call_transition(c, call_id, to, now, cause="", **fields) -> bool:
    row = get_call(c, call_id)
    if row is None or not call_transition_allowed(row["state"], to):
        return False
    sets = ["state=?", "version=version+1", "state_since=?"]
    vals = [to, now]
    for k, v in fields.items():
        assert k in CALL_FIELDS, k
        sets.append(f"{k}=?")
        vals.append(v)
    n = c.execute(f"UPDATE calls SET {','.join(sets)} WHERE id=? AND version=?", (*vals, call_id, row["version"])).rowcount
    if n != 1:
        return False
    c.execute("INSERT INTO call_transitions(call_id,from_state,to_state,at,cause) VALUES(?,?,?,?,?)",
              (call_id, row["state"], to, now, cause))
    return True


def call_update(c, call_id, **fields):
    """Metadata-only change (no state transition)."""
    sets, vals = [], []
    for k, v in fields.items():
        assert k in CALL_FIELDS, k
        sets.append(f"{k}=?")
        vals.append(v)
    c.execute(f"UPDATE calls SET {','.join(sets)} WHERE id=?", (*vals, call_id))


def pooled_demand(c, campaign_id) -> int:
    """Unanswered pooled calls + answered-but-waiting calls. Read inside the allocation txn."""
    return c.execute("SELECT COUNT(*) FROM calls WHERE campaign_id=? AND bound=0 AND state IN "
                     "('RESERVED','INITIATED','RINGING','ANSWERED') AND agent_id IS NULL", (campaign_id,)).fetchone()[0]


# ------------------------------------------------------------------ snapshot
def _blend(total, n, prior, weight=10.0):
    return (total + prior * weight) / (n + weight)


def take_snapshot(c, cfg, campaign_id, now) -> Snapshot:
    row = c.execute("SELECT active FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    active = bool(row["active"]) if row else True
    agents = {r[0]: r[1] for r in c.execute("SELECT state,COUNT(*) FROM agents WHERE campaign_id=? GROUP BY state", (campaign_id,))}
    pooled_un = held = bound = 0
    for r in c.execute("SELECT state,bound,COUNT(*) n FROM calls WHERE campaign_id=? AND state IN "
                       "('RESERVED','INITIATED','RINGING','ANSWERED') GROUP BY state,bound", (campaign_id,)):
        if r["bound"]:
            bound += r["n"]
        elif r["state"] == C.ANSWERED:
            held += r["n"]
        else:
            pooled_un += r["n"]
    # ANSWERED pooled calls that already bound to an agent are CONNECTED, so ANSWERED+bound=0 means "waiting".
    outs = [r[0] == "ANSWERED" for r in c.execute(
        "SELECT outcome FROM calls WHERE campaign_id=? AND outcome IS NOT NULL ORDER BY outcome_at DESC LIMIT ?",
        (campaign_id, cfg.long_window))]
    short = outs[:cfg.short_window]
    ab = [r[0] for r in c.execute(
        "SELECT abandoned FROM calls WHERE campaign_id=? AND outcome='ANSWERED' ORDER BY outcome_at DESC LIMIT ?",
        (campaign_id, cfg.long_window))]
    t = c.execute("SELECT COUNT(*),COALESCE(SUM(ended_at-answered_at),0) FROM (SELECT ended_at,answered_at FROM calls "
                  "WHERE campaign_id=? AND state='COMPLETED' AND answered_at IS NOT NULL AND abandoned=0 "
                  "ORDER BY ended_at DESC LIMIT 100)", (campaign_id,)).fetchone()
    s = c.execute("SELECT COUNT(*),COALESCE(SUM(answered_at-initiated_at),0) FROM (SELECT answered_at,initiated_at FROM calls "
                  "WHERE campaign_id=? AND outcome='ANSWERED' AND initiated_at IS NOT NULL ORDER BY outcome_at DESC LIMIT 100)",
                  (campaign_id,)).fetchone()
    talk = _blend(t[1], t[0], cfg.prior_talk_s)
    setup = _blend(s[1], s[0], cfg.prior_setup_s)
    wrap_soon = c.execute("SELECT COUNT(*) FROM agents WHERE campaign_id=? AND state='WRAP_UP' AND wrap_until<=?",
                          (campaign_id, now + setup)).fetchone()[0]
    recent_off = c.execute("SELECT COUNT(*) FROM agent_transitions t JOIN agents a ON a.id=t.agent_id "
                           "WHERE a.campaign_id=? AND t.to_state='OFFLINE' AND t.at>=?", (campaign_id, now - 10)).fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM (SELECT 1 FROM borrowers WHERE campaign_id=? AND state='PENDING' AND dnc=0 "
                        "AND next_attempt_at<=? LIMIT 1000)", (campaign_id, now)).fetchone()[0]
    logged_in = sum(v for k, v in agents.items() if k not in (A.OFFLINE, A.PAUSED))
    return Snapshot(
        now=now, campaign_id=campaign_id, active=active, agents=agents, free=agents.get(A.AVAILABLE, 0),
        connected=agents.get(A.CONNECTED, 0), wrap_up=agents.get(A.WRAP_UP, 0), wrap_soon=wrap_soon,
        logged_in=logged_in, pooled_unanswered=pooled_un, held=held, bound_inflight=bound, pending_borrowers=pending,
        ans_n_long=len(outs), ans_a_long=sum(outs), ans_n_short=len(short), ans_a_short=sum(short),
        abandon_n=len(ab), abandon_a=sum(ab), talk_mean=talk, setup_mean=setup, recent_offline=recent_off)


# ------------------------------------------------------------------ invariants (used by tests + simulator)
def check_invariants(c):
    """Returns a list of violated invariants (empty == healthy)."""
    bad = []
    q = lambda sql: c.execute(sql).fetchall()
    if q(f"SELECT borrower_id FROM calls WHERE state IN {SQL_ACTIVE} GROUP BY borrower_id HAVING COUNT(*)>1"):
        bad.append("borrower has >1 active call")
    if q(f"SELECT agent_id FROM calls WHERE agent_id IS NOT NULL AND state IN {SQL_ACTIVE} GROUP BY agent_id HAVING COUNT(*)>1"):
        bad.append("agent has >1 active call")
    for r in q("SELECT a.id,a.state,a.current_call_id FROM agents a WHERE a.state IN ('RESERVED','DIALING','CONNECTED')"):
        k = c.execute(f"SELECT 1 FROM calls WHERE id=? AND agent_id=? AND state IN {SQL_ACTIVE}", (r["current_call_id"], r["id"])).fetchone()
        if not k:
            bad.append(f"agent {r['id']} {r['state']} without matching active call")
    for r in q("SELECT id FROM calls WHERE state='CONNECTED' AND agent_id IS NULL"):
        bad.append(f"call {r['id']} CONNECTED without agent")
    for r in q("SELECT id FROM calls WHERE state IN ('COMPLETED','FAILED','CANCELLED','ABANDONED') AND ended_at IS NULL AND 0"):
        bad.append("unreachable")
    for r in q("SELECT b.id FROM borrowers b WHERE b.state='RESERVED' AND NOT EXISTS "
               f"(SELECT 1 FROM calls k WHERE k.borrower_id=b.id AND k.state IN {SQL_ACTIVE})"):
        bad.append(f"borrower {r['id']} RESERVED with no active call")
    # no duplicate transitions: each (call, to_state) at most once
    if q("SELECT call_id,to_state FROM call_transitions GROUP BY call_id,to_state HAVING COUNT(*)>1"):
        bad.append("duplicate call transition")
    return bad
