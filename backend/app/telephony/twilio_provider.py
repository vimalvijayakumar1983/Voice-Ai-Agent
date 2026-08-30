"""Twilio telephony provider implementation."""

import structlog
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Dial, Gather, VoiceResponse

from app.core.config import settings
from app.telephony.base import (
    CallRequest,
    CallResult,
    TelephonyProvider,
    TwiMLResponse,
)

logger = structlog.get_logger()


class TwilioProvider(TelephonyProvider):
    def __init__(
        self,
        *,
        account_sid: str | None = None,
        auth_token: str | None = None,
    ):
        self._account_sid = account_sid or settings.twilio_account_sid
        self._auth_token = auth_token or settings.twilio_auth_token
        self._client: Client | None = None
        self._validator: RequestValidator | None = None

    @property
    def client(self) -> Client:
        if not self._client:
            self._client = Client(self._account_sid, self._auth_token)
        return self._client

    @property
    def validator(self) -> RequestValidator:
        if not self._validator:
            self._validator = RequestValidator(self._auth_token)
        return self._validator

    async def make_call(self, request: CallRequest) -> CallResult:
        call = self.client.calls.create(
            to=request.to_number,
            from_=request.from_number,
            url=request.webhook_url,
            status_callback=request.status_callback_url,
            timeout=request.timeout,
        )
        logger.info("twilio_call_initiated", call_sid=call.sid, to=request.to_number)
        return CallResult(provider_call_sid=call.sid, status=call.status)

    async def end_call(self, call_sid: str) -> None:
        self.client.calls(call_sid).update(status="completed")
        logger.info("twilio_call_ended", call_sid=call_sid)

    def generate_greeting(self, message: str, voice: str) -> TwiMLResponse:
        response = VoiceResponse()
        response.say(message, voice=voice)
        return TwiMLResponse(xml=str(response))

    def generate_gather(
        self, prompt: str, voice: str, action_url: str, num_digits: int = 1, timeout: int = 5
    ) -> TwiMLResponse:
        response = VoiceResponse()
        gather = Gather(
            num_digits=num_digits,
            action=action_url,
            timeout=timeout,
        )
        gather.say(prompt, voice=voice)
        response.append(gather)
        response.say("We didn't receive any input. Goodbye!", voice=voice)
        return TwiMLResponse(xml=str(response))

    def generate_connect_stream(self, websocket_url: str) -> TwiMLResponse:
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=websocket_url)
        response.append(connect)
        return TwiMLResponse(xml=str(response))

    def generate_transfer(self, number: str, caller_id: str) -> TwiMLResponse:
        response = VoiceResponse()
        dial = Dial(caller_id=caller_id)
        dial.number(number)
        response.append(dial)
        return TwiMLResponse(xml=str(response))

    def validate_webhook(self, url: str, params: dict, signature: str) -> bool:
        return self.validator.validate(url, params, signature)


def get_telephony_provider(
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
) -> TelephonyProvider:
    """Factory to get the configured telephony provider."""
    return TwilioProvider(account_sid=account_sid, auth_token=auth_token)
