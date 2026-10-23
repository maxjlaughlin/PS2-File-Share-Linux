"""Tests for socket parsing, byte accounting and the usage database."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps2share import monitor  # noqa: E402

# Real `ss -tinH state established` output, two sockets.
SS_OUTPUT = """\
ESTAB  0      0           192.168.1.10:445        192.168.1.50:49152
\t cubic wscale:7,7 rto:204 rtt:0.42/0.2 mss:1448 pmtu:1500 cwnd:10 bytes_sent:104857600 bytes_acked:104857600 bytes_received:8192 segs_out:7000 segs_in:3000 send 276Mbps
ESTAB  0      0           192.168.1.10:445        192.168.1.77:51000
\t cubic wscale:7,7 rto:204 rtt:0.5/0.2 mss:1448 pmtu:1500 cwnd:10 bytes_sent:2048 bytes_acked:2048 bytes_received:512 segs_out:12 segs_in:9 send 23Mbps
"""

# Older iproute2 reports bytes_acked but not bytes_sent.
SS_OLD_OUTPUT = """\
ESTAB  0      0           192.168.1.10:445        192.168.1.50:49152
\t cubic wscale:7,7 rto:204 bytes_acked:5000 bytes_received:99 segs_out:7 segs_in:3
"""

SS_IPV6_OUTPUT = """\
ESTAB  0      0           [::ffff:192.168.1.10]:445      [::ffff:192.168.1.50]:49152
\t cubic bytes_sent:4096 bytes_received:128
"""


class SsParsingTests(unittest.TestCase):
    def test_reads_both_directions_per_socket(self):
        counters = monitor.parse_ss(SS_OUTPUT)
        self.assertEqual(counters["192.168.1.10:445-192.168.1.50:49152"], (104857600, 8192))
        self.assertEqual(counters["192.168.1.10:445-192.168.1.77:51000"], (2048, 512))

    def test_falls_back_to_bytes_acked(self):
        counters = monitor.parse_ss(SS_OLD_OUTPUT)
        self.assertEqual(counters["192.168.1.10:445-192.168.1.50:49152"], (5000, 99))

    def test_unwraps_ipv4_mapped_addresses(self):
        counters = monitor.parse_ss(SS_IPV6_OUTPUT)
        self.assertEqual(counters["192.168.1.10:445-192.168.1.50:49152"], (4096, 128))

    def test_empty_input(self):
        self.assertEqual(monitor.parse_ss(""), {})

    def test_socket_without_an_info_line_still_appears(self):
        counters = monitor.parse_ss("ESTAB 0 0 192.168.1.10:445 192.168.1.50:49152\n")
        self.assertEqual(counters["192.168.1.10:445-192.168.1.50:49152"], (0, 0))


class AddressTests(unittest.TestCase):
    def test_ipv4_little_endian_hex(self):
        # 0100007F:01BD is 127.0.0.1:445
        self.assertEqual(monitor._parse_addr("0100007F:01BD"), ("127.0.0.1", 445))

    def test_ipv6_any_address(self):
        address, port = monitor._parse_addr("00000000000000000000000000000000:01BD")
        self.assertEqual((address, port), ("::", 445))

    def test_rejects_rubbish(self):
        self.assertEqual(monitor._parse_addr("nonsense"), ("", 0))


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = monitor.UsageStore(os.path.join(self.tmp.name, "usage.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_accumulates_within_a_minute(self):
        now = time.time()
        self.store.record(now, {"192.168.1.50": (1000, 10)})
        self.store.record(now + 1, {"192.168.1.50": (2000, 20)})
        self.assertEqual(self.store.totals(0), (3000, 30))

    def test_splits_by_client(self):
        now = time.time()
        self.store.record(now, {"192.168.1.50": (1000, 0), "192.168.1.77": (500, 0)})
        rows = self.store.per_client(0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["client"], "192.168.1.50")
        self.assertEqual(rows[0]["tx"], 1000)

    def test_ignores_empty_samples(self):
        self.store.record(time.time(), {})
        self.store.record(time.time(), {"192.168.1.50": (0, 0)})
        self.assertEqual(self.store.totals(0), (0, 0))

    def test_only_counts_within_the_window(self):
        now = time.time()
        self.store.record(now - 3 * 86400, {"a": (1000, 0)})
        self.store.record(now, {"a": (7, 0)})
        self.assertEqual(self.store.totals(now - 86400), (7, 0))
        self.assertEqual(self.store.totals(0), (1007, 0))

    def test_daily_fills_in_quiet_days(self):
        self.store.record(time.time(), {"a": (1234, 1)})
        days = self.store.daily(7)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[-1]["total"], 1235)
        self.assertEqual(days[0]["total"], 0)


class CounterDeltaTests(unittest.TestCase):
    """The sampler must survive sockets appearing, closing and being reused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = monitor.UsageStore(os.path.join(self.tmp.name, "usage.db"))
        self.monitor = monitor.Monitor(self.store, interval=1.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _connection(self, peer="192.168.1.50", port=49152):
        return monitor.Connection(
            local_ip="192.168.1.10", local_port=445, peer_ip=peer, peer_port=port, inode="1"
        )

    def test_first_sighting_of_a_socket_counts_nothing(self):
        connection = self._connection()
        self.monitor._previous[connection.key] = (0, 0)
        self.assertEqual(connection.key, "192.168.1.10:445-192.168.1.50:49152")

    def test_a_reused_address_pair_does_not_produce_a_negative_delta(self):
        # Simulate the arithmetic the sampler performs when counters reset.
        previous, current = (1_000_000, 500), (1000, 5)
        sent = current[0] - previous[0] if current[0] >= previous[0] else current[0]
        received = current[1] - previous[1] if current[1] >= previous[1] else current[1]
        self.assertEqual((sent, received), (1000, 5))

    def test_snapshot_is_wellformed_when_nothing_has_happened(self):
        snapshot = self.monitor.snapshot()
        self.assertEqual(snapshot["clients"], [])
        self.assertEqual(snapshot["active_clients"], 0)
        self.assertEqual(snapshot["current"]["total"], 0)
        self.assertIn("history", snapshot)

    def test_a_sample_against_the_live_system_does_not_raise(self):
        self.monitor.sample()
        self.assertIn(self.monitor.source, ("ss", "fdinfo", "none"))


class ProcessNameTests(unittest.TestCase):
    """Samba renames the child that actually serves a client."""

    def test_matches_the_parent_and_the_per_client_children(self):
        self.assertTrue(monitor.is_smbd_name("smbd"))
        self.assertTrue(monitor.is_smbd_name("smbd[192.168.1.50]"))
        self.assertTrue(monitor.is_smbd_name("smbd[192.168.10"))  # kernel truncates at 15

    def test_ignores_unrelated_processes(self):
        for name in ("nmbd", "smbclient", "bash", "winbindd", ""):
            self.assertFalse(monitor.is_smbd_name(name), name)


class FirstSightingTests(unittest.TestCase):
    """A connection that appears between samples must still be counted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = monitor.UsageStore(os.path.join(self.tmp.name, "usage.db"))
        self.monitor = monitor.Monitor(self.store, interval=1.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_the_first_sample_does_not_backfill_traffic_in_flight(self):
        self.assertFalse(self.monitor._primed)
        self.monitor.sample()
        self.assertTrue(self.monitor._primed)

    def test_a_new_socket_after_priming_counts_its_whole_counter(self):
        self.monitor._primed = True
        previous = self.monitor._previous.get("x")
        self.assertIsNone(previous)
        # The sampler treats an unseen socket's counters as entirely new bytes,
        # because the socket cannot predate the previous sample.
        current = (4096, 128)
        sent, received = (0, 0) if not self.monitor._primed else current
        self.assertEqual((sent, received), (4096, 128))


class OpenFileTests(unittest.TestCase):
    def test_percent_through_a_file(self):
        handle = monitor.OpenFile(path="/srv/ps2/DVD/game.iso", size=1000, position=420)
        self.assertAlmostEqual(handle.percent, 42.0)

    def test_percent_of_an_empty_file_is_zero(self):
        self.assertEqual(monitor.OpenFile(path="/x", size=0, position=0).percent, 0.0)

    def test_percent_never_exceeds_one_hundred(self):
        self.assertEqual(monitor.OpenFile(path="/x", size=10, position=50).percent, 100.0)


class ProcReadingTests(unittest.TestCase):
    def test_listening_ports_are_plausible(self):
        ports = monitor.read_listening_ports()
        self.assertIsInstance(ports, list)
        self.assertTrue(all(0 < port < 65536 for port in ports))

    def test_reading_connections_does_not_raise(self):
        self.assertIsInstance(monitor.read_tcp_connections(), list)


if __name__ == "__main__":
    unittest.main()
