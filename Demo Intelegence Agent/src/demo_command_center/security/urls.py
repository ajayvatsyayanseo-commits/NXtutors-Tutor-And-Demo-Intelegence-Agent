"""Outbound URL allowlist — the SSRF boundary.

Every URL this process fetches, and every URL it puts in front of a parent, goes
through `validate()`. Two distinct attacks are being stopped:

* **SSRF.** A tutor profile URL or a `redirect_url` echoed back by a provider
  could point at `169.254.169.254` and read the Lambda's IAM role credentials.
  Scheme, host and literal-IP checks are all required — the metadata endpoint is
  an IP, so host-name allowlisting alone would not catch it.
* **Link injection into WhatsApp.** A Meet link we show a parent must have come
  from Google. `validate()` on the outbound path is what makes "the Meet URL is
  authentic" a checked property rather than a hope.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse

#: Hosts this service is ever allowed to call or to surface.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "graph.facebook.com",
        "www.googleapis.com",
        "oauth2.googleapis.com",
        "meet.google.com",
        "api.openai.com",
        "api.cashfree.com",
        "sandbox.cashfree.com",
        "payments.cashfree.com",
        "payments-test.cashfree.com",
    }
)


class UrlRejected(ValueError):
    def __init__(self, url: str, reason: str) -> None:
        # The URL is echoed back deliberately truncated: a full attacker-supplied
        # URL in an exception message ends up in logs and, worse, in alerts.
        super().__init__(f"rejected url ({reason}): {url[:80]}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    allowed_schemes: frozenset[str] = field(default_factory=lambda: frozenset({"https"}))
    #: Subdomains of an allowed host are accepted only when this is on. Off by
    #: default: `graph.facebook.com.evil.test` is not a Meta host, and a naive
    #: `endswith` check is exactly how that gets through.
    allow_subdomains: bool = False


def validate(url: str, policy: UrlPolicy | None = None) -> str:
    """Return the URL if it is allowed, else raise `UrlRejected`."""
    rules = policy or UrlPolicy()
    if not url or len(url) > 2048:
        raise UrlRejected(url or "", "empty_or_oversized")

    parsed = urlparse(url)
    if parsed.scheme not in rules.allowed_schemes:
        raise UrlRejected(url, f"scheme:{parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlRejected(url, "no_host")
    if parsed.username or parsed.password:
        # `https://graph.facebook.com@evil.test/` reads as Meta to a human.
        raise UrlRejected(url, "userinfo_in_url")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A literal IP is never legitimate here, and private/link-local literals
        # are the metadata-service attack outright.
        raise UrlRejected(url, "literal_ip" if address.is_global else "private_ip")

    if host in rules.allowed_hosts:
        return url
    if rules.allow_subdomains and any(host.endswith(f".{a}") for a in rules.allowed_hosts):
        return url
    raise UrlRejected(url, f"host_not_allowed:{host}")


def is_allowed(url: str, policy: UrlPolicy | None = None) -> bool:
    try:
        validate(url, policy)
    except UrlRejected:
        return False
    return True


#: The Meet link handed to a parent must be a real Google conference URL.
MEET_POLICY = UrlPolicy(allowed_hosts=frozenset({"meet.google.com"}))
#: Cashfree returns a hosted payment page; we forward that link and nothing else.
CASHFREE_LINK_POLICY = UrlPolicy(
    allowed_hosts=frozenset({"payments.cashfree.com", "payments-test.cashfree.com"})
)
