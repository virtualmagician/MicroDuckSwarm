"""--master-host must actually pin. It did not.

`_configured_master_addr` was read exactly once, to seed `master_addr`, and
`_set_master_addr` then overwrote it from the source address of any inbound
packet. The agent binds 0.0.0.0 and nothing on this protocol is authenticated,
so the one control the docs offered against a foreign sender did not exist: any
host that could reach the agent's port could send a `cmd` and have it executed,
or a `time_resp` and move the show clock.

These tests pin the filter itself and, just as importantly, the two ways it is
deliberately loose. It matches on host and never on port, because a master's
source port legitimately varies and matching it would strand a duck for no
security gain. And a --master-host that will not resolve disables the pin
rather than the duck, because a cast that refuses every master is worse on a
stage than one that accepts any: "panic always works from any state" is a hard
rule and a pin that can brick eight ducks is not a safety feature.

See docs/swarmlink-protocol.md #0.
"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from duck_agent.agent import _FOREIGN_LOG_CAP, DuckAgent, _resolve_master_ips  # noqa: E402
from tests.test_agent import FakeMaster, FakeRobotd  # noqa: E402
from tests.test_agent_safety import SHOWS_DIR  # noqa: E402


class ResolveMasterIps(unittest.TestCase):
    """No test here may depend on a working resolver.

    `.invalid` is reserved by RFC 6761 and must not resolve, but a captive
    portal or a venue DNS that answers everything with its own address makes
    that false, and these tests would then fail on exactly the network a show
    runs on. Anything that needs a resolver failure fakes one.
    """

    def test_an_ip_literal_resolves_to_itself(self) -> None:
        self.assertEqual(_resolve_master_ips("127.0.0.1", "duck-test"), frozenset({"127.0.0.1"}))

    def test_localhost_resolves_to_the_v4_loopback_specifically(self) -> None:
        # Specifically 127.0.0.1, not "::1 counts too". The agent's socket is
        # AF_INET, so a v6 address in this set can never match a source and
        # would be a pin nothing satisfies. An earlier version of this test
        # accepted either and would have blessed exactly that bug.
        self.assertEqual(_resolve_master_ips("localhost", "duck-test"), frozenset({"127.0.0.1"}))

    def test_an_ipv6_host_is_unpinned_rather_than_unmatchable(self) -> None:
        # The whole failure this guards: an AF_INET socket's recvfrom source is
        # always a dotted quad, so a pin of {"::1"} matches nothing, forever,
        # including panic. It must degrade to unpinned, not to deaf.
        for host in ("::1", "fd7a:115c:a1e0::1"):
            with self.subTest(host=host):
                with self.assertLogs("duck_agent.agent", level="ERROR"):
                    self.assertIsNone(_resolve_master_ips(host, "duck-test"))

    def test_a_host_resolving_only_to_ipv6_is_unpinned(self) -> None:
        # The same case reached through a NAME rather than a literal, faked so
        # it does not need a v6-only host to exist on this network.
        with mock.patch("duck_agent.agent.socket.getaddrinfo",
                        return_value=[(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", 0, 0, 0))]):
            with self.assertLogs("duck_agent.agent", level="ERROR"):
                self.assertIsNone(_resolve_master_ips("v6only.example", "duck-test"))

    def test_a_resolver_failure_returns_none_rather_than_an_empty_set(self) -> None:
        # None is "unpinned". An empty frozenset would be "trust nobody", which
        # is the failure mode this must never have.
        with mock.patch("duck_agent.agent.socket.getaddrinfo", side_effect=socket.gaierror(8, "nope")):
            with self.assertLogs("duck_agent.agent", level="ERROR") as logs:
                self.assertIsNone(_resolve_master_ips("whatever.example", "duck-test"))
        self.assertIn("does not resolve", "\n".join(logs.output))

    def test_a_malformed_hostname_does_not_escape_as_an_exception(self) -> None:
        # getaddrinfo raises UnicodeError (a ValueError, NOT an OSError) from
        # the IDNA codec for an empty or over-long label. Uncaught it escapes
        # DuckAgent.__init__ and crash-loops the agent under systemd, so a
        # typo in agent.env becomes a duck that never starts.
        for host in ("duck..local", "x" * 70 + ".local"):
            with self.subTest(host=host):
                with self.assertLogs("duck_agent.agent", level="ERROR"):
                    self.assertIsNone(_resolve_master_ips(host, "duck-test"))


class _PinnedAgentTest(unittest.TestCase):
    """One agent, one robotd, and two masters: the pinned one and a stranger.

    Both are on 127.0.0.1, so the stranger differs only by source PORT. That is
    the point: a port-matching filter would reject the real master too, and a
    host-matching one must accept both. The foreign-source case is covered by
    the unit tests above and by test_a_foreign_host_is_dropped below, which
    fakes the source rather than needing a second interface.
    """

    master_host = None

    def setUp(self) -> None:
        self.robotd = FakeRobotd()
        self.master = FakeMaster()
        self.agent = DuckAgent(
            duck_id="duck-test",
            robotd_target=self.robotd.target,
            shows_dir=SHOWS_DIR,
            listen_port=0,
            master_host=self.master_host,
        )
        self.agent.start()
        self.agent_addr = ("127.0.0.1", self.agent.bound_port)
        self.addCleanup(self.agent.stop)
        self.addCleanup(self.robotd.stop)
        self.addCleanup(self.master.stop)

    def _wait(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


class Unpinned(_PinnedAgentTest):
    master_host = None

    def test_without_master_host_anyone_is_the_master(self) -> None:
        # The default, and the behaviour before this change. Worth pinning so
        # the security note in the docs stays true rather than aspirational.
        self.assertIsNone(self.agent._allowed_master_ips)
        cmd_id = self.master.send_cmd(self.agent_addr, "stop")
        self.assertTrue(self.master.wait_for_ack(cmd_id), "an unpinned agent must answer anyone")


class PinnedToLoopback(_PinnedAgentTest):
    master_host = "127.0.0.1"

    def test_the_pin_is_recorded(self) -> None:
        self.assertEqual(self.agent._allowed_master_ips, frozenset({"127.0.0.1"}))

    def test_a_master_on_the_pinned_host_is_accepted_whatever_its_source_port(self) -> None:
        # The agent was configured with the default master port 47800; this
        # FakeMaster is on an ephemeral one. A port-matching filter would drop
        # every real command here.
        self.assertNotEqual(self.master.port, 47800)
        cmd_id = self.master.send_cmd(self.agent_addr, "stop")
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], f"pinned host rejected on port grounds: {acks}")

    def test_a_second_master_on_the_same_host_is_also_accepted(self) -> None:
        # Same host, a different ephemeral port again. Nothing about the pin
        # is per-connection.
        other = FakeMaster()
        self.addCleanup(other.stop)
        cmd_id = other.send_cmd(self.agent_addr, "stop")
        self.assertTrue(other.wait_for_ack(cmd_id), "a second sender on the pinned host was dropped")

    def test_a_foreign_host_is_dropped_before_dispatch(self) -> None:
        # Drive _from_master directly with a source that is not the pin: a real
        # second interface is not available in a unit test, and this is the
        # exact predicate _recv_loop consults before it parses anything.
        self.assertTrue(self.agent._from_master(("127.0.0.1", 51000)))
        with self.assertLogs("duck_agent.agent", level="WARNING") as logs:
            self.assertFalse(self.agent._from_master(("10.11.12.13", 47800)))
        self.assertIn("10.11.12.13", "\n".join(logs.output))

    def test_a_flooding_foreign_source_is_logged_once(self) -> None:
        # The journal on a duck lives on zram (docs/provisioning.md), so one
        # line per dropped packet is its own denial of service. Asserts on the
        # LOG, not on the dedup set: an implementation that logged every packet
        # while still populating the set would pass the weaker check.
        with self.assertLogs("duck_agent.agent", level="WARNING") as logs:
            for port in range(1, 60):
                self.assertFalse(self.agent._from_master(("10.11.12.13", port)))
        self.assertEqual(len(logs.output), 1, f"one line per packet: {logs.output[:3]}")

    def test_many_distinct_foreign_sources_do_not_grow_the_set_without_bound(self) -> None:
        # The dedup set is keyed by an attacker-chosen source address. Trading
        # a log flood for unbounded memory on a duck is the worse of the two.
        for i in range(_FOREIGN_LOG_CAP * 3):
            self.agent._from_master((f"10.11.{i // 256}.{i % 256}", 47800))
        self.assertLessEqual(len(self.agent._logged_foreign), _FOREIGN_LOG_CAP,
                             "per-source log suppression state must be bounded")


class PinnedToAnUnresolvableName(_PinnedAgentTest):
    master_host = "whatever.example"

    def setUp(self) -> None:
        # Fake the resolver failure rather than trusting that some name will
        # not resolve: a captive portal answers everything, and this test would
        # then fail on exactly the kind of network a show runs on.
        patcher = mock.patch("duck_agent.agent.socket.getaddrinfo",
                             side_effect=socket.gaierror(8, "nope"))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_an_unresolvable_pin_leaves_the_duck_reachable(self) -> None:
        # Rule 5: panic always works from any state. A pin that can brick a
        # cast because venue DNS is not up yet is not a safety feature.
        self.assertIsNone(self.agent._allowed_master_ips)
        cmd_id = self.master.send_cmd(self.agent_addr, "panic")
        acks = self.master.wait_for_ack(cmd_id)
        self.assertTrue(acks and acks[0]["ok"], f"an unresolvable pin made the duck deaf: {acks}")


class TheReceiveLoopActuallyConsultsThePin(_PinnedAgentTest):
    """The tests above prove the predicate. These prove it is WIRED IN.

    Written after the first version of this file passed with the filter call
    deleted from _recv_loop: every test drove _from_master directly, so none of
    them covered the one line that makes it do anything. A second loopback
    address is not bindable on macOS, so instead of faking the source we narrow
    the pin to an address the real FakeMaster does not have, and send for real.
    """

    master_host = "127.0.0.1"

    def _repin(self, ips) -> None:
        self.agent._allowed_master_ips = frozenset(ips)
        self.agent._logged_foreign.clear()

    def test_a_command_from_an_unpinned_source_is_never_acked(self) -> None:
        self._repin({"10.11.12.13"})
        cmd_id = self.master.send_cmd(self.agent_addr, "stop")
        self.assertEqual(self.master.wait_for_ack(cmd_id, timeout=1.0), [],
                         "a datagram from outside the pin reached the command handler")

    def test_the_same_command_is_acked_once_the_pin_includes_the_sender(self) -> None:
        # Guards the test above: without this it would also pass for an agent
        # that had simply stopped answering anything.
        self._repin({"10.11.12.13"})
        self.assertEqual(self.master.wait_for_ack(self.master.send_cmd(self.agent_addr, "stop"), timeout=1.0), [])
        self._repin({"127.0.0.1"})
        acks = self.master.wait_for_ack(self.master.send_cmd(self.agent_addr, "stop"))
        self.assertTrue(acks and acks[0]["ok"], f"agent stopped answering the pinned host too: {acks}")

    def test_a_foreign_time_resp_cannot_reach_the_clock(self) -> None:
        # The reason the filter runs before parsing rather than per handler:
        # a time_resp is not a command, needs no ack, and moves the show clock.
        self.master.send_cmd(self.agent_addr, "stop")
        self.assertTrue(self._wait(lambda: self.agent.clock.sample_count() >= 1), "no baseline sync")
        self._repin({"10.11.12.13"})
        before = self.agent.clock.sample_count()
        for _ in range(20):
            self.master.send_raw(self.agent_addr, {
                "v": 1, "type": "time_resp", "t0": time.monotonic_ns(),
                "t1": time.monotonic_ns(), "t2": time.monotonic_ns(),
            })
        time.sleep(0.4)
        self.assertEqual(self.agent.clock.sample_count(), before,
                         "a time_resp from outside the pin was fed to the clock")


class PinAppliesToEveryDatagramType(_PinnedAgentTest):
    master_host = "127.0.0.1"

    def test_a_real_datagram_of_every_type_is_dropped(self) -> None:
        # An earlier version of this test looped over type names while calling
        # _from_master with the same tuple every time, so it never varied the
        # thing it claimed to vary and a puppet-exempt filter would have passed
        # it. Send real datagrams of each type instead, with the pin narrowed
        # so this sender is foreign, and assert nothing moved.
        self.agent._allowed_master_ips = frozenset({"10.11.12.13"})
        self.agent._logged_foreign.clear()
        before_moves = len(self.robotd.by_method("robot.move"))
        before_samples = self.agent.clock.sample_count()

        cmd_id = self.master.send_cmd(self.agent_addr, "stop")
        self.master.send_raw(self.agent_addr, {
            "v": 1, "type": "time_resp", "t0": time.monotonic_ns(),
            "t1": time.monotonic_ns(), "t2": time.monotonic_ns()})
        self.master.send_raw(self.agent_addr, {
            "v": 1, "type": "state", "seq": 1, "transport": "playing", "show_time": 1.0})
        for seq in range(1, 20):
            self.master.send_raw(self.agent_addr, {
                "v": 1, "type": "puppet", "seq": seq, "master_time": time.monotonic_ns(),
                "move": {"vx": 0.25, "vy": 0.0, "vyaw": 0.0}})
        time.sleep(0.4)

        self.assertEqual(self.master.wait_for_ack(cmd_id, timeout=0.5), [], "cmd reached the handler")
        self.assertEqual(self.agent.clock.sample_count(), before_samples, "time_resp reached the clock")
        self.assertEqual(len(self.robotd.by_method("robot.move")), before_moves,
                         "a puppet frame from outside the pin moved the duck")

    def test_a_pinned_agent_still_syncs_its_clock(self) -> None:
        # The end-to-end proof that the filter did not break the one exchange
        # the agent initiates itself.
        self.master.send_cmd(self.agent_addr, "stop")  # teach it the master address
        self.assertTrue(self._wait(lambda: self.agent.clock.sample_count() >= 1),
                        "a pinned agent never completed a time sync")


if __name__ == "__main__":
    unittest.main()
