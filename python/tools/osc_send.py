#!/usr/bin/env python3
"""osc_send.py -- stdlib-only OSC 1.0 / UDP encoder, decoder, and CLI.

Companion tool for docs/osc-facade.md's `swarmctl serve` OSC facade: used
by scripts/e2e_osc.sh, by python/tests/test_osc_send.py, and as a quick
operator/dev tool for poking the facade by hand.

Single file, stdlib only (see CLAUDE.md). No dependency on the rest of
this repo -- it only knows the OSC 1.0 wire format, not duckswarm
semantics.

    # send one datagram
    python3 tools/osc_send.py 127.0.0.1:53300 /duckswarm/load s:demo
    python3 tools/osc_send.py 127.0.0.1:53300 /duckswarm/play f:1.5
    python3 tools/osc_send.py 127.0.0.1:53300 /duckswarm/go

    # passively listen for feedback already subscribed by someone else
    python3 tools/osc_send.py --listen 0.0.0.0:53301 --seconds 3

    # subscribe yourself (ping the facade every 2s) and listen on the same
    # socket for its feedback, failing if the given addresses never arrive
    python3 tools/osc_send.py --ping-then-listen 127.0.0.1:53300 --seconds 30 \\
        --expect /duckswarm/status/transport --expect /duckswarm/status/duck

An OSC argument, on the wire and in this module's Python API, is a
`(typetag, value)` pair: `("i", 3)`, `("f", 1.5)`, `("s", "demo")`,
`("T", True)`, `("F", False)`. `T`/`F` carry no payload on the wire; the
Python value is carried along only for convenience (round-tripping
`decode(encode(...))` gives back `True`/`False`). `encode`/`decode` are
importable for tests and other tools.
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
from typing import Any, List, Optional, Sequence, Tuple

Arg = Tuple[str, Any]

PING_ADDRESS = "/duckswarm/ping"
PING_INTERVAL_S = 2.0
DEFAULT_LISTEN_SECONDS = 3.0
RECV_BUFSIZE = 65536
# OSC 1.0 "i"/"f" are 32-bit; parse_arg_token rejects CLI values outside
# these bounds so encode()'s struct.pack(">i"/">f", ...) can never raise
# struct.error/OverflowError -- it only ever raises ValueError, which
# main() already handles as a clean CLI error.
INT32_MIN = -2147483648
INT32_MAX = 2147483647
FLOAT32_MAX = 3.4028234663852886e38  # largest finite value struct('>f') can pack
# recvfrom() poll granularity while a --listen/--ping-then-listen loop is
# also watching a wall-clock deadline and (in ping mode) a ping interval;
# small enough that both are timely without busy-looping.
POLL_INTERVAL_S = 0.2


# --------------------------------------------------------------------------
# OSC 1.0 codec: address pattern, ',' typetag string, big-endian i/f,
# null-padded s, T/F (no payload), b (parsed and ignored on decode).
# --------------------------------------------------------------------------


def _osc_pad(raw: bytes) -> bytes:
    """Null-terminate then pad to a multiple of 4 bytes (OSC 1.0 string rule:
    always at least one null, total length a multiple of 4)."""
    b = raw + b"\x00"
    pad = (-len(b)) % 4
    return b + b"\x00" * pad


def _pad_string(s: str) -> bytes:
    return _osc_pad(s.encode("utf-8"))


def _read_osc_string(data: bytes, pos: int) -> Tuple[str, int]:
    if pos >= len(data):
        raise ValueError("truncated OSC message: expected a string, found nothing")
    terminator = data.find(b"\x00", pos)
    if terminator == -1:
        raise ValueError("malformed OSC message: unterminated string (no null byte)")
    raw_len = terminator - pos
    total_len = raw_len + 1 + ((-(raw_len + 1)) % 4)
    end = pos + total_len
    if end > len(data):
        raise ValueError("malformed OSC message: string padding runs past end of datagram")
    try:
        s = data[pos:terminator].decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"malformed OSC message: invalid utf-8 in string: {e}") from e
    return s, end


def encode(address: str, args: Sequence[Arg]) -> bytes:
    """Encode one OSC message (never a bundle) as a UDP payload.

    `args` is a sequence of `(typetag, value)` pairs; typetag is one of
    "i" (int32), "f" (float32), "s" (str), "T"/"F" (bool, no payload).
    """
    if not address.startswith("/"):
        raise ValueError(f"OSC address must start with '/': {address!r}")
    if any(ch in address for ch in " \x00"):
        raise ValueError(f"OSC address must not contain spaces or null bytes: {address!r}")

    typetags = "," + "".join(tag for tag, _ in args)
    parts = [_pad_string(address), _pad_string(typetags)]
    for tag, value in args:
        if tag == "i":
            parts.append(struct.pack(">i", int(value)))
        elif tag == "f":
            parts.append(struct.pack(">f", float(value)))
        elif tag == "s":
            parts.append(_pad_string(str(value)))
        elif tag in ("T", "F"):
            pass  # no payload on the wire
        else:
            raise ValueError(f"unsupported OSC type tag {tag!r} (support: i, f, s, T, F)")
    return b"".join(parts)


def decode(data: bytes) -> Tuple[str, List[Arg]]:
    """Decode one OSC UDP payload into `(address, args)`.

    Raises ValueError on anything malformed, including OSC bundles
    (`#bundle...`) -- this facade's inbound traffic is single messages
    only (docs/osc-facade.md); callers that may see a bundle (e.g. the
    `--listen` loop) should catch ValueError and skip the datagram.
    """
    if not data:
        raise ValueError("malformed OSC message: empty datagram")
    address, pos = _read_osc_string(data, 0)
    if address.startswith("#bundle"):
        raise ValueError("OSC bundles are not supported")
    if not address.startswith("/"):
        raise ValueError(f"malformed OSC message: address does not start with '/': {address!r}")

    typetags, pos = _read_osc_string(data, pos)
    if not typetags.startswith(","):
        raise ValueError(f"malformed OSC message: type tag string missing ',' prefix: {typetags!r}")

    args: List[Arg] = []
    for tag in typetags[1:]:
        if tag == "i":
            if pos + 4 > len(data):
                raise ValueError("malformed OSC message: truncated 'i' argument")
            (value,) = struct.unpack_from(">i", data, pos)
            pos += 4
            args.append(("i", value))
        elif tag == "f":
            if pos + 4 > len(data):
                raise ValueError("malformed OSC message: truncated 'f' argument")
            (value,) = struct.unpack_from(">f", data, pos)
            pos += 4
            args.append(("f", value))
        elif tag == "s":
            s, pos = _read_osc_string(data, pos)
            args.append(("s", s))
        elif tag == "T":
            args.append(("T", True))
        elif tag == "F":
            args.append(("F", False))
        elif tag == "b":
            if pos + 4 > len(data):
                raise ValueError("malformed OSC message: truncated blob length")
            (blob_len,) = struct.unpack_from(">i", data, pos)
            pos += 4
            if blob_len < 0 or pos + blob_len > len(data):
                raise ValueError("malformed OSC message: truncated blob data")
            pos += blob_len + ((-blob_len) % 4)
            args.append(("b", None))  # parsed and ignored, per docs/osc-facade.md
        else:
            raise ValueError(f"malformed OSC message: unknown type tag {tag!r}")
    return address, args


# --------------------------------------------------------------------------
# CLI arg parsing
# --------------------------------------------------------------------------


def parse_hostport(value: str) -> Tuple[str, int]:
    if ":" not in value:
        raise ValueError(f"expected HOST:PORT, got {value!r}")
    host, _, port_s = value.rpartition(":")
    if not host:
        raise ValueError(f"expected HOST:PORT, got {value!r}")
    try:
        port = int(port_s)
    except ValueError:
        raise ValueError(f"expected HOST:PORT with an integer port, got {value!r}") from None
    return host, port


def parse_arg_token(token: str) -> Arg:
    """Parse one CLI OSC-arg token: "i:1", "f:1.5", "s:text", "T", or "F"."""
    if token == "T":
        return ("T", True)
    if token == "F":
        return ("F", False)
    if ":" not in token:
        raise ValueError(f"OSC arg must be i:N, f:N, s:TEXT, T, or F, got {token!r}")
    tag, _, raw = token.partition(":")
    if tag == "i":
        try:
            ivalue = int(raw)
        except ValueError:
            raise ValueError(f"OSC 'i' arg must be an integer, got {token!r}") from None
        if not (INT32_MIN <= ivalue <= INT32_MAX):
            raise ValueError(
                f"OSC 'i' arg must fit in int32 ({INT32_MIN}..{INT32_MAX}), got {token!r}"
            )
        return ("i", ivalue)
    if tag == "f":
        try:
            fvalue = float(raw)
        except ValueError:
            raise ValueError(f"OSC 'f' arg must be a number, got {token!r}") from None
        if math.isfinite(fvalue) and abs(fvalue) > FLOAT32_MAX:
            raise ValueError(f"OSC 'f' arg must fit in float32, got {token!r}")
        return ("f", fvalue)
    if tag == "s":
        return ("s", raw)
    raise ValueError(f"unknown OSC arg type {tag!r} in {token!r} (expected i, f, or s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="osc_send.py",
        description="Send or receive OSC 1.0 / UDP messages (stdlib only).",
    )
    p.add_argument("target", nargs="?", metavar="HOST:PORT", help="Send-mode destination.")
    p.add_argument("address", nargs="?", metavar="/address", help="Send-mode OSC address, e.g. /duckswarm/play.")
    p.add_argument("args", nargs="*", metavar="ARG", help="Send-mode OSC args: i:N, f:N, s:TEXT, T, F.")

    p.add_argument(
        "--listen",
        metavar="HOST:PORT",
        help="Listen mode: bind HOST:PORT and print every received message for --seconds.",
    )
    p.add_argument(
        "--ping-then-listen",
        dest="ping_then_listen",
        metavar="HOST:PORT",
        help=f"Like --listen, but HOST:PORT is a remote OSC endpoint: send {PING_ADDRESS} to it "
        f"every {PING_INTERVAL_S:g}s from the listening socket (renews a facade's status subscription) "
        "while listening for --seconds.",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_LISTEN_SECONDS,
        help=f"How long --listen/--ping-then-listen listens (default {DEFAULT_LISTEN_SECONDS:g}).",
    )
    p.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="/address",
        help="Address that must be seen at least once before --seconds elapses (repeatable). "
        "If given, exit 1 unless every one was seen.",
    )
    p.add_argument(
        "--from",
        dest="from_port",
        type=int,
        default=None,
        metavar="PORT",
        help="Local UDP port to send/listen from (default: an OS-assigned ephemeral port). "
        "Fixing this lets a facade's replies find their way back to a specific socket.",
    )
    return p


# --------------------------------------------------------------------------
# Message formatting for --listen/--ping-then-listen output
# --------------------------------------------------------------------------


def _format_arg(arg: Arg) -> str:
    tag, value = arg
    if tag == "f":
        return f"{value:.3f}"
    if tag == "i":
        return str(value)
    if tag == "s":
        return str(value)
    if tag in ("T", "F"):
        return "True" if tag == "T" else "False"
    return "<blob>"


def format_message(address: str, args: Sequence[Arg]) -> str:
    return " ".join([address] + [_format_arg(a) for a in args])


# --------------------------------------------------------------------------
# Send mode
# --------------------------------------------------------------------------


def send_once(target: str, address: str, args: Sequence[Arg], from_port: Optional[int] = None) -> None:
    host, port = parse_hostport(target)
    payload = encode(address, args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if from_port is not None:
            sock.bind(("0.0.0.0", from_port))
        sock.sendto(payload, (host, port))
    finally:
        sock.close()


# --------------------------------------------------------------------------
# Listen / ping-then-listen mode
# --------------------------------------------------------------------------


def _listen_loop(
    sock: socket.socket,
    seconds: float,
    expect: Sequence[str],
    ping_target: Optional[Tuple[str, int]] = None,
    out=sys.stdout,
) -> int:
    """Drive one --listen/--ping-then-listen session on an already-bound
    socket: print every valid message, ping `ping_target` (if given) every
    PING_INTERVAL_S starting immediately, for `seconds`. Returns the process
    exit code: 0 if every `expect` address was seen, else 1.

    Every print is flushed immediately: `out` is commonly a file a
    driving script (e.g. scripts/e2e_osc.sh) polls from another process
    while this one is still running, and a fully-buffered redirected
    stdout would otherwise sit on lines for many KB or until exit.
    """
    seen: set[str] = set()
    sock.settimeout(POLL_INTERVAL_S)
    deadline = time.monotonic() + max(0.0, seconds)
    next_ping = 0.0  # due immediately, so the facade subscribes this socket right away

    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        if ping_target is not None and now >= next_ping:
            try:
                sock.sendto(encode(PING_ADDRESS, []), ping_target)
            except OSError as e:
                print(f"note: ping to {ping_target} failed: {e}", file=out, flush=True)
            next_ping = now + PING_INTERVAL_S

        try:
            data, addr = sock.recvfrom(RECV_BUFSIZE)
        except socket.timeout:
            continue
        except OSError:
            break

        try:
            address, args = decode(data)
        except ValueError as e:
            print(f"note: ignoring malformed datagram from {addr}: {e}", file=out, flush=True)
            continue

        seen.add(address)
        print(format_message(address, args), file=out, flush=True)

    missing = [a for a in expect if a not in seen]
    if missing:
        print(f"never saw: {', '.join(missing)}", file=out, flush=True)
        return 1
    return 0


def listen(host: str, port: int, seconds: float, expect: Sequence[str], out=sys.stdout) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        return _listen_loop(sock, seconds, expect, ping_target=None, out=out)
    finally:
        sock.close()


def ping_then_listen(
    target: str, seconds: float, expect: Sequence[str], from_port: Optional[int] = None, out=sys.stdout
) -> int:
    host, port = parse_hostport(target)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", from_port if from_port is not None else 0))
        return _listen_loop(sock, seconds, expect, ping_target=(host, port), out=out)
    finally:
        sock.close()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    mode_flags = [bool(ns.listen), bool(ns.ping_then_listen), bool(ns.target)]
    if sum(mode_flags) == 0:
        parser.error("specify HOST:PORT /address ... to send, or --listen/--ping-then-listen to receive")
    if sum(mode_flags) > 1:
        parser.error("--listen, --ping-then-listen, and send-mode HOST:PORT are mutually exclusive")

    if ns.listen:
        try:
            host, port = parse_hostport(ns.listen)
        except ValueError as e:
            parser.error(str(e))
        return listen(host, port, ns.seconds, ns.expect)

    if ns.ping_then_listen:
        try:
            parse_hostport(ns.ping_then_listen)  # validate early for a clean error
        except ValueError as e:
            parser.error(str(e))
        return ping_then_listen(ns.ping_then_listen, ns.seconds, ns.expect, from_port=ns.from_port)

    # send mode
    if not ns.address:
        parser.error("send mode requires an OSC address, e.g. /duckswarm/load")
    try:
        typed_args = [parse_arg_token(t) for t in ns.args]
    except ValueError as e:
        parser.error(str(e))
    try:
        send_once(ns.target, ns.address, typed_args, from_port=ns.from_port)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"sent: {format_message(ns.address, typed_args)} -> {ns.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
