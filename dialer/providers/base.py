"""The only thing the dialer knows about telecom. Anything that implements this can be plugged in (Plivo, Twilio, ...)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


class ProviderError(Exception):
    """Definite failure: the provider told us it did NOT place the call. Safe to retry elsewhere."""


class ProviderTimeout(Exception):
    """Ambiguous: we don't know whether the call was placed. Must reconcile via lookup(), never blindly retry."""


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    call_id: str          # our id; passed to the provider at originate() and echoed back
    type: str             # RINGING | ANSWERED | COMPLETED | FAILED
    reason: str = ""      # for FAILED: NO_ANSWER | BUSY | ERROR | CANCELLED ...
    occurred_at: float = 0.0


@dataclass(frozen=True)
class ProviderCallInfo:
    status: str           # RINGING | ANSWERED | COMPLETED | FAILED
    reason: str = ""


class TelecomProvider(ABC):
    name: str

    @abstractmethod
    def originate(self, call_id: str, phone: str) -> str:
        """Idempotent on call_id: calling twice must not place two calls. Returns a provider reference."""

    @abstractmethod
    def cancel(self, call_id: str) -> None:
        """Best effort: stop a call that has not been answered yet."""

    @abstractmethod
    def hangup(self, call_id: str) -> None:
        """End a call in any state."""

    @abstractmethod
    def lookup(self, call_id: str) -> Optional[ProviderCallInfo]:
        """Authoritative status; None means the provider has never heard of this call_id."""

    @abstractmethod
    def poll_events(self, now: float) -> List[ProviderEvent]:
        """Events that are deliverable at `now` (stand-in for a webhook / event stream)."""
