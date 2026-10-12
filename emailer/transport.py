"""Typed outbound transport contracts and P9 self-hosted safety flag.

The flag is intentionally not a transport selector.  P9 records readiness
only; the canonical ``send_core`` continues to use the existing provider
transport until a separately approved later phase wires a self-hosted one.
"""

from __future__ import annotations

import os
from typing import Optional, Protocol


class SyncEmailTransport(Protocol):
    """The keyword contract accepted by ``send_core.deliver``."""

    def __call__(
        self,
        *,
        to_email: str,
        subject: str,
        html_body: str,
        to_name: Optional[str] = None,
        smtp_profile: str = "default",
    ) -> tuple[bool, str, Optional[str]]: ...


class AsyncEmailTransport(Protocol):
    """The keyword contract accepted by ``send_core.send_one_async``."""

    async def __call__(
        self,
        *,
        to_email: str,
        subject: str,
        html_body: str,
        to_name: Optional[str] = None,
    ) -> tuple[bool, str, Optional[str]]: ...


def self_hosted_transport_enabled() -> bool:
    """Return true only for the explicit P9 feature value ``true``.

    Missing, malformed and truthy-looking values such as ``yes`` fail closed.
    This function does not select or construct a self-hosted transport.
    """
    return os.getenv("FTA_SELF_HOSTED_SMTP_ENABLED", "false").strip().lower() == "true"
