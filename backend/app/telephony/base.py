"""Telephony provider abstraction layer."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class CallRequest:
    to_number: str
    from_number: str
    webhook_url: str
    status_callback_url: str
    timeout: int = 30


@dataclass
class CallResult:
    provider_call_sid: str
    status: str


@dataclass
class TwiMLResponse:
    """Represents a telephony markup response (TwiML-like)."""

    xml: str


class TelephonyProvider(ABC):
    @abstractmethod
    async def make_call(self, request: CallRequest) -> CallResult:
        """Initiate an outbound call."""
        ...

    @abstractmethod
    async def end_call(self, call_sid: str) -> None:
        """End an active call."""
        ...

    @abstractmethod
    def generate_greeting(self, message: str, voice: str) -> TwiMLResponse:
        """Generate TwiML for a greeting."""
        ...

    @abstractmethod
    def generate_gather(
        self, prompt: str, voice: str, action_url: str, num_digits: int = 1, timeout: int = 5
    ) -> TwiMLResponse:
        """Generate TwiML for gathering input."""
        ...

    @abstractmethod
    def generate_connect_stream(
        self,
        websocket_url: str,
        parameters: dict[str, str] | None = None,
    ) -> TwiMLResponse:
        """Generate TwiML to connect a media stream (for real-time AI)."""
        ...

    @abstractmethod
    def generate_transfer(self, number: str, caller_id: str) -> TwiMLResponse:
        """Generate TwiML for call transfer."""
        ...

    @abstractmethod
    def validate_webhook(self, url: str, params: dict, signature: str) -> bool:
        """Validate an incoming webhook is authentic."""
        ...
