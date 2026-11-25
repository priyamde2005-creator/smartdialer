"""State names and the legal transitions. Everything that mutates state goes through these tables."""


class AgentState:
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


A = AgentState
AGENT_TRANSITIONS = {
    (A.OFFLINE, A.AVAILABLE),
    (A.AVAILABLE, A.RESERVED), (A.AVAILABLE, A.PAUSED), (A.AVAILABLE, A.OFFLINE),
    (A.RESERVED, A.DIALING), (A.RESERVED, A.CONNECTED), (A.RESERVED, A.AVAILABLE), (A.RESERVED, A.OFFLINE),
    (A.DIALING, A.CONNECTED), (A.DIALING, A.AVAILABLE), (A.DIALING, A.OFFLINE),
    (A.CONNECTED, A.WRAP_UP), (A.CONNECTED, A.OFFLINE),
    (A.WRAP_UP, A.AVAILABLE), (A.WRAP_UP, A.PAUSED), (A.WRAP_UP, A.OFFLINE),
    (A.PAUSED, A.AVAILABLE), (A.PAUSED, A.OFFLINE),
}
AGENT_CALL_STATES = (A.RESERVED, A.DIALING, A.CONNECTED)  # states that carry a call id
ALL_AGENT_STATES = (A.OFFLINE, A.AVAILABLE, A.RESERVED, A.DIALING, A.CONNECTED, A.WRAP_UP, A.PAUSED)


class CallState:
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"


C = CallState
ACTIVE_CALL_STATES = (C.QUEUED, C.RESERVED, C.INITIATED, C.RINGING, C.ANSWERED, C.CONNECTED)
TERMINAL_CALL_STATES = (C.COMPLETED, C.FAILED, C.CANCELLED, C.ABANDONED)
# Forward-only ladder. An event that does not move the call strictly up the ladder is stale.
CALL_RANK = {C.QUEUED: 0, C.RESERVED: 1, C.INITIATED: 2, C.RINGING: 3, C.ANSWERED: 4, C.CONNECTED: 5}


def call_transition_allowed(cur: str, to: str) -> bool:
    if cur in TERMINAL_CALL_STATES:
        return False                      # terminal states are absorbing
    if to in TERMINAL_CALL_STATES:
        return True                       # any live call may end
    return CALL_RANK[to] > CALL_RANK[cur]


class BorrowerState:
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    DONE = "DONE"
    EXHAUSTED = "EXHAUSTED"


SQL_ACTIVE = "(" + ",".join(f"'{s}'" for s in ACTIVE_CALL_STATES) + ")"
