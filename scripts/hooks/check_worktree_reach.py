#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Refuse credential-file reads and off-list fetches from an agent worktree.

Registered as a ``PreToolUse`` hook on ``Bash`` in ``.claude/settings.json``,
beside ``check_agent_filing.py``, and scoped the same way: it acts only when the
command runs from a worktree under ``$HOME/wt`` (or ``BACKLOG_WORKTREE_PREFIX``),
which is where fixer and reviewer agents work. The main checkout is never
refused.

Inside such a worktree it refuses a Bash command that

* names a local credential store: ``~/.aws``, ``~/.config/gh``, ``~/.netrc``
  (spelled with ``~``, ``$HOME``, ``${HOME}`` or the home path itself), or a
  process environment file, ``/proc/<anything>/environ``;
* runs ``curl``, ``wget``, ``nc``, ``ncat`` or ``netcat`` as its command
  (directly or through ``sudo``, ``env``, ``xargs`` and the like) against a host
  other than GitHub's, the repository's own git origin host, PyPI's, npm's or
  the loopback interface, or against a destination this hook cannot read off the
  command line (a URL in a variable, ``--connect-to``, ``--resolve``, a ``Host:``
  header, ``wget -i``). A proxy named with ``-x``/``--proxy`` is checked as a
  destination.

The rule behind both: agents working the backlog have no use for the host's
credentials, and the fetches they need go to the code and package hosts above.
``fix-single-issue.md`` and ``work-the-backlog.md`` state that rule for agents;
this hook refuses the plain spellings of breaking it.

⚠️ **This is text matching on the command line. It is not a capability
boundary.** A script file, ``python3 -c``, ``bash -c`` with an assembled string,
variable indirection, an encoded path, another language's HTTP client, or a
``cd`` out of the worktree all reach the same files and hosts without spelling
them where this hook looks; so do ``cd ~`` followed by a relative path,
``env`` or ``printenv`` (which read the environment without the ``/proc``
file), and an agent rewriting its own ``origin`` remote, since the origin host
is read from git. Credentials also come out of tools that name no file
(``gh auth token``, ``aws configure get``, ``git credential fill``) and out of
stores this hook does not list (``~/.git-credentials``, ``~/.docker``,
``~/.pypirc``, ``~/.npmrc``); ``/dev/tcp`` redirection, ``git``, ``pip``,
``npm`` or ``uv`` pointed at another host, ``-L`` redirects and an allowed host
used as the receiving end are outside it too. In the other direction a command
that merely mentions a credential path in a comment or a search pattern
(``grep -rn '~/.aws' docs/``) is refused; prefix it with the override. The control that would actually work is not having
readable long-lived credentials on the host, which is outside this repository.
What this buys is the plain case, and a line in the transcript when it fires.

Override: ``ALLOW_WORKTREE_REACH=1``, read from an inline assignment on the
command and from the environment, the same as the sibling hooks. An honoured
override prints one line to stderr naming the check it disabled, because a
variable exported by a shell profile or a runner persists and would otherwise
turn the check off for every command, invisibly.

A command the shell lexer cannot read (an unbalanced quote inside a heredoc,
say) still has its credential paths checked, which works on the raw text, and
its fetches checked from each ``curl``/``wget``/``nc`` word followed by a URL on
the same line. Anything else it cannot read is allowed, for the reason every hook
here gives: a guard that wedges the session is worse than one that misses a case.

Run the tests with ``make test-hooks``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

# The sibling hook's shell reader: newline is a separator, `#` mid-word is not a
# comment, and heredoc bodies are dropped as data. Imported rather than copied,
# so a fix to how one hook reads a command line reaches both. Hooks run as
# scripts, so their own directory is on the import path.
from check_shared_branch import SHELL_KEYWORDS
from check_shared_branch import segments as shell_segments

OVERRIDE = "ALLOW_WORKTREE_REACH"
AFFIRMATIVE = {"1", "true", "yes", "y", "on"}

#: Credential stores under the home directory, relative to it.
HOME_CREDENTIAL_PATHS = (".aws", ".config/gh", ".netrc")

#: ``/proc/<anything>environ``: ``self``, a pid, ``$PPID``, a glob, ``task/<n>/``,
#: ``self/../self`` and doubled slashes all leave ``/proc/`` before and ``environ``
#: after.
PROC_ENVIRON = re.compile(r"/proc/[^\s;&|<>]*environ\b")

#: The commands whose destination is checked.
FETCHERS = {"curl", "wget", "nc", "ncat", "netcat"}

#: Hosts a fetch may reach, matched exactly. GitHub's web, API, raw-content and
#: archive hosts; PyPI's index and file host; npm's registry. The repository's
#: GitLab origin host is added at run time from ``git remote get-url origin``.
ALLOWED_FETCH_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "www.npmjs.com",
    }
)

#: The options of each fetcher that take a separate value, so the word after one
#: is that value and not a URL. One table per tool, because the letters mean
#: different things: ``-d`` takes a value in curl and none in wget or nc.
#:
#: curl's and wget's are taken from ``curl --help all`` (curl 8.21.0) and
#: ``wget --help`` (GNU Wget 1.21.3); ``test_each_option_table_matches_the_tool``
#: re-derives them wherever the tool is installed. ``nc`` is two different
#: programs (OpenBSD netcat and Nmap's ncat, often installed as ``nc``), so its
#: table is the options that take a value in BOTH, from their manual pages; an
#: option that takes one in only one of them is read as a flag, which can only
#: make the next word be checked as a host, never skipped.
CURL_VALUE_OPTIONS = frozenset({
    "-A", "-C", "-D", "-E", "-F", "-H", "-K", "-P", "-Q", "-T", "-U", "-X", "-Y",
    "-b", "-c", "-d", "-e", "-h", "-m", "-o", "-r", "-t", "-u", "-w", "-x", "-y",
    "-z", "--abstract-unix-socket", "--alt-svc", "--aws-sigv4", "--cacert",
    "--capath", "--cert", "--cert-type", "--ciphers", "--config",
    "--connect-timeout", "--connect-to", "--continue-at", "--cookie",
    "--cookie-jar", "--create-file-mode", "--crlfile", "--curves", "--data",
    "--data-ascii", "--data-binary", "--data-raw", "--data-urlencode",
    "--delegation", "--dns-interface", "--dns-ipv4-addr", "--dns-ipv6-addr",
    "--dns-servers", "--doh-url", "--dump-header", "--ech", "--egd-file",
    "--engine", "--etag-compare", "--etag-save", "--expect100-timeout", "--form",
    "--form-string", "--ftp-account", "--ftp-alternative-to-user", "--ftp-method",
    "--ftp-port", "--ftp-ssl-ccc-mode", "--happy-eyeballs-timeout-ms",
    "--haproxy-clientip", "--header", "--help", "--hostpubmd5", "--hostpubsha256",
    "--hsts", "--interface", "--ip-tos", "--ipfs-gateway", "--json",
    "--keepalive-cnt", "--keepalive-time", "--key", "--key-type", "--knownhosts",
    "--krb", "--libcurl", "--limit-rate", "--local-port", "--login-options",
    "--mail-auth", "--mail-from", "--mail-rcpt", "--max-filesize", "--max-redirs",
    "--max-time", "--netrc-file", "--noproxy", "--oauth2-bearer", "--output",
    "--output-dir", "--parallel-max", "--parallel-max-host", "--pass",
    "--pinnedpubkey", "--preproxy", "--proto", "--proto-default", "--proto-redir",
    "--proxy", "--proxy-cacert", "--proxy-capath", "--proxy-cert",
    "--proxy-cert-type", "--proxy-ciphers", "--proxy-crlfile", "--proxy-header",
    "--proxy-key", "--proxy-key-type", "--proxy-pass", "--proxy-pinnedpubkey",
    "--proxy-service-name", "--proxy-tls13-ciphers", "--proxy-tlsauthtype",
    "--proxy-tlspassword", "--proxy-tlsuser", "--proxy-user", "--pubkey", "--quote",
    "--random-file", "--range", "--rate", "--referer", "--request",
    "--request-target", "--resolve", "--retry", "--retry-delay", "--retry-max-time",
    "--sasl-authzid", "--service-name", "--sigalgs", "--socks4", "--socks4a",
    "--socks5", "--socks5-gssapi-service", "--socks5-hostname", "--speed-limit",
    "--speed-time", "--ssl-sessions", "--stderr", "--telnet-option",
    "--tftp-blksize", "--time-cond", "--tls-max", "--tls13-ciphers",
    "--tlsauthtype", "--tlspassword", "--tlsuser", "--trace", "--trace-ascii",
    "--trace-config", "--unix-socket", "--upload-file", "--upload-flags", "--url",
    "--url-query", "--user", "--user-agent", "--variable", "--vlan-priority",
    "--write-out",
})  # fmt: skip
WGET_VALUE_OPTIONS = frozenset({
    "-A", "-B", "-D", "-I", "-O", "-P", "-Q", "-R", "-T", "-U", "-X", "-a", "-e",
    "-i", "-l", "-o", "-t", "-w", "--accept", "--accept-regex", "--append-output",
    "--backups", "--base", "--bind-address", "--body-data", "--body-file",
    "--ca-certificate", "--ca-directory", "--certificate", "--certificate-type",
    "--ciphers", "--compression", "--config", "--connect-timeout", "--crl-file",
    "--cut-dirs", "--default-page", "--directory-prefix", "--dns-timeout",
    "--domains", "--exclude-directories", "--exclude-domains", "--execute",
    "--follow-tags", "--ftp-password", "--ftp-user", "--header", "--http-password",
    "--http-user", "--ignore-tags", "--include-directories", "--input-file",
    "--input-metalink", "--level", "--limit-rate", "--load-cookies",
    "--local-encoding", "--metalink-index", "--method", "--output-document",
    "--output-file", "--password", "--pinnedpubkey", "--post-data", "--post-file",
    "--prefer-family", "--private-key", "--private-key-type", "--progress",
    "--proxy-password", "--proxy-user", "--quota", "--read-timeout", "--referer",
    "--regex-type", "--reject", "--reject-regex", "--rejected-log",
    "--remote-encoding", "--report-speed", "--restrict-file-names",
    "--retry-on-http-error", "--save-cookies", "--secure-protocol", "--start-pos",
    "--timeout", "--tries", "--use-askpass", "--user", "--user-agent", "--wait",
    "--waitretry", "--warc-dedup", "--warc-file", "--warc-header",
    "--warc-max-size", "--warc-tempdir",
})  # fmt: skip
NC_VALUE_OPTIONS = frozenset({"-i", "-p", "-s", "-w", "-x"})
NCAT_VALUE_OPTIONS = frozenset({
    "-c", "-e", "-g", "-G", "-m", "-d", "-o", "-x", "-i", "-p", "-s", "-w",
    "--sh-exec", "--exec", "--lua-exec", "--max-conns", "--delay", "--output",
    "--hex-dump", "--idle-timeout", "--source-port", "--source", "--wait",
    "--proxy", "--proxy-type", "--proxy-auth", "--ssl-cert", "--ssl-key",
    "--ssl-trustfile", "--ssl-ciphers", "--ssl-servername", "--ssl-alpn",
    "--allow", "--allowfile", "--deny", "--denyfile",
})  # fmt: skip

VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "curl": CURL_VALUE_OPTIONS,
    "wget": WGET_VALUE_OPTIONS,
    "nc": NC_VALUE_OPTIONS,
    "netcat": NC_VALUE_OPTIONS,
    "ncat": NCAT_VALUE_OPTIONS,
}

#: Per tool, the options whose value is where the connection goes, checked like
#: a URL: curl's proxies and ``--url``, OpenBSD nc's ``-x`` proxy, ncat's
#: ``--proxy``. wget names its proxy through ``-e``, read in :func:`_wget_setting`.
DESTINATION_OPTIONS: dict[str, frozenset[str]] = {
    "curl": frozenset({
        "-x", "--proxy", "--preproxy", "--url", "--socks4", "--socks4a",
        "--socks5", "--socks5-hostname",
    }),
    "wget": frozenset(),
    "nc": frozenset({"-x"}),
    "netcat": frozenset({"-x"}),
    "ncat": frozenset({"--proxy"}),
}  # fmt: skip

#: Per tool, the options that redirect the connection in a form this hook does
#: not read (curl's ``--connect-to a:443:b:443`` and ``--resolve a:443:ip``), or
#: read the URLs from a file (curl's ``-K`` config, which can hold ``url =``
#: lines; wget's ``-i`` list and ``--config``). A fetch carrying one is refused.
UNREADABLE_OPTIONS: dict[str, frozenset[str]] = {
    "curl": frozenset({"--connect-to", "--resolve", "-K", "--config"}),
    "wget": frozenset({"-i", "--input-file", "--config"}),
    "nc": frozenset(),
    "netcat": frozenset(),
    "ncat": frozenset(),
}

#: wget ``-e`` settings that name a proxy, and the one that turns on proxies taken
#: from the environment, which this hook cannot see.
WGET_PROXY_SETTINGS = frozenset({"http_proxy", "https_proxy", "ftp_proxy"})

#: Commands that run a later word as the command, so ``sudo curl`` and
#: ``xargs curl`` run curl, with the options of each that take a separate value.
#: Shell keywords (``if``, ``then``, ``{``, ``!`` ...) are skipped the same way.
WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "xargs": frozenset({"-n", "-I", "-L", "-P", "-d", "-a", "-s", "-E", "-l"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-f", "-o"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "builtin": frozenset(),
    "watch": frozenset({"-n", "--interval", "-d"}),
    "strace": frozenset({"-e", "-o", "-p", "-s", "-u", "-E"}),
    "busybox": frozenset(),
}

#: Proxy variables an assignment in front of the command can set.
PROXY_VARIABLE = re.compile(r"^(?:https?|all|ftp)_proxy$", re.IGNORECASE)

#: The loopback interface carries nothing off the host, and local test servers
#: (``sam local``, a probe on ``127.0.0.1``) live there.
LOOPBACK = re.compile(r"^(?:localhost|127(?:\.\d{1,3}){3}|::1|\[::1\])$")

#: A token spelled like a host name, with an optional port and path.
HOSTLIKE = re.compile(
    r"^(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\.?|"
    r"localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\])(?::\d+)?(?:[/?#].*)?$"
)


def _affirmative(value: str | None) -> bool:
    return value is not None and value.strip().lower() in AFFIRMATIVE


def _inline_override(segments: list[list[str]]) -> bool:
    for tokens in segments:
        for token in tokens:
            name, sep, value = token.partition("=")
            if not sep or not name.replace("_", "").isalnum():
                break
            if name == OVERRIDE and _affirmative(value):
                return True
    return False


def _worktree_root(cwd: str | None) -> Path | None:
    configured = os.environ.get("BACKLOG_WORKTREE_PREFIX")
    home = os.environ.get("HOME")
    prefix = Path(configured) if configured else (Path(home) / "wt" if home else None)
    if prefix is None or not cwd:
        return None
    try:
        here = Path(cwd).resolve()
        root = prefix.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return root if here == root or root in here.parents else None


def _credential_paths(command: str, home: str) -> list[str]:
    """Credential stores the command text names, in any of its spellings."""
    found: list[str] = []
    # Quotes split a path without changing it ("$HOME"/.aws), and "/./" and
    # doubled slashes do not change it either, so the text is normalised first.
    command = command.replace('"', "").replace("'", "")
    command = re.sub(r"/(?:\./)+", "/", re.sub(r"/{2,}", "/", command))
    command = re.sub(r"~[A-Za-z0-9._-]+(?=/)", "~", command)
    previous = None
    while previous != command:  # a/b/../c is a/c
        previous = command
        command = re.sub(r"/(?!\.\.(?:/|$))[^/\s]+/\.\.(?=/|$)", "", command)
    homes = [
        re.escape(spelling) for spelling in ("~", "$HOME", "${HOME}", home) if spelling
    ]
    for relative in HOME_CREDENTIAL_PATHS:
        pattern = (
            r"(?:(?<![\w/.~$])|(?<=[=:]))(?:"
            + "|".join(homes)
            + r")/+"
            + re.escape(relative).replace("/", "/+")
            + r"(?![\w.-])"
        )
        if re.search(pattern, command):
            found.append(f"~/{relative}")
    if PROC_ENVIRON.search(command):
        found.append("/proc/*/environ")
    return found


def _origin_host(cwd: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603, S607 - fixed argv
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    url = result.stdout.strip()
    if "://" in url:
        host = urllib.parse.urlsplit(url).hostname or ""
    else:  # scp-like user@host:path
        match = re.match(r"^(?:[^@/:]+@)?([A-Za-z0-9.-]+):", url)
        host = match.group(1) if match else ""
    host = host.lower().rstrip(".")
    return host if "." in host else ""


def _host(token: str) -> str | None:
    """The host a URL or host-like token names, lower-cased; ``None`` if neither."""
    if "://" in token:
        try:
            host = urllib.parse.urlsplit(token).hostname
        except ValueError:
            return "?"
        return (host or "?").lower().rstrip(".")
    match = HOSTLIKE.match(token)
    return match.group("host").lower().strip("[]").rstrip(".") if match else None


def _command_word(tokens: list[str]) -> tuple[str, list[str]] | None:
    """The fetcher a segment runs and its arguments, or ``None``.

    The command word is the first token after leading assignments, shell
    keywords, and wrappers that run a later word (``sudo -u x``, ``timeout -s
    KILL 5``) with their options. A fetcher's name anywhere else (``grep -rn
    curl``, ``which curl``, ``echo curl ...``) is an argument, not a fetch. A
    proxy variable assigned in front of the command is returned as a ``-x``
    argument, so it is checked as a destination; ``env -S '<command>'`` is read as
    the command it spells.
    """
    proxies: list[str] = []
    via_xargs = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = token.rsplit("/", 1)[-1].lstrip("\\")
        assigned = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", token)
        if assigned:
            if PROXY_VARIABLE.match(assigned.group(1)):
                proxies += ["-x", assigned.group(2)]
            index += 1
        elif token in SHELL_KEYWORDS:
            index += 1
        elif name in WRAPPERS:
            via_xargs = via_xargs or name == "xargs"
            takes_value = WRAPPERS[name]
            index += 1
            while index < len(tokens):
                current = tokens[index]
                if name == "env" and current in ("-S", "--split-string"):
                    try:
                        inner = shlex.split(tokens[index + 1])
                    except (IndexError, ValueError):
                        return name, ["$"]  # a command this cannot read
                    return _command_word(inner + tokens[index + 2 :])
                if current in takes_value:
                    index += 2
                elif current.startswith("-") or re.fullmatch(
                    r"\d+(?:\.\d+)?[smhd]?", current
                ):
                    index += 1
                else:
                    break
        else:
            if name in FETCHERS:
                # xargs appends arguments read from stdin, which may be URLs.
                return name, proxies + tokens[index + 1 :] + (
                    ["$"] if via_xargs else []
                )
            return None
    return None


def _short_cluster(token: str, takes_value: frozenset[str]) -> tuple[str, str] | None:
    """For a short-option cluster such as ``-sSLx`` or ``-xhttp://p``, the first
    option in it that takes a value for this tool, and the value attached after
    it (empty when the value is the next word)."""
    if not re.match(r"^-[A-Za-z]", token) or token.startswith("--"):
        return None
    for position, letter in enumerate(token[1:], start=1):
        if f"-{letter}" in takes_value:
            return f"-{letter}", token[position + 1 :]
    return None


def _wget_setting(value: str) -> str | None:
    """The host a wget ``-e key=value`` setting sends traffic to, ``"?"`` for one
    this hook cannot read, or ``None`` for a setting that changes neither."""
    key, _, setting = value.partition("=")
    key = key.strip().lower()
    if key in WGET_PROXY_SETTINGS:
        setting = setting.strip()
        return _host(setting) or _host(f"http://{setting}") or "?"
    if key == "use_proxy" and setting.strip().lower() in AFFIRMATIVE:
        return "?"
    return None


def allowed_fetch_hosts(cwd: str) -> frozenset[str]:
    """``ALLOWED_FETCH_HOSTS`` plus the worktree's ``origin`` host and its web name.

    The origin is read from git, so an agent that rewrites its own ``origin``
    widens this set; that is one of the routes the header lists.
    """
    origin = _origin_host(cwd)
    extra = {origin, origin.removeprefix("ssh.")} - {""}
    return ALLOWED_FETCH_HOSTS | extra


def _fetch_hosts(tokens: list[str]) -> tuple[str, list[str]] | None:
    """The fetcher a segment runs and the hosts it names, or ``None``.

    ``"?"`` stands for a destination this hook cannot read: a variable or a
    command substitution where a URL would be, or an option that redirects the
    connection. A fetch naming no host at all (``curl --version``) names none.
    Options are read against the tool's own table, so a flag is never taken to
    swallow the word after it.
    """
    found = _command_word(tokens)
    if found is None:
        return None
    name, arguments = found
    takes_value = VALUE_OPTIONS[name]
    destination = DESTINATION_OPTIONS[name]
    unreadable = UNREADABLE_OPTIONS[name]
    hosts: list[str] = []

    def option_value(option: str, value: str) -> None:
        if option in unreadable:
            hosts.append("?")
        elif option in destination:
            hosts.append(_host(value) or _host(f"http://{value}") or "?")
        elif name == "curl" and option in ("-H", "--header"):
            if value.lower().startswith("host:"):
                hosts.append("?")
        elif name == "wget" and option in ("-e", "--execute"):
            host = _wget_setting(value)
            if host is not None:
                hosts.append(host)

    pending = ""
    for token in arguments:
        if pending:
            option_value(pending, token)
            pending = ""
        elif token.startswith("--"):
            option, equals, inline = token.partition("=")
            if option in takes_value or option in unreadable or option in destination:
                if equals:
                    option_value(option, inline)
                else:
                    pending = option
            elif option in unreadable:
                hosts.append("?")
        elif token.startswith("-") and token != "-":
            cluster = _short_cluster(token, takes_value | unreadable | destination)
            if cluster:
                option, inline = cluster
                if inline:
                    option_value(option, inline)
                else:
                    pending = option
        elif "$" in token or "`" in token:
            hosts.append("?")
        else:
            host = _host(token)
            if host is not None:
                hosts.append(host)
    if pending:
        option_value(pending, "")
    return name, hosts


def decide(payload: dict) -> tuple[str | None, list[str]]:
    """Return a refusal reason, or ``None`` to allow, plus stderr notices."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None, []
    command = tool_input.get("command")
    cwd = payload.get("cwd")
    if not isinstance(command, str) or _worktree_root(cwd) is None:
        return None, []

    segments = shell_segments(command)
    problems = [
        f"names {path}"
        for path in _credential_paths(command, os.environ.get("HOME", ""))
    ]
    fetches = [fetch for tokens in segments if (fetch := _fetch_hosts(tokens))]
    if fetches:
        allowed = allowed_fetch_hosts(str(cwd))
        for name, hosts in fetches:
            for host in hosts:
                if host not in allowed and not LOOPBACK.match(host):
                    shown = "a destination it cannot read" if host == "?" else host
                    problems.append(f"runs {name} against {shown}")

    if not problems:
        return None, []
    if _inline_override(segments) or _affirmative(os.environ.get(OVERRIDE)):
        source = (
            "inline assignment" if _inline_override(segments) else "the environment"
        )
        return None, [
            f"check-worktree-reach: refusal disabled by {OVERRIDE}=1 from {source} "
            f"for a command that {'; '.join(problems)}."
        ]
    return (
        "check-worktree-reach: refusing this command in an agent worktree: it "
        + "; ".join(problems)
        + ".\n\nAgents working the backlog hold no AWS, GitHub or other host "
        "credentials, and fetch only from GitHub, this repository's GitLab host, "
        "PyPI and npm. If the work genuinely needs this, stop and ask the "
        f"coordinator; to run it deliberately, prefix the command with {OVERRIDE}=1."
    ), []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    try:
        reason, notices = decide(payload)
    except Exception:  # noqa: BLE001 - a guard must not wedge the session
        return 0
    for line in notices:
        print(line, file=sys.stderr)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
