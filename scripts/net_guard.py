"""Global-only network egress policy shared by every HTTP path in the pipeline.

Three layers live here:

1. ``is_public_ip`` — the single IP verdict used everywhere: an address is
   dialable only when it is globally routable (``is_global``) after unwrapping
   IPv4-mapped / 6to4 / Teredo encodings.  This closes the verified gap where
   RFC 6598 shared space (100.64.0.0/10, e.g. cloud metadata at
   100.100.100.200) passed the historical six-flag check on every Python.
2. ``validate_public_url`` — URL-level SSRF guard (scheme, embedded userinfo,
   ASCII control characters, literal IPs, and every DNS answer for hostnames).
3. ``PinnedPublicHTTPAdapter`` — connect-pinned transport: resolve DNS once,
   validate every candidate answer, then pin the connection to the validated
   answers while keeping the original URL (so the ``Host`` header, TLS SNI and
   certificate hostname verification still refer to the real hostname).  This
   closes the validate-then-connect TOCTOU/DNS-rebinding window.

The adapter overrides ``socket.getaddrinfo`` for the duration of one request.
The pipeline is a single-threaded CLI process, so the temporary swap is safe
here; it is restored in a ``finally`` block.
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

__all__ = [
    "is_public_ip",
    "validate_public_url",
    "UnresolvableHostError",
    "PinnedPublicHTTPAdapter",
    "read_bounded_body",
    "reject_url_fragments",
]

# ASCII C0 controls + DEL must never appear in a fetched URL.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class UnresolvableHostError(ValueError):
    """A hostname produced no DNS answer.  Distinct from a policy block so
    callers can defer the failure to connect time (DNS outages are often
    transient) while a non-public target is rejected immediately."""


def _unwrap_tunneling(address: "ipaddress.IPv4Address | ipaddress.IPv6Address"):
    """Collapse IPv4-in-IPv6 encodings to the embedded IPv4 address.

    ``::ffff:a.b.c.d`` (mapped), ``2002:aabb:ccdd::`` (6to4) and Teredo
    ``2001:0:...`` all smuggle a real IPv4 endpoint; the embedded address is
    the one that would actually be dialed, so that is what must be checked.
    """
    if address.version != 6:
        return address
    mapped = address.ipv4_mapped
    if mapped is not None:
        return mapped
    sixtofour = address.sixtofour
    if sixtofour is not None:
        return sixtofour
    teredo = address.teredo
    if teredo is not None:
        return teredo[1]  # (server, client) — the client is the actual peer
    return address


def _classic_blocked(address) -> bool:
    return (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified or address.is_multicast
    )


def is_public_ip(address) -> bool:
    """True only for a globally routable address after unwrapping tunneling.

    Accepts an ``ipaddress`` object or its textual form; unparsable text is
    not an address and therefore fails closed.  Requires ``is_global`` in
    addition to the classic blocklist so special ranges whose classification
    differs across Python versions (notably 100.64.0.0/10 shared space) can
    never slip through.
    """
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address.strip("[]"))
        except ValueError:
            return False
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    plain = _unwrap_tunneling(address)
    if _classic_blocked(plain) or not plain.is_global:
        return False
    return True


def reject_url_fragments(url: str) -> None:
    """Reject URLs carrying ASCII control characters or embedded userinfo."""
    if _CONTROL_CHARACTERS.search(url):
        raise ValueError("Blocked URL: ASCII control characters are not allowed in fetched URLs.")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ValueError("Blocked URL: embedded userinfo (user:pass@host) is not allowed.")


def validate_public_url(url: str) -> None:
    """SSRF guard: raise ValueError unless the URL is http(s) and targets a public host.

    Blocks non-http(s) schemes, embedded userinfo, ASCII control characters,
    localhost, and any address (literal or DNS-resolved) that fails
    :func:`is_public_ip` — including every DNS answer for a hostname.  Every
    redirect hop must pass this check before being followed.
    """
    reject_url_fragments(url)
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme '{parsed.scheme or '(none)'}' — only http/https are allowed.")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("Blocked URL: no hostname present.")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if not is_public_ip(ip):
            raise ValueError(f"Blocked target '{host}': not a globally routable public address.")
        return

    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise ValueError("Blocked target 'localhost'.")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnresolvableHostError(f"Cannot resolve host '{host}': {exc}") from exc
    except UnicodeError as exc:
        raise ValueError(f"Blocked target '{host}': malformed hostname.") from exc
    resolved_any = False
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        resolved_any = True
        if not is_public_ip(resolved):
            raise ValueError(
                f"Blocked target '{host}' (resolves to {resolved}): "
                "not a globally routable public address."
            )
    if not resolved_any:
        raise UnresolvableHostError(f"Cannot resolve host '{host}': no usable DNS answers.")


def _validated_dns_answers(host: str, resolver=None) -> list:
    """Resolve ``host`` once and validate EVERY candidate answer.

    Returns the ordered unique list of validated answers, or ``[]`` when
    ``host`` is already a literal IP address (validated separately).  Raises
    ``requests.ConnectionError`` when any candidate is not a public address —
    a DNS name that mixes public and private answers is treated as hostile.
    """
    resolver = resolver or socket.getaddrinfo
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise requests.ConnectionError(
                f"Blocked target '{host}': not a globally routable public address."
            )
        return []  # literal IP: no DNS lookup happens, nothing to pin

    try:
        infos = resolver(host, None)
    except (socket.gaierror, UnicodeError) as exc:
        raise requests.ConnectionError(f"Cannot resolve host '{host}': {exc}") from exc
    candidates: list = []
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if not is_public_ip(address):
            raise requests.ConnectionError(
                f"Blocked target '{host}' (resolves to {address}): "
                "not a globally routable public address."
            )
        if address not in candidates:
            candidates.append(address)
    if not candidates:
        raise requests.ConnectionError(f"Cannot resolve host '{host}': no usable DNS answers.")
    return candidates


def _pinned_getaddrinfo_factory(host: str, candidates: list, real_getaddrinfo):
    """Build a ``socket.getaddrinfo`` replacement that pins ``host``.

    Lookups for the target host never touch DNS again: each validated address
    is re-fed through the real resolver as a literal IP (so the returned
    sockaddr tuples carry the caller's port and family), preserving answer
    order.  Any other host (proxies, unrelated lookups) resolves normally.
    """

    def pinned_getaddrinfo(host_arg, port_arg=None, family=0, type=0, proto=0, flags=0):
        if host_arg == host:
            answers = []
            for address in candidates:
                answers.extend(real_getaddrinfo(str(address), port_arg, family, type, proto, flags))
            return answers
        return real_getaddrinfo(host_arg, port_arg, family, type, proto, flags)

    return pinned_getaddrinfo


class PinnedPublicHTTPAdapter(HTTPAdapter):
    """Transport that closes the validate-then-connect DNS rebinding window.

    ``send`` resolves the request host once, validates every candidate with
    :func:`is_public_ip`, then serves the connection from the validated
    answers only.  The request URL is never rewritten, so the ``Host`` header,
    TLS SNI and certificate hostname verification keep referring to the real
    hostname.  Single-threaded CLI process assumed (the temporary
    ``socket.getaddrinfo`` swap is process-global); it is restored in a
    ``finally`` block.
    """

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        url = request.url
        try:
            reject_url_fragments(url)
        except ValueError as exc:
            # Uniform transport refusal: the adapter speaks in ConnectionError
            # so callers cannot mistake a policy block for a data error.
            raise requests.ConnectionError(str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise requests.ConnectionError(f"Blocked URL scheme '{parts.scheme}' on the pinned transport.")
        host = parts.hostname or ""
        if not host:
            raise requests.ConnectionError("Blocked URL: no hostname present.")
        try:
            port = parts.port
        except ValueError as exc:
            raise requests.ConnectionError(f"Blocked URL: invalid port ({exc}).") from exc
        del port  # only validated for parse errors; the resolver gets its own port

        candidates = _validated_dns_answers(host)
        real_getaddrinfo = socket.getaddrinfo
        if candidates:
            socket.getaddrinfo = _pinned_getaddrinfo_factory(host, candidates, real_getaddrinfo)
        try:
            return super().send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)
        finally:
            socket.getaddrinfo = real_getaddrinfo


def read_bounded_body(response, limit: int) -> str:
    """Stream at most ``limit`` bytes from ``response`` and decode as UTF-8.

    Used for robots.txt and other metadata bodies whose size the server
    controls.  Responses that cannot stream (test doubles and non-streaming
    response objects) fall back to their ``.text`` attribute truncated to the
    limit, so behavior is identical for real streamed responses and for
    in-process fakes.
    """
    buffer = bytearray()
    for chunk in response.iter_content(chunk_size=65536):
        remaining = limit - len(buffer)
        if remaining <= 0:
            break
        buffer.extend(chunk[:remaining])
    if not buffer:
        text = getattr(response, "text", "")
        if isinstance(text, str) and text:
            return text[:limit]
    return buffer.decode("utf-8", errors="replace")
