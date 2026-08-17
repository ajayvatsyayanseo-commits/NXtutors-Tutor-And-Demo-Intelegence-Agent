"""SSRF prevention and outbound URL allowlisting.

Every outbound HTTP destination in this service is configured, not derived from
input — but "configured" includes values from SSM and from a RAG document's
metadata, so the check is applied uniformly rather than trusted by origin.

The private-range block matters specifically because this runs in a VPC: an
unchecked fetch of `http://169.254.169.254/` is an instance-credential leak.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

#: The only schemes ever permitted outbound.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"https"})
#: `http` is tolerated for these hosts only, so local development works.
LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})

#: The link-local range that serves cloud instance metadata.
_METADATA_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fd00:ec2::/32"),
)


class UrlRejection(StrEnum):
    MALFORMED = "malformed"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    HOST_MISSING = "host_missing"
    HOST_NOT_ALLOWLISTED = "host_not_allowlisted"
    PRIVATE_ADDRESS = "private_address"
    METADATA_ENDPOINT = "metadata_endpoint"
    UNRESOLVABLE = "unresolvable"
    CREDENTIALS_IN_URL = "credentials_in_url"


class UnsafeUrl(Exception):
    def __init__(self, reason: UrlRejection, url: str) -> None:
        # The URL is included truncated — enough to debug, not enough to leak a
        # long token that someone embedded in a query string.
        super().__init__(f"{reason.value}: {url[:120]}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """Which hosts this process may talk to."""

    allowed_hosts: frozenset[str]
    allow_local: bool = False
    #: Resolving DNS closes the "allowlisted host with a private A record" hole,
    #: but costs a lookup. On by default; disabled in unit tests.
    resolve_dns: bool = True

    def with_host(self, url: str) -> UrlPolicy:
        """Extend the allowlist with a configured base URL's host."""
        host = urlparse(url).hostname
        if not host:
            return self
        return UrlPolicy(
            allowed_hosts=self.allowed_hosts | {host.lower()},
            allow_local=self.allow_local,
            resolve_dns=self.resolve_dns,
        )


def validate(url: str, policy: UrlPolicy) -> str:
    """Return the URL if it is safe to fetch, else raise `UnsafeUrl`."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UnsafeUrl(UrlRejection.MALFORMED, url) from exc

    if parsed.username or parsed.password:
        raise UnsafeUrl(UrlRejection.CREDENTIALS_IN_URL, url)

    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeUrl(UrlRejection.HOST_MISSING, url)

    is_local = host in LOCAL_HOSTS
    if parsed.scheme not in ALLOWED_SCHEMES and not (is_local and policy.allow_local):
        raise UnsafeUrl(UrlRejection.SCHEME_NOT_ALLOWED, url)

    if is_local:
        if not policy.allow_local:
            raise UnsafeUrl(UrlRejection.PRIVATE_ADDRESS, url)
        return url

    if host not in policy.allowed_hosts:
        raise UnsafeUrl(UrlRejection.HOST_NOT_ALLOWLISTED, url)

    for address in _addresses_for(host, policy):
        if any(address in network for network in _METADATA_NETWORKS):
            raise UnsafeUrl(UrlRejection.METADATA_ENDPOINT, url)
        if address.is_private or address.is_loopback or address.is_reserved:
            raise UnsafeUrl(UrlRejection.PRIVATE_ADDRESS, url)

    return url


def _addresses_for(
    host: str, policy: UrlPolicy
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolved addresses for a host, or the literal if it is already an IP."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    if not policy.resolve_dns:
        return []
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrl(UrlRejection.UNRESOLVABLE, host) from exc
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        candidate = info[4][0]
        try:
            out.append(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return out


def build_policy(*base_urls: str, allow_local: bool) -> UrlPolicy:
    """Allowlist derived from the configured integration base URLs.

    Nothing is allowlisted by default: a host has to be configured somewhere to
    become reachable.
    """
    policy = UrlPolicy(allowed_hosts=frozenset(), allow_local=allow_local)
    for url in base_urls:
        if url:
            policy = policy.with_host(url)
    return policy
