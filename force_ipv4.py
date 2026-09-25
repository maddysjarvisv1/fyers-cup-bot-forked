"""Force all outbound HTTP in this process onto IPv4.

Fyers whitelists by source IP. This box has no local IPv6, but the provider
egress translates to an IPv6 NAT address (2a02:...) that Fyers rejects. Forcing
IPv4 makes Fyers see the stable public IPv4 (69.62.72.117) so the whitelist
matching is deterministic.

Import this module BEFORE fyers_apiv3 / requests are used in the process:
    import force_ipv4   # noqa  (must be first)
"""
import socket

_orig = socket.getaddrinfo


def _force_v4(host, port, family=0, *args, **kwargs):
    # Prefer IPv4 (AF_INET) whenever the caller didn't demand IPv6 explicitly.
    if family == socket.AF_UNSPEC:
        family = socket.AF_INET
    return _orig(host, port, family, *args, **kwargs)


socket.getaddrinfo = _force_v4