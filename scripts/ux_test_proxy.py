#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The UX test browser's only network path: a self-contained egress proxy.

The unattended MR UX test drives a headless Chrome that renders the merge
request's own UI code, signed in as a persona of the shared review stack. That
code is untrusted, and so is everything it loads, so the browser is given exactly
one way out, and this is it. Chrome's flags and its managed policy
(``scripts/ux_runner/chrome_policy.json``) name ``http://127.0.0.1:3128`` as the
fixed proxy for every request, loopback included (``<-loopback>``), and its host
resolver rules make every direct name lookup fail, so a request that does not
come here does not leave the browser at all. The proxy runs as its own
unprivileged user, ``uxproxy``, so the socket audit can attribute every remote
socket of the browser's uid to Chrome rather than to the proxy's upstream
connections.

What it passes
--------------
* **CONNECT to a listed ``host:443``.** The list is exact host names: the review
  stack's Cognito, SSM, API Gateway and Lambda Function URL hosts and its buckets
  by exact name (each on its regional host and, in the commercial partition, on
  its global one too), derived by :func:`stack_rules` from the stack's own UI
  environment, plus :data:`EGRESS_ALLOWED_STATIC_HOSTS`. Every address the name
  resolves to must be public, and the tunnel connects to the address that was
  checked rather than looking the name up again, so a name cannot pass the check
  on one answer and connect on another. A tunnel is opaque TLS: the proxy sees
  host names, never paths or bodies, which is why the list is per host and short.
* **GET and HEAD of ``http://localhost:3000/...``, in local-build mode.** With
  ``--build-dir`` the proxy serves the MR's static build itself, so the browser
  loads the MR's UI without any web server that runs MR code. Paths are resolved
  one component at a time and no symlink is followed (:class:`Site`).

Everything else is refused with a one-line body and recorded, one JSON line per
refusal, with a reason from a fixed vocabulary (:data:`REFUSAL_REASONS`): any
other host or port, a name that resolves to an address that is not public
(instance metadata, loopback, private, link-local) unless it is an AWS service name
answering a VPC endpoint's address, plain HTTP to anything but the
local build, a request that is not a proxy request, any method but GET and HEAD,
a path outside the build, a tunnel beyond the cap, and a request the proxy cannot
read: one ``http.server`` cannot parse, a head longer than :data:`HEAD_LIMIT`
bytes, or one still incomplete :data:`HEAD_TIMEOUT` seconds after the proxy began
waiting for it. A connection beyond the connection cap is recorded as well, but
closed unanswered, before anything is read from it. The harness reads that log
twice. At start-up it sends Chrome to ``http://ux-proxy-selftest.invalid/`` and
requires the refusal to appear (``ux_runner/chrome.py``), which proves the
browser's traffic really arrives here; at the end it copies the log into the job's
artifacts, so a host the UI needed and did not get shows up by name.

Why it is self-contained
------------------------
It is run as ``python3 -I -B`` from the pinned harness and imports nothing beyond
the standard library and nothing from ``scripts/``. ``-I`` keeps the script's own
directory, the user site and every ``PYTHON*`` variable out of ``sys.path``, and
``-B`` keeps it from writing bytecode anywhere. This is the one process the whole
confinement rests on, so it depends on nothing else being right.
Its harness side lives in ``scripts/ux_runner/egress.py``.

Bounds worth knowing
--------------------
* A host on the list is reachable by the MR's page code, all of it, with the
  persona's credentials. That is why :data:`EGRESS_ALLOWED_STATIC_HOSTS` is pinned
  by a test and why the stack's hosts are exact names rather than patterns:
  bucket names are global, so a pattern such as ``idp-ux-nightly-*`` would admit a
  bucket anybody can create.
* A path-style S3 URL (``s3.<region>.amazonaws.com/<bucket>/...``) is refused:
  its host names no bucket, so listing it would admit every bucket. The backend
  presigns that way only for uploads, which the unattended test never makes.
* Names are resolved by the system resolver. The public-address rule is what
  stands between a name on the list and an internal address; it does not make the
  resolver trustworthy. An IP address is never tunnelled to, in any spelling: a
  permitted name must end in a label that begins with a letter, and a rules file
  that lists anything else is refused.
* A client is held to a deadline, a size and a line count for each request head
  as a whole, not for each read, and at most twice ``--max-tunnels`` connections
  are open at once, so a slow or oversized request costs one connection for at
  most :data:`HEAD_TIMEOUT` seconds. A GET or HEAD that says a body follows is answered
  and its connection closed: the proxy never reads a body, and on a kept-alive
  connection the body would otherwise be read as the next request.
* The refusal log stops writing at its limit (5,000 lines by default) while the
  counts in the stats file carry on. It keeps no query string, which is where a
  presigned URL carries its signature and session token.

Usage (``ux_runner/egress.py`` builds exactly this)::

    python3 -I -B scripts/ux_test_proxy.py --listen 127.0.0.1:3128 --rules rules.json \\
        [--build-dir src/ui/build] --refusal-log refusals.jsonl --stats stats.json \\
        --ready-file ready
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import io
import ipaddress
import json
import os
import re
import selectors
import shutil
import signal
import socket
import socketserver
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from types import FrameType
from typing import Any, cast
from urllib.parse import SplitResult, unquote, urlsplit

#: Where the proxy listens unless told otherwise. ``chrome_policy.json`` names the
#: same address, and the policy is static JSON installed with the toolchain.
DEFAULT_LISTEN = "127.0.0.1:3128"
#: The origin the local build is served at, in absolute-form requests only.
LOCAL_UI_HOST = "localhost"
LOCAL_UI_PORT = 3000
#: The only port a tunnel may reach.
TLS_PORT = 443
#: How many bytes a request's line and headers may take together. A browser's
#: request heads are a few kilobytes; this is ``http.server``'s limit for one line.
HEAD_LIMIT = 64 * 1024
#: How many lines a request head may hold before its empty line, the request line
#: included. ``http.server`` refuses more than 99 headers in any case; this keeps a
#: head of thousands of short lines from costing a read for each.
HEAD_LINES = 128
#: How many seconds a client has to send a whole request head once the proxy
#: starts waiting for it. A connection that sends nothing in that time is an idle
#: keep-alive connection and is closed quietly; one that stalls part-way is refused.
HEAD_TIMEOUT = 30.0

#: Hosts every run may tunnel to, whatever the stack: ``@monaco-editor/react``
#: loads the editor from this CDN at run time. Pinned by
#: ``scripts/tests/test_ux_test_proxy.py``, because a host added here becomes
#: reachable by the MR's own page code.
EGRESS_ALLOWED_STATIC_HOSTS: tuple[str, ...] = ("cdn.jsdelivr.net:443",)

#: Content types by file suffix for the local build. Every other suffix is served
#: as ``application/octet-stream``, which ``nosniff`` keeps from being run.
CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".gif": "image/gif",
    ".htm": "text/html; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".mjs": "text/javascript; charset=utf-8",
    ".otf": "font/otf",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".xml": "application/xml",
}

#: Why a request was refused: the fixed vocabulary of the refusal log and of the
#: ``by_reason`` counts, so both can be read without parsing free text.
REFUSAL_REASONS: tuple[str, ...] = (
    "host_not_allowed",
    "port_not_allowed",
    "resolves_to_non_public_address",
    "plain_http_not_allowed",
    "not_a_proxy_request",
    "method_not_allowed",
    "path_not_served",
    "too_many_tunnels",
    "malformed_request",
    "request_timeout",
    "too_many_connections",
)

RULES_VERSION = 1
_CONNECT_TIMEOUT = 10.0
_SOCKET_TIMEOUT = 60.0
_CHUNK = 64 * 1024
_TARGET_LIMIT = 200
_METHOD_LIMIT = 32
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
#: The last label must begin with a letter, as every top-level domain does. That is
#: what keeps an IPv4 address in any spelling the resolver accepts (``52.95.110.1``,
#: ``127.1``, ``0x7f.0x1``) from passing for a name: each ends in a digit-led label.
_HOSTNAME_RE = re.compile(rf"(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_REGION_RE = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]")
#: A region of the commercial partition, whose S3 also answers on the global
#: ``<bucket>.s3.amazonaws.com`` host: botocore's ``regionRegex`` for ``aws``.
_COMMERCIAL_REGION_RE = re.compile(r"(?:us|eu|ap|sa|ca|me|af|il|mx)-[a-z]+-[0-9]+")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_STACK_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}")
_PORT_RE = re.compile(r"[0-9]{1,5}")
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
#: The ranges an interface VPC endpoint takes its addresses from: RFC 1918, and the
#: shared range a VPC may also be given. With private DNS, the service's public
#: name answers one of them inside that VPC, as ``ssm.<region>.amazonaws.com`` does
#: on the CI runners. Accepted only for an AWS service name (:func:`resolve_public`)
#: and pinned by ``scripts/tests/test_ux_test_proxy.py``, because a range added here
#: becomes reachable by the MR's own page code wherever such a name resolves into it.
VPC_ENDPOINT_ALLOWED_NETWORKS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
)
#: The suffix of the names that may answer a VPC endpoint's address.
AWS_SERVICE_SUFFIX = ".amazonaws.com"
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


class EgressConfigError(ValueError):
    """A rules file, a UI environment or an option is malformed.

    The message names the key or the option, never its value: the values are the
    review stack's identifiers, and the message ends up in a job log.
    """


def _is_hostname(value: str) -> bool:
    """A lower-case DNS name of at least two labels, with no port or scheme.

    Its last label begins with a letter, so no IP address passes for one.
    """
    return len(value) <= 253 and _HOSTNAME_RE.fullmatch(value) is not None


def _normal_host(host: str) -> str:
    """``host`` lower-cased with one trailing dot removed, as rules compare it."""
    name = host.lower()
    return name[:-1] if name.endswith(".") else name


@dataclass(frozen=True)
class EgressRules:
    """The CONNECT targets the browser may reach, as exact ``host:443`` strings."""

    hosts: frozenset[str]

    def permits(self, host: str, port: int) -> bool:
        """Whether a tunnel to ``host:port`` is on the list.

        The host is compared lower-cased with one trailing dot removed, so the
        spellings a client may legitimately send for one name are one name, and
        the port must be 443. The host must also be a host name: an IP address is
        never permitted, even by a list that names one.
        """
        name = _normal_host(host)
        return (
            port == TLS_PORT and _is_hostname(name) and f"{name}:{port}" in self.hosts
        )

    def to_json(self) -> dict[str, Any]:
        """The ``--rules`` file content: the version and the sorted hosts."""
        return {"version": RULES_VERSION, "hosts": sorted(self.hosts)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EgressRules:
        """Read a ``--rules`` file, refusing anything but version 1 of ``host:443``.

        This is the proxy's own reading of what the harness wrote, so it is strict
        rather than forgiving: an entry that is not a lower-case host name on port
        443 means the file is not one the harness produced.
        """
        if not isinstance(data, Mapping) or data.get("version") != RULES_VERSION:
            raise EgressConfigError("the egress rules are not version 1 rules")
        hosts = data.get("hosts")
        if not isinstance(hosts, list):
            raise EgressConfigError("the egress rules have no list of hosts")
        for entry in hosts:
            if not isinstance(entry, str):
                raise EgressConfigError("an egress rule is not a host name on port 443")
            name, sep, port = entry.rpartition(":")
            if not sep or port != str(TLS_PORT) or not _is_hostname(name):
                raise EgressConfigError("an egress rule is not a host name on port 443")
        return cls(frozenset(hosts))


def parse_ui_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, as the stack's ``WebUITestEnvFile`` output holds.

    Blank lines and ``#`` lines are skipped, each line is split on its first
    ``=``, and values are kept verbatim, quotes included, because nothing here is
    a shell. A key that is not an upper-case environment name is refused with its
    line number only.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or _ENV_KEY_RE.fullmatch(key) is None:
            raise EgressConfigError(f"UX_TEST_UI_ENV line {number} is not KEY=VALUE")
        values[key] = value
    return values


def _https_host(value: str, key: str, shape: re.Pattern[str] | None) -> str:
    """The host of an ``https`` URL on port 443, checked against ``shape``.

    With no ``shape`` any lower-case DNS name passes. A URL carrying user
    information, another scheme or another port is refused, naming ``key``.
    """
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise EgressConfigError(f"{key} is not a URL") from None
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, TLS_PORT):
        raise EgressConfigError(f"{key} must be an https URL on port 443")
    host = parts.hostname or ""
    matches = shape.fullmatch(host) is not None if shape else _is_hostname(host)
    if not matches:
        raise EgressConfigError(f"{key} does not name a host of the expected shape")
    return host


def _bucket_hosts(name: str, key: str, stack: str, region: str) -> tuple[str, ...]:
    """The virtual-hosted S3 endpoints of one exact bucket of ``stack``.

    The browser signs no S3 URL of its own, so both hosts are ones the backend
    presigns. ``getFilePresignedUrl`` and ``getFilePresignedUrls``, whose URLs the
    document viewer and the test studio fetch, sign with a client in the stack's
    region and ``addressing_style`` ``virtual``: outside ``us-east-1`` that is the
    regional host, and in ``us-east-1`` botocore presigns the global
    ``<bucket>.s3.amazonaws.com`` host instead, as it does in every commercial
    region for a presigner with ``addressing_style`` ``auto``. So in the commercial
    partition the global host is listed as well. Within one partition a bucket
    name is unique, so both hosts reach the same bucket. Outside it the global
    host is not listed, because it is the commercial partition's, where the same
    name can belong to anybody.

    The name must be a bucket name without dots that starts with the stack's own
    name and a dash. What keeps a stranger's bucket off the list is exactness: the
    names come from the stack's own outputs and its account's bucket list, never
    from a pattern. The prefix rule only catches a name pasted in from elsewhere.
    """
    if _BUCKET_RE.fullmatch(name) is None or not name.startswith(stack.lower() + "-"):
        raise EgressConfigError(
            f"{key} holds a name that is not an exact bucket name of the stack"
        )
    regional = f"{name}.s3.{region}.amazonaws.com:{TLS_PORT}"
    if _COMMERCIAL_REGION_RE.fullmatch(region) is None:
        return (regional,)
    return (regional, f"{name}.s3.amazonaws.com:{TLS_PORT}")


def stack_rules(
    ui_env: Mapping[str, str], stack: str, *, web_url: str | None = None
) -> EgressRules:
    """The CONNECT targets of one review stack, derived from its UI environment.

    The region is required. The Cognito user-pool and identity endpoints always
    follow from it, SSM when the UI reads a settings parameter, then the hosts of
    the API and streaming URLs, each bucket by its exact name (the two the UI
    names, plus ``UX_TEST_BUCKETS``) on the hosts :func:`_bucket_hosts` gives it, a
    Cognito domain when the stack has one, the web host in deployed-stack mode,
    and :data:`EGRESS_ALLOWED_STATIC_HOSTS`. Any other key, or one that is absent
    or empty, adds nothing. Every value that is present is shape-checked, and a
    malformed one raises naming its key.
    """
    if _STACK_RE.fullmatch(stack) is None:
        raise EgressConfigError("the stack name is not a CloudFormation stack name")
    region = ui_env.get("VITE_AWS_REGION", "")
    if _REGION_RE.fullmatch(region) is None:
        raise EgressConfigError("VITE_AWS_REGION is required and must be a region")
    escaped = re.escape(region)
    hosts = {
        f"cognito-idp.{region}.amazonaws.com:{TLS_PORT}",
        f"cognito-identity.{region}.amazonaws.com:{TLS_PORT}",
    }
    if ui_env.get("VITE_SETTINGS_PARAMETER"):
        hosts.add(f"ssm.{region}.amazonaws.com:{TLS_PORT}")
    api_shape = re.compile(rf"[a-z0-9]{{10}}\.execute-api\.{escaped}\.amazonaws\.com")
    stream_shape = re.compile(rf"[a-z0-9]{{20,64}}\.lambda-url\.{escaped}\.on\.aws")
    for key, shape in (
        ("VITE_API_BASE_URL", api_shape),
        ("VITE_STREAM_URL", stream_shape),
    ):
        if ui_env.get(key):
            hosts.add(f"{_https_host(ui_env[key], key, shape)}:{TLS_PORT}")
    for key in ("VITE_INPUT_BUCKET_NAME", "VITE_TEST_SET_BUCKET_NAME"):
        if ui_env.get(key):
            hosts.update(_bucket_hosts(ui_env[key], key, stack, region))
    for name in ui_env.get("UX_TEST_BUCKETS", "").split(","):
        if name.strip():
            hosts.update(_bucket_hosts(name.strip(), "UX_TEST_BUCKETS", stack, region))
    domain = ui_env.get("VITE_COGNITO_DOMAIN", "")
    if domain:
        if not _is_hostname(domain) or not domain.endswith(".amazoncognito.com"):
            raise EgressConfigError(
                "VITE_COGNITO_DOMAIN is not an Amazon Cognito domain"
            )
        hosts.add(f"{domain}:{TLS_PORT}")
    if web_url is not None:
        hosts.add(f"{_https_host(web_url, 'the web URL', None)}:{TLS_PORT}")
    hosts.update(EGRESS_ALLOWED_STATIC_HOSTS)
    return EgressRules(frozenset(hosts))


def _global(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Globally routable unicast: Python's ``is_global`` without multicast."""
    return address.is_global and not address.is_multicast


def is_public_address(address: str) -> bool:
    """Whether a tunnel may connect to ``address``: global unicast only.

    An IPv4-mapped IPv6 address is judged as the IPv4 address it carries, and so
    is the IPv4 address inside a NAT64 or an IPv4-compatible one, which Python
    otherwise reports as global even when it embeds ``127.0.0.1``. Loopback,
    private, link-local (``169.254.169.254`` included), unique-local
    (``fd00:ec2::254`` included), unspecified and multicast addresses are all
    refused, and so is anything that does not parse.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return _global(parsed.ipv4_mapped)
        if parsed in _NAT64_PREFIX or parsed in _IPV4_COMPATIBLE:
            embedded = ipaddress.IPv4Address(int(parsed) & 0xFFFFFFFF)
            return _global(embedded) and _global(parsed)
    return _global(parsed)


def is_vpc_endpoint_address(address: str) -> bool:
    """Whether ``address`` lies in :data:`VPC_ENDPOINT_ALLOWED_NETWORKS`.

    An IPv4-mapped IPv6 address is judged as the IPv4 address it carries. No other
    IPv6 address is one, so ``fd00:ec2::254`` and every other unique-local or
    link-local address stays refused, as do loopback, link-local IPv4 and anything
    that does not parse.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is None:
            return False
        parsed = parsed.ipv4_mapped
    return any(parsed in network for network in VPC_ENDPOINT_ALLOWED_NETWORKS)


def resolve_public(
    host: str,
    port: int,
    resolver: Callable[..., list[Any]] = socket.getaddrinfo,
    *,
    vpc_endpoint: bool = False,
) -> str | None:
    """The address a tunnel to ``host`` connects to, or ``None`` to refuse it.

    Every address the resolver answers must be public, not merely the one that
    would be used, so a name that answers one public and one internal address is
    refused rather than left to chance. With ``vpc_endpoint``, which the proxy sets
    only for an allowlisted AWS service name, every answer may instead be a VPC
    endpoint's address (:func:`is_vpc_endpoint_address`), which is how an interface
    endpoint with private DNS answers; a mix of the two is still refused, and the
    browser still checks that service's TLS certificate through the tunnel. The
    first IPv4 address wins, then the first IPv6 one. A resolver error propagates;
    the caller reports it as an upstream failure rather than as a refusal.
    """
    answers = resolver(host, port, type=socket.SOCK_STREAM)
    addresses: list[tuple[int, str]] = []
    for answer in answers:
        family, address = answer[0], str(answer[4][0])
        if family not in (socket.AF_INET, socket.AF_INET6):
            return None
        addresses.append((family, address))
    public = all(is_public_address(address) for _, address in addresses)
    endpoint = vpc_endpoint and all(
        is_vpc_endpoint_address(address) for _, address in addresses
    )
    if not (public or endpoint):
        return None
    for wanted in (socket.AF_INET, socket.AF_INET6):
        for family, address in addresses:
            if family == wanted:
                return address
    return None


@dataclass
class Site:
    """The MR's static build, served at ``http://localhost:3000``.

    The root's real path is taken once, when the site is made, and every request
    is resolved against that and checked to still lie under it. Resolved again per
    request, a component of the root's own path that another user replaced with a
    symlink after the proxy started would move the whole site to wherever the link
    pointed, and every check below would then hold relative to the new place.
    """

    root: Path
    real_root: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.real_root = os.path.realpath(self.root)

    def resolve(self, url_path: str) -> Path | None:
        """The regular file ``url_path`` names under :attr:`root`, or ``None``.

        The path is unquoted after its query and fragment are dropped, so an
        encoded ``?`` stays part of a name. NUL and backslash are refused, and so
        is every ``.``, ``..`` or empty segment. Each component is then examined
        with ``lstat`` from the root down: a symlink anywhere is refused, every
        intermediate component must be a directory and the last a regular file,
        and the result must still sit under the root's real path as it was when
        the site was made (:attr:`real_root`).
        """
        raw = url_path.split("?", 1)[0].split("#", 1)[0]
        path = unquote(raw)
        if "\x00" in path or "\\" in path:
            return None
        if path in ("", "/"):
            segments = ["index.html"]
        elif path.startswith("/"):
            segments = path[1:].split("/")
        else:
            return None
        if any(segment in ("", ".", "..") for segment in segments):
            return None
        root = self.real_root
        current = root
        for index, segment in enumerate(segments):
            current = os.path.join(current, segment)
            try:
                mode = os.lstat(current).st_mode
            except (OSError, ValueError):
                return None
            last = index == len(segments) - 1
            if stat.S_ISLNK(mode):
                return None
            if last and not stat.S_ISREG(mode):
                return None
            if not last and not stat.S_ISDIR(mode):
                return None
        if not os.path.realpath(current).startswith(root + os.sep):
            return None
        return Path(current)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RefusalLog:
    """The refusals, one JSON line each, and a count per reason.

    Writing stops at ``limit`` lines while the counts carry on, so a page that
    floods the proxy with refused requests costs a bounded amount of disk and the
    totals stay exact. The target is text the page chose, so it is kept without
    its query or fragment and cut to 200 characters: a presigned URL carries its
    signature and session token in the query, and this log is copied into the
    job's artifacts.
    """

    def __init__(self, path: Path | None, limit: int = 5000) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._written = 0

    def record(self, method: str, target: str, reason: str) -> None:
        """Count one refusal and, under the limit, append its line."""
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"{reason!r} is not a refusal reason")
        kept = target.split("?", 1)[0].split("#", 1)[0]
        line = json.dumps(
            {
                "t": _utc_now(),
                "method": method[:_METHOD_LIMIT],
                "target": kept[:_TARGET_LIMIT],
                "reason": reason,
            }
        )
        with self._lock:
            self._counts[reason] = self._counts.get(reason, 0) + 1
            if self.path is None or self._written >= self.limit:
                return
            self._written += 1
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                self._written = self.limit

    @property
    def counts(self) -> dict[str, int]:
        """Refusals so far, by reason."""
        with self._lock:
            return dict(self._counts)


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout)


def _relay(client: socket.socket, upstream: socket.socket, idle: float) -> None:
    """Copy bytes both ways until either side closes or ``idle`` seconds pass."""
    client.settimeout(_SOCKET_TIMEOUT)
    upstream.settimeout(_SOCKET_TIMEOUT)
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, (client, upstream))
        selector.register(upstream, selectors.EVENT_READ, (upstream, client))
        while True:
            ready = selector.select(timeout=idle)
            if not ready:
                return
            for key, _events in ready:
                source, sink = key.data
                try:
                    chunk = source.recv(_CHUNK)
                    if not chunk:
                        return
                    sink.sendall(chunk)
                except OSError:
                    return


def _authority(target: str) -> tuple[str, int] | None:
    """``host`` and ``port`` of a CONNECT target, or ``None`` for any other shape.

    An IPv6 literal, a missing port and a port that is not a number are all
    ``None``: none of them can be on the list.
    """
    host, sep, port_text = target.rpartition(":")
    if not sep or not host or "[" in host or ":" in host:
        return None
    if _PORT_RE.fullmatch(port_text) is None or not 0 < int(port_text) < 65536:
        return None
    return host, int(port_text)


def _split(target: str) -> SplitResult | None:
    """``target`` as ``urlsplit`` reads it, or ``None`` when it cannot be read.

    ``urlsplit`` raises on a malformed bracketed host (``http://[x]/``,
    ``http://[abc/``). That is a request to refuse and record, not a failure of
    the handler.
    """
    try:
        return urlsplit(target)
    except ValueError:
        return None


def _is_local_ui(parts: SplitResult | None) -> bool:
    if parts is None:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname == LOCAL_UI_HOST
        and port == LOCAL_UI_PORT
        and "@" not in parts.netloc
    )


class _UnreadableHead(Exception):
    """A request head the proxy stopped reading: what it got, and the answer."""

    def __init__(self, status: int, reason: str, head: bytes) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.head = head


def _peek(sock: socket.socket, size: int, timeout: float) -> bytes | None:
    """Up to ``size`` bytes waiting on ``sock``, left there; ``None`` on timeout.

    A connection that has ended, by a close or a reset, reads as ``b""``.
    """
    if timeout <= 0:
        return None
    sock.settimeout(timeout)
    try:
        return sock.recv(size, socket.MSG_PEEK)
    except TimeoutError:
        return None
    except OSError:
        return b""


def _take(sock: socket.socket, size: int) -> bytes:
    """Consume ``size`` bytes that a peek has shown are already waiting."""
    taken = b""
    while len(taken) < size:
        chunk = sock.recv(size - len(taken))
        if not chunk:
            break
        taken += chunk
    return taken


def _read_head(sock: socket.socket, timeout: float, limit: int = HEAD_LIMIT) -> bytes:
    """One request's line and headers, through the empty line that ends them.

    Nothing after the head is consumed. Each pass peeks at what has arrived and
    takes only as far as the next line feed, so the first byte a CONNECT client
    sends after its headers is still on the socket for the tunnel, and a keep-alive
    client's next request is still there for the next call. That also costs a few
    system calls per line, where reading through an unbuffered file costs one per
    byte.

    ``timeout`` bounds the whole head, not each read, so a client trickling a byte
    at a time cannot hold its connection open. A client that sends nothing before
    the deadline, or closes before its first byte, gets ``b""``: that is how an
    idle keep-alive connection ends. One that sends part of a head and stalls
    raises :class:`_UnreadableHead` with 408, one whose head is longer than
    ``limit`` with 414 while it is still on its request line and 431 after, one
    with more than :data:`HEAD_LINES` lines with 431, and one that closes
    part-way with 400. An empty first line is a head on its own, for the handler
    to refuse.
    """
    deadline = time.monotonic() + timeout
    head = bytearray()
    line_start = 0
    lines = 0
    while True:
        size = min(_CHUNK, limit + 1 - len(head))
        peeked = _peek(sock, size, deadline - time.monotonic())
        if not peeked:
            if not head:
                return b""
            if peeked is None:
                raise _UnreadableHead(408, "request_timeout", bytes(head))
            raise _UnreadableHead(400, "malformed_request", bytes(head))
        newline = peeked.find(b"\n")
        head += _take(sock, len(peeked) if newline < 0 else newline + 1)
        if len(head) > limit:
            status = 431 if line_start else 414
            raise _UnreadableHead(status, "malformed_request", bytes(head))
        if newline >= 0:
            if head[line_start:] in (b"\n", b"\r\n"):
                return bytes(head)
            line_start = len(head)
            lines += 1
            if lines > HEAD_LINES:
                raise _UnreadableHead(431, "malformed_request", bytes(head))


def _first_line(head: bytes) -> str:
    """The request line of a raw head, decoded as ``http.server`` decodes it."""
    return head.split(b"\n", 1)[0].decode("iso-8859-1").rstrip("\r")


def _request_words(requestline: str) -> tuple[str, str]:
    """The method and target a request line names, as far as it names them."""
    words = requestline.split()
    return (words[0] if words else ""), (words[1] if len(words) > 1 else "")


def _carries_body(headers: Message) -> bool:
    """Whether a request says a body follows its head.

    Any ``Transfer-Encoding`` says so, and so does any ``Content-Length`` but 0.
    """
    if headers.get("Transfer-Encoding") is not None:
        return True
    lengths = headers.get_all("Content-Length", [])
    return any(str(value).strip() != "0" for value in lengths)


class _Handler(http.server.BaseHTTPRequestHandler):
    """One client connection: CONNECT tunnels, local-build GET and HEAD, refusals.

    The request is never read through a socket file. :meth:`handle_one_request`
    reads each head with :func:`_read_head`, which consumes nothing after it,
    under the proxy's deadline and byte cap, and ``http.server`` parses that head
    from memory. So a CONNECT's tunnel starts at the first byte after its headers,
    and every request ``http.server`` refuses on its own account (a request line
    or a version it cannot read, too many headers) reaches :meth:`send_error`,
    where it is answered and recorded like any other refusal. A request line too
    malformed to name a version is answered as HTTP/1.0 would be, with a status
    line, rather than as HTTP/0.9, with a bare body. Every method without a
    handler of its own is refused through :meth:`__getattr__`, so an unusual
    method is counted and logged too, instead of getting ``http.server``'s
    unlogged 501.
    """

    protocol_version = "HTTP/1.1"
    default_request_version = "HTTP/1.0"
    server_version = "ux-test-proxy"
    sys_version = ""
    timeout = 120.0

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def setup(self) -> None:
        """The connection, with an empty ``rfile`` in place of the socket's."""
        super().setup()
        self.rfile.close()
        self.rfile = io.BytesIO()

    def handle_one_request(self) -> None:
        """Read one request head, have ``http.server`` parse it, and dispatch it.

        The connection closes after this request unless the request keeps it
        open. A head that cannot be read is refused with the status
        :func:`_read_head` gives it and recorded under its request line as far as
        it arrived; an answer to a client that has already gone costs nothing
        but the answer. A request line with no words in it is refused here,
        because ``parse_request`` would close on it without a word, and so
        without a record. Reading never raises a timeout, because the head's
        deadline is :func:`_read_head`'s to keep; a write that times out ends the
        connection through :meth:`EgressProxy.handle_error`, like any failure.
        """
        self.close_connection = True
        self.command, self.path, self.requestline = "", "", ""
        self.request_version = self.default_request_version
        try:
            head = _read_head(self.connection, self.proxy.request_timeout)
        except _UnreadableHead as unreadable:
            self.connection.settimeout(self.timeout)
            self.requestline = _first_line(unreadable.head)
            with contextlib.suppress(OSError):
                self._refuse(unreadable.reason, status=unreadable.status)
            return
        if not head:
            return
        self.connection.settimeout(self.timeout)
        if not _first_line(head).split():
            self._refuse("malformed_request", status=400)
            return
        self.rfile = io.BytesIO(head)
        self.raw_requestline = self.rfile.readline()
        if self.parse_request():
            getattr(self, f"do_{self.command}")()
            self.wfile.flush()

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Answer and record a request ``http.server`` refuses on its own account.

        ``parse_request`` calls this for a request line it cannot read, an HTTP
        version it does not speak and more headers than it accepts. The status
        is its own; the body and the record are this proxy's, under
        ``malformed_request``.
        """
        self._refuse("malformed_request", status=code)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._refuse_method
        raise AttributeError(name)

    @property
    def proxy(self) -> EgressProxy:
        return cast(EgressProxy, self.server)

    def _respond(
        self,
        status: int,
        text: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(
        self,
        reason: str,
        *,
        status: int = 403,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        method, target = _request_words(self.requestline)
        self.proxy.refusals.record(method, target, reason)
        self._respond(
            status, f"refused by the UX test egress proxy: {reason}", headers=headers
        )

    def _upstream_failed(self) -> None:
        self.proxy.count("upstream_failed")
        self._respond(502, "the UX test egress proxy could not reach the upstream")

    def _refuse_method(self) -> None:
        if _is_local_ui(_split(self.path)):
            self._refuse(
                "method_not_allowed", status=405, headers={"Allow": "GET, HEAD"}
            )
        else:
            self._refuse("method_not_allowed")

    def do_CONNECT(self) -> None:
        authority = _authority(self.path)
        if authority is None:
            self._refuse("host_not_allowed")
            return
        host, port = authority
        rules = self.proxy.rules
        if not rules.permits(host, port):
            listed = rules.permits(host, TLS_PORT)
            self._refuse("port_not_allowed" if listed else "host_not_allowed")
            return
        name = _normal_host(host)
        try:
            address = resolve_public(
                name,
                port,
                self.proxy.resolver,
                vpc_endpoint=name.endswith(AWS_SERVICE_SUFFIX),
            )
        except (OSError, UnicodeError, ValueError):
            self._upstream_failed()
            return
        if address is None:
            self._refuse("resolves_to_non_public_address")
            return
        if not self.proxy.tunnel_slots.acquire(blocking=False):
            self._refuse("too_many_tunnels", status=503)
            return
        try:
            self._tunnel(address, port)
        finally:
            self.proxy.tunnel_slots.release()

    def _tunnel(self, address: str, port: int) -> None:
        try:
            upstream = self.proxy.connect_upstream(address, port, _CONNECT_TIMEOUT)
        except OSError:
            self._upstream_failed()
            return
        with upstream:
            self.proxy.count("connect_allowed")
            self.send_response_only(200, "Connection Established")
            self.end_headers()
            _relay(self.connection, upstream, self.proxy.tunnel_idle)
        self.close_connection = True

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve()

    def _serve(self) -> None:
        """Serve a local-build file, or refuse the request.

        A request that says a body follows is answered and its connection
        closed. The proxy never reads a body, so on a kept-alive connection the
        body would otherwise be read as the next request.
        """
        parts = _split(self.path)
        if parts is None:
            self._refuse("malformed_request", status=400)
            return
        if not parts.scheme or not parts.netloc:
            self._refuse("not_a_proxy_request")
            return
        site = self.proxy.site
        if site is None or not _is_local_ui(parts):
            self._refuse("plain_http_not_allowed")
            return
        path = site.resolve(parts.path)
        if path is None:
            self._refuse("path_not_served", status=404)
            return
        try:
            descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW)
        except OSError:
            self._refuse("path_not_served", status=404)
            return
        with os.fdopen(descriptor, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            content_type = CONTENT_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            )
            self.proxy.count("served")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if _carries_body(self.headers):
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile, _CHUNK)


class EgressProxy(http.server.ThreadingHTTPServer):
    """The proxy server: rules, an optional local build, the refusal log, counts.

    ``resolver`` and ``connect`` are parameters so a test can answer names and
    open "upstream" connections itself, with no lookup and no outbound socket.
    Every count is taken before the client is answered, so whoever has seen a
    response also sees it counted in :meth:`stats`.

    At most ``max_connections`` connections are open at once, tunnels included:
    twice ``max_tunnels`` unless given, so a tunnel beyond its own cap is refused
    as ``too_many_tunnels``, with its target, before this cap closes anything. A
    connection beyond it is recorded as ``too_many_connections`` and closed
    before a byte of it is read, because reading it would take the thread the
    cap exists to save. ``request_timeout`` is how long each request head may
    take to arrive (:func:`_read_head`).

    Handler threads are daemons and :meth:`server_close` does not wait for them,
    because a tunnel can idle for ``tunnel_idle`` seconds and a SIGTERM must still
    end in a written stats file.
    """

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        *,
        rules: EgressRules,
        site: Site | None,
        log: RefusalLog,
        resolver: Callable[..., list[Any]] = socket.getaddrinfo,
        connect: Callable[[str, int, float], socket.socket] | None = None,
        max_tunnels: int = 64,
        tunnel_idle: float = 300.0,
        max_connections: int | None = None,
        request_timeout: float = HEAD_TIMEOUT,
    ) -> None:
        self.rules = rules
        self.site = site
        self.refusals = log
        self.resolver = resolver
        self.connect_upstream = connect or _connect
        self.tunnel_slots = threading.BoundedSemaphore(max_tunnels)
        self.tunnel_idle = tunnel_idle
        self.connection_slots = threading.BoundedSemaphore(
            2 * max_tunnels if max_connections is None else max_connections
        )
        self.request_timeout = request_timeout
        self._counter_lock = threading.Lock()
        self._counters = {"served": 0, "connect_allowed": 0, "upstream_failed": 0}
        super().__init__(address, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Hand the connection to a handler thread, or close it at the cap."""
        if not self.connection_slots.acquire(blocking=False):
            self.refusals.record("", "", "too_many_connections")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Serve one connection on its handler thread, then free its slot."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def server_bind(self) -> None:
        """Bind without ``http.server``'s reverse lookup of the listen address."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """One line on stderr instead of a traceback per failed connection."""
        failure = sys.exc_info()[1]
        print(
            f"ux_test_proxy: {type(failure).__name__} while serving a request",
            file=sys.stderr,
            flush=True,
        )

    def count(self, name: str) -> None:
        with self._counter_lock:
            self._counters[name] += 1

    def stats(self) -> dict[str, Any]:
        """The run's totals, in the shape the harness's summary reads."""
        by_reason = self.refusals.counts
        with self._counter_lock:
            counters = dict(self._counters)
        return {
            "served": counters["served"],
            "connect_allowed": counters["connect_allowed"],
            "refused": sum(by_reason.values()),
            "upstream_failed": counters["upstream_failed"],
            "by_reason": dict(sorted(by_reason.items())),
        }


def _listen_address(value: str) -> tuple[str, int]:
    """``--listen`` as an IPv4 loopback address and a port; port 0 is allowed."""
    host, sep, port_text = value.rpartition(":")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        not sep
        or not isinstance(address, ipaddress.IPv4Address)
        or not address.is_loopback
        or _PORT_RE.fullmatch(port_text) is None
        or int(port_text) > 65535
    ):
        raise EgressConfigError("--listen must be an IPv4 loopback address and a port")
    return host, int(port_text)


def _load_rules(path: Path) -> EgressRules:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise EgressConfigError("--rules cannot be read") from None
    except ValueError:
        raise EgressConfigError("--rules is not JSON") from None
    return EgressRules.from_json(data)


def _site(build_dir: Path) -> Site:
    """The local build, refused unless its ``index.html`` is readable by this uid.

    The proxy runs as ``uxproxy``, so this is the check that the harness's
    ``chmod`` of the build actually worked; without it every page would be a 404
    and the run would fail later, at sign-in, for a reason that names nothing.
    """
    try:
        with (build_dir / "index.html").open("rb"):
            pass
    except OSError:
        raise EgressConfigError("--build-dir has no readable index.html") from None
    return Site(build_dir)


def _write_atomically(path: Path, text: str) -> None:
    """Write ``text`` so a reader polling for ``path`` never sees half of it."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--listen",
        default=DEFAULT_LISTEN,
        help="loopback IPv4 address and port (default: %(default)s)",
    )
    parser.add_argument("--rules", required=True, help="egress rules JSON")
    parser.add_argument(
        "--build-dir", help="serve this static build at http://localhost:3000"
    )
    parser.add_argument("--refusal-log", help="append one JSON line per refusal")
    parser.add_argument("--stats", help="write the totals here on shutdown")
    parser.add_argument(
        "--ready-file", help="write the bound host:port here once listening"
    )
    parser.add_argument(
        "--max-tunnels",
        type=int,
        default=64,
        help=(
            "concurrent tunnels; open connections are capped at twice this "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--tunnel-idle",
        type=float,
        default=300.0,
        help="seconds a tunnel may idle (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the proxy until SIGTERM or SIGINT, then write the stats and exit 0.

    Exit 2 means a bad option, rules file or build directory, or an address that
    cannot be bound; the reason is on stderr, and no ready file is written.
    """
    args = _parser().parse_args(argv)
    try:
        host, port = _listen_address(args.listen)
        rules = _load_rules(Path(args.rules))
        site = _site(Path(args.build_dir)) if args.build_dir else None
        if args.max_tunnels < 1 or args.tunnel_idle <= 0:
            raise EgressConfigError("--max-tunnels and --tunnel-idle must be positive")
    except EgressConfigError as exc:
        print(f"ux_test_proxy: {exc}", file=sys.stderr, flush=True)
        return 2
    log = RefusalLog(Path(args.refusal_log) if args.refusal_log else None)
    try:
        server = EgressProxy(
            (host, port),
            rules=rules,
            site=site,
            log=log,
            max_tunnels=args.max_tunnels,
            tunnel_idle=args.tunnel_idle,
        )
    except OSError as exc:
        print(
            f"ux_test_proxy: cannot listen on {host}:{port}: {exc.strerror or exc}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    def stop(signum: int, frame: FrameType | None) -> None:
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bound_host, bound_port = server.server_address[:2]
    if args.ready_file:
        _write_atomically(Path(args.ready_file), f"{bound_host}:{bound_port}\n")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if args.stats:
            try:
                _write_atomically(
                    Path(args.stats), json.dumps(server.stats(), indent=2) + "\n"
                )
            except OSError as exc:
                print(
                    f"ux_test_proxy: cannot write --stats: {exc.strerror or exc}",
                    file=sys.stderr,
                    flush=True,
                )
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
