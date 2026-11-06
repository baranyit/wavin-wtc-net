"""Exception hierarchy for the Wavin WTC-NET protocol core.

Pure Python: no Home Assistant imports.
"""

from __future__ import annotations


class WtcError(Exception):
    """Base class for all integration-level errors."""


class WtcRequestRejectedError(WtcError):
    """A request violated a local safety rule and was never sent on the wire."""


class WtcModbusExceptionError(WtcError):
    """The device answered with a Modbus exception response.

    The connection itself is still considered healthy.
    """

    def __init__(self, exception_code: int | None, message: str | None = None) -> None:
        self.exception_code = exception_code
        super().__init__(message or f"Modbus exception response (code {exception_code})")


class WtcMalformedResponseError(WtcError):
    """The device answered, but the payload is unusable (short, out of range, wrong type)."""


class WtcTransportError(WtcError):
    """Connection-level failure. Data from the endpoint must be treated as unavailable."""


class WtcConnectionError(WtcTransportError):
    """Connecting failed or the connection was lost."""


class WtcTimeoutError(WtcTransportError):
    """A request did not complete within the configured timeout."""


class WtcBackoffError(WtcTransportError):
    """The endpoint is in reconnect backoff. No traffic was sent."""

    def __init__(self, retry_in: float) -> None:
        self.retry_in = retry_in
        super().__init__(f"Endpoint in reconnect backoff, retry in {retry_in:.1f} s")
