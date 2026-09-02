"""Tests for python/tools/osc_send.py: the OSC 1.0 codec, CLI arg parsing,
and a couple of real-socket functional checks of --listen/--ping-then-listen.

Standalone from the rest of the repo, same pattern as test_showmaster*.py:
osc_send.py only knows the OSC wire format, not duckswarm semantics.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import unittest
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from tools import osc_send  # noqa: E402


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# Codec: round-trips
# --------------------------------------------------------------------------


class RoundTripTest(unittest.TestCase):
    def test_no_args(self):
        data = osc_send.encode("/duckswarm/go", [])
        self.assertEqual(osc_send.decode(data), ("/duckswarm/go", []))

    def test_int_arg(self):
        data = osc_send.encode("/duckswarm/seek", [("i", 42)])
        self.assertEqual(osc_send.decode(data), ("/duckswarm/seek", [("i", 42)]))

    def test_negative_int_arg(self):
        data = osc_send.encode("/x", [("i", -7)])
        self.assertEqual(osc_send.decode(data), ("/x", [("i", -7)]))

    def test_float_arg(self):
        data = osc_send.encode("/duckswarm/play", [("f", 1.5)])
        address, args = osc_send.decode(data)
        self.assertEqual(address, "/duckswarm/play")
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0][0], "f")
        self.assertAlmostEqual(args[0][1], 1.5, places=5)

    def test_string_arg(self):
        data = osc_send.encode("/duckswarm/load", [("s", "demo")])
        self.assertEqual(osc_send.decode(data), ("/duckswarm/load", [("s", "demo")]))

    def test_string_arg_needs_full_padding_word(self):
        """A string whose raw+null length is already a multiple of 4 still
        gets one full extra padding word (OSC 1.0 always pads with >=1
        null and rounds up to a 4-byte boundary)."""
        data = osc_send.encode("/s", [("s", "abc")])  # "abc\0" == 4 bytes already
        self.assertEqual(osc_send.decode(data), ("/s", [("s", "abc")]))

    def test_bool_args(self):
        data = osc_send.encode("/duckswarm/flag", [("T", True), ("F", False)])
        self.assertEqual(osc_send.decode(data), ("/duckswarm/flag", [("T", True), ("F", False)]))

    def test_mixed_args(self):
        args = [("i", 3), ("f", -0.25), ("s", "hello world"), ("T", True)]
        data = osc_send.encode("/duckswarm/status/duck", args)
        address, decoded = osc_send.decode(data)
        self.assertEqual(address, "/duckswarm/status/duck")
        self.assertEqual(decoded[0], ("i", 3))
        self.assertAlmostEqual(decoded[1][1], -0.25, places=5)
        self.assertEqual(decoded[1][0], "f")
        self.assertEqual(decoded[2], ("s", "hello world"))
        self.assertEqual(decoded[3], ("T", True))

    def test_unicode_string_arg(self):
        data = osc_send.encode("/duckswarm/status/show", [("s", "démo-⚡")])
        self.assertEqual(osc_send.decode(data), ("/duckswarm/status/show", [("s", "démo-⚡")]))


# --------------------------------------------------------------------------
# Codec: exact byte vector (docs/osc-facade.md's own worked example)
# --------------------------------------------------------------------------


class ExactByteVectorTest(unittest.TestCase):
    def test_duckswarm_play_f_1_5(self):
        data = osc_send.encode("/duckswarm/play", [("f", 1.5)])
        expected = (
            b"/duckswarm/play\x00"  # 15 chars + 1 null = 16 bytes, already aligned
            + b",f\x00\x00"  # typetag ",f" + null, padded to 4
            + b"\x3f\xc0\x00\x00"  # big-endian float32 1.5
        )
        self.assertEqual(data, expected)
        self.assertEqual(len(data), 24)

    def test_float_bytes_match_struct_pack(self):
        data = osc_send.encode("/a", [("f", 1.5)])
        tail = data[-4:]
        self.assertEqual(tail, struct.pack(">f", 1.5))


# --------------------------------------------------------------------------
# Codec: malformed input
# --------------------------------------------------------------------------


class MalformedDecodeTest(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(ValueError):
            osc_send.decode(b"")

    def test_no_null_terminator(self):
        with self.assertRaises(ValueError):
            osc_send.decode(b"garbage-with-no-null-anywhere-in-here")

    def test_address_missing_leading_slash(self):
        data = osc_send._pad_string("nope") + osc_send._pad_string(",")
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_typetag_missing_comma(self):
        data = osc_send._pad_string("/a") + osc_send._pad_string("f")  # no leading ','
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_truncated_int_argument(self):
        data = osc_send._pad_string("/a") + osc_send._pad_string(",i") + b"\x00\x00"  # only 2 of 4 bytes
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_truncated_float_argument(self):
        data = osc_send._pad_string("/a") + osc_send._pad_string(",f")  # no payload at all
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_truncated_string_argument(self):
        # typetag says a string follows, but the datagram ends right there
        data = osc_send._pad_string("/a") + osc_send._pad_string(",s")
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_unknown_type_tag(self):
        data = osc_send._pad_string("/a") + osc_send._pad_string(",q") + b"\x00\x00\x00\x00"
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_bundle_is_rejected_not_crashed(self):
        # Real OSC bundles start "#bundle\0" followed by an 8-byte timetag,
        # not a comma-prefixed typetag string -- decode() must raise
        # ValueError (callers like --listen catch it and skip the datagram)
        # rather than raising something else or silently misparsing it.
        data = osc_send._pad_string("#bundle") + (b"\x00" * 8)
        with self.assertRaises(ValueError):
            osc_send.decode(data)

    def test_blob_is_parsed_and_ignored(self):
        payload = b"\xde\xad\xbe\xef"
        data = (
            osc_send._pad_string("/a")
            + osc_send._pad_string(",b")
            + struct.pack(">i", len(payload))
            + payload
        )
        address, args = osc_send.decode(data)
        self.assertEqual(address, "/a")
        self.assertEqual(args, [("b", None)])


# --------------------------------------------------------------------------
# encode() input validation
# --------------------------------------------------------------------------


class EncodeValidationTest(unittest.TestCase):
    def test_address_must_start_with_slash(self):
        with self.assertRaises(ValueError):
            osc_send.encode("duckswarm/play", [])

    def test_unsupported_type_tag_rejected(self):
        with self.assertRaises(ValueError):
            osc_send.encode("/a", [("q", 1)])

    def test_address_with_space_rejected(self):
        with self.assertRaises(ValueError):
            osc_send.encode("/duckswarm/oops here", [])


# --------------------------------------------------------------------------
# format_message()/_format_arg(): the wire format for --listen/
# --ping-then-listen output. scripts/e2e_osc.sh greps these lines verbatim
# (the /duckswarm/ack "i" field, the /duckswarm/status/duck line) even
# though docs/osc-facade.md does not pin the print format -- exercise them
# directly so a formatting change fails fast in this (ubuntu, fast) job
# instead of only in the macOS-only OSC e2e.
# --------------------------------------------------------------------------


class FormatMessageTest(unittest.TestCase):
    def test_ack_line_matches_e2e_osc_grep(self):
        # scripts/e2e_osc.sh: grep -q "^/duckswarm/ack $ack_cmd duck-01 1"
        line = osc_send.format_message(
            "/duckswarm/ack", [("s", "load"), ("s", "duck-01"), ("i", 1), ("s", "")]
        )
        self.assertEqual(line, "/duckswarm/ack load duck-01 1 ")

    def test_status_duck_line_matches_e2e_osc_grep(self):
        # scripts/e2e_osc.sh: re.match(r'^/duckswarm/status/duck \S+ \S+ playing ', line)
        line = osc_send.format_message(
            "/duckswarm/status/duck",
            [("s", "duck-01"), ("s", "lead"), ("s", "playing"), ("f", 3.25), ("f", 12.0), ("i", 1)],
        )
        self.assertEqual(line, "/duckswarm/status/duck duck-01 lead playing 3.250 12.000 1")

    def test_int_arg_formats_as_plain_integer(self):
        self.assertEqual(osc_send._format_arg(("i", 7)), "7")

    def test_float_arg_formats_with_three_decimals(self):
        self.assertEqual(osc_send._format_arg(("f", 1.5)), "1.500")

    def test_bool_args_format_as_python_bool_strings(self):
        self.assertEqual(osc_send._format_arg(("T", True)), "True")
        self.assertEqual(osc_send._format_arg(("F", False)), "False")

    def test_blob_arg_formats_as_placeholder(self):
        self.assertEqual(osc_send._format_arg(("b", None)), "<blob>")


# --------------------------------------------------------------------------
# CLI arg parsing
# --------------------------------------------------------------------------


class ParseArgTokenTest(unittest.TestCase):
    def test_int(self):
        self.assertEqual(osc_send.parse_arg_token("i:42"), ("i", 42))

    def test_negative_int(self):
        self.assertEqual(osc_send.parse_arg_token("i:-3"), ("i", -3))

    def test_float(self):
        self.assertEqual(osc_send.parse_arg_token("f:1.5"), ("f", 1.5))

    def test_string(self):
        self.assertEqual(osc_send.parse_arg_token("s:demo"), ("s", "demo"))

    def test_string_may_contain_colon(self):
        self.assertEqual(osc_send.parse_arg_token("s:127.0.0.1:53300"), ("s", "127.0.0.1:53300"))

    def test_true_flag(self):
        self.assertEqual(osc_send.parse_arg_token("T"), ("T", True))

    def test_false_flag(self):
        self.assertEqual(osc_send.parse_arg_token("F"), ("F", False))

    def test_bad_int(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("i:notanumber")

    def test_bad_float(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("f:notanumber")

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("q:1")

    def test_missing_colon(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("plainstring")

    def test_int_at_int32_bounds_accepted(self):
        self.assertEqual(osc_send.parse_arg_token("i:2147483647"), ("i", 2147483647))
        self.assertEqual(osc_send.parse_arg_token("i:-2147483648"), ("i", -2147483648))

    def test_int_beyond_int32_bounds_rejected(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("i:2147483648")
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("i:-2147483649")

    def test_float_beyond_float32_range_rejected(self):
        with self.assertRaises(ValueError):
            osc_send.parse_arg_token("f:1e50")

    def test_float_infinity_accepted(self):
        # inf/-inf pack fine as IEEE-754 float32 (see osc_send.FLOAT32_MAX's
        # docstring/comment) -- only a too-large *finite* value overflows.
        self.assertEqual(osc_send.parse_arg_token("f:inf"), ("f", float("inf")))


class ParseHostPortTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(osc_send.parse_hostport("127.0.0.1:53300"), ("127.0.0.1", 53300))

    def test_hostname(self):
        self.assertEqual(osc_send.parse_hostport("localhost:9"), ("localhost", 9))

    def test_missing_port_raises(self):
        with self.assertRaises(ValueError):
            osc_send.parse_hostport("127.0.0.1")

    def test_non_integer_port_raises(self):
        with self.assertRaises(ValueError):
            osc_send.parse_hostport("127.0.0.1:notaport")


class BuildParserTest(unittest.TestCase):
    def test_send_mode_positionals(self):
        ns = osc_send.build_parser().parse_args(["127.0.0.1:53300", "/duckswarm/load", "s:demo"])
        self.assertEqual(ns.target, "127.0.0.1:53300")
        self.assertEqual(ns.address, "/duckswarm/load")
        self.assertEqual(ns.args, ["s:demo"])
        self.assertIsNone(ns.listen)
        self.assertIsNone(ns.ping_then_listen)

    def test_send_mode_no_args(self):
        ns = osc_send.build_parser().parse_args(["127.0.0.1:53300", "/duckswarm/go"])
        self.assertEqual(ns.target, "127.0.0.1:53300")
        self.assertEqual(ns.address, "/duckswarm/go")
        self.assertEqual(ns.args, [])

    def test_send_mode_multiple_args(self):
        ns = osc_send.build_parser().parse_args(
            ["127.0.0.1:53300", "/duckswarm/status/duck", "s:duck-01", "f:1.5", "T"]
        )
        self.assertEqual(ns.args, ["s:duck-01", "f:1.5", "T"])

    def test_listen_mode(self):
        ns = osc_send.build_parser().parse_args(["--listen", "0.0.0.0:53301", "--seconds", "3"])
        self.assertEqual(ns.listen, "0.0.0.0:53301")
        self.assertEqual(ns.seconds, 3.0)
        self.assertIsNone(ns.target)
        self.assertIsNone(ns.address)

    def test_listen_mode_with_expect(self):
        ns = osc_send.build_parser().parse_args(
            ["--listen", "0.0.0.0:53301", "--seconds", "3", "--expect", "/a", "--expect", "/b"]
        )
        self.assertEqual(ns.expect, ["/a", "/b"])

    def test_ping_then_listen_mode(self):
        ns = osc_send.build_parser().parse_args(
            ["--ping-then-listen", "127.0.0.1:53300", "--seconds", "30", "--from", "53301"]
        )
        self.assertEqual(ns.ping_then_listen, "127.0.0.1:53300")
        self.assertEqual(ns.seconds, 30.0)
        self.assertEqual(ns.from_port, 53301)

    def test_default_seconds(self):
        ns = osc_send.build_parser().parse_args(["--listen", "0.0.0.0:53301"])
        self.assertEqual(ns.seconds, osc_send.DEFAULT_LISTEN_SECONDS)

    def test_default_expect_is_empty(self):
        ns = osc_send.build_parser().parse_args(["--listen", "0.0.0.0:53301"])
        self.assertEqual(ns.expect, [])

    def test_default_from_port_is_none(self):
        ns = osc_send.build_parser().parse_args(["127.0.0.1:53300", "/duckswarm/go"])
        self.assertIsNone(ns.from_port)


class MainModeValidationTest(unittest.TestCase):
    """main()'s mutual-exclusion / required-argument checks, via argparse's
    error path (SystemExit(2), message on stderr)."""

    def _run(self, argv):
        stderr = StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr
        try:
            with self.assertRaises(SystemExit) as ctx:
                osc_send.main(argv)
        finally:
            sys.stderr = old_stderr
        return ctx.exception.code, stderr.getvalue()

    def test_no_mode_at_all(self):
        code, err = self._run([])
        self.assertEqual(code, 2)
        self.assertIn("specify", err)

    def test_listen_and_send_together_rejected(self):
        code, err = self._run(["--listen", "0.0.0.0:53301", "127.0.0.1:53300", "/duckswarm/go"])
        self.assertEqual(code, 2)
        self.assertIn("mutually exclusive", err)

    def test_target_without_address_rejected(self):
        code, err = self._run(["127.0.0.1:53300"])
        self.assertEqual(code, 2)
        self.assertIn("address", err)

    def test_bad_arg_token_rejected(self):
        code, err = self._run(["127.0.0.1:53300", "/duckswarm/play", "f:notanumber"])
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# Functional: real UDP sockets, listener and sender talking to each other
# on loopback (unique ephemeral ports, so this is safe next to other tests).
# --------------------------------------------------------------------------


class ListenFunctionalTest(unittest.TestCase):
    def test_listen_sees_a_sent_message_and_satisfies_expect(self):
        # Bind synchronously on this thread first, then hand the already-bound
        # socket to _listen_loop (which listen() itself binds internally) --
        # this removes the bind-vs-send race a fixed sleep would otherwise
        # gamble on (see O31).
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        out = StringIO()
        result = {}

        def run_listener():
            result["code"] = osc_send._listen_loop(sock, seconds=1.5, expect=["/duckswarm/status/transport"], out=out)

        t = threading.Thread(target=run_listener)
        t.start()
        osc_send.send_once(f"127.0.0.1:{port}", "/duckswarm/status/transport", [("s", "playing")])
        t.join(timeout=5)
        alive = t.is_alive()
        sock.close()
        self.assertFalse(alive, "listener thread did not finish")
        self.assertEqual(result["code"], 0)
        self.assertIn("/duckswarm/status/transport playing", out.getvalue())

    def test_listen_exits_1_when_expect_never_arrives(self):
        port = _free_udp_port()
        out = StringIO()
        code = osc_send.listen("127.0.0.1", port, seconds=0.3, expect=["/duckswarm/status/transport"], out=out)
        self.assertEqual(code, 1)
        self.assertIn("/duckswarm/status/transport", out.getvalue())  # named in the "never saw" line

    def test_listen_ignores_malformed_datagram_and_keeps_going(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        out = StringIO()
        result = {}

        def run_listener():
            result["code"] = osc_send._listen_loop(sock, seconds=1.0, expect=["/ok"], out=out)

        t = threading.Thread(target=run_listener)
        t.start()
        junk_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        junk_sock.sendto(b"not-an-osc-message-no-null", ("127.0.0.1", port))
        junk_sock.close()
        osc_send.send_once(f"127.0.0.1:{port}", "/ok", [])
        t.join(timeout=5)
        alive = t.is_alive()
        sock.close()
        self.assertFalse(alive, "listener thread did not finish")
        self.assertEqual(result["code"], 0)
        self.assertIn("note: ignoring malformed datagram", out.getvalue())
        self.assertIn("/ok", out.getvalue())


class PingThenListenFunctionalTest(unittest.TestCase):
    def test_ping_is_sent_to_target_and_replies_are_seen(self):
        """A fake facade: receives the ping, learns the sender's (ip, port)
        from it, and replies with a status message -- exactly the pattern
        docs/osc-facade.md describes for the real swarmctl serve facade."""
        fake_facade = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fake_facade.bind(("127.0.0.1", 0))
        fake_facade_port = fake_facade.getsockname()[1]

        def fake_facade_loop():
            fake_facade.settimeout(3.0)
            try:
                data, addr = fake_facade.recvfrom(65536)
            except socket.timeout:
                return
            address, _ = osc_send.decode(data)
            if address == osc_send.PING_ADDRESS:
                fake_facade.sendto(osc_send.encode("/duckswarm/status/transport", [("s", "stopped")]), addr)

        server_thread = threading.Thread(target=fake_facade_loop)
        server_thread.start()

        out = StringIO()
        code = osc_send.ping_then_listen(
            f"127.0.0.1:{fake_facade_port}", seconds=1.5, expect=["/duckswarm/status/transport"], out=out
        )
        server_thread.join(timeout=5)
        fake_facade.close()

        self.assertEqual(code, 0)
        self.assertIn("/duckswarm/status/transport stopped", out.getvalue())


if __name__ == "__main__":
    unittest.main()
