"""ASGI-level request-body limits applied before framework buffering."""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _RequestBodyTooLargeError(HTTPException):
    """Internal receive-boundary signal for an over-limit streaming body."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body is too large")


class RequestBodyLimitMiddleware:
    """Reject HTTP request bodies that exceed a fixed byte ceiling.

    ``Content-Length`` is checked without reading the body, while the wrapped
    ``receive`` callable counts actual chunks so missing or dishonest length
    headers cannot bypass the limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body is too large"},
        )
        await response(scope, receive, send)

    def _declared_body_is_too_large(self, scope: Scope) -> bool:
        for name, raw_value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(raw_value.strip())
            except ValueError:
                # The streaming counter remains authoritative. The HTTP server
                # is responsible for rejecting malformed framing headers.
                continue
            if declared_length > self.max_bytes:
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._declared_body_is_too_large(scope):
            await self._reject(scope, receive, send)
            return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_bytes:
                    raise _RequestBodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLargeError:
            if response_started:
                # A streaming handler must not replace an already-started
                # response. Abort the exchange rather than emit a second start.
                raise
            await self._reject(scope, receive, send)
