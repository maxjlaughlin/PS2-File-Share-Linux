"""Tests for address discovery and the firewall helper."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps2share import netinfo  # noqa: E402


def with_firewall(tool, active=True, allows=False):
    return mock.patch.object(
        netinfo,
        "firewall_state",
        return_value={"tool": tool, "active": active, "allows_smb": allows, "detail": ""},
    )


class FirewallCommandTests(unittest.TestCase):
    """The web interface port has to be openable too: a firewall that blocks
    the UI cannot be opened from the UI."""

    def test_ufw_opens_both_smb_and_the_web_interface(self):
        with with_firewall("ufw"):
            commands = netinfo.firewall_commands("192.168.1.0/24", ui_port=8723)
        self.assertEqual(
            commands,
            [
                "ufw allow from 192.168.1.0/24 to any app Samba",
                "ufw allow from 192.168.1.0/24 to any port 8723 proto tcp",
            ],
        )

    def test_ufw_ui_only(self):
        with with_firewall("ufw"):
            commands = netinfo.firewall_commands("10.0.0.0/8", smb=False, ui_port=8723)
        self.assertEqual(commands, ["ufw allow from 10.0.0.0/8 to any port 8723 proto tcp"])

    def test_firewalld_reloads_once_at_the_end(self):
        with with_firewall("firewalld"):
            commands = netinfo.firewall_commands(ui_port=8723)
        self.assertEqual(
            commands,
            [
                "firewall-cmd --permanent --add-service=samba",
                "firewall-cmd --permanent --add-port=8723/tcp",
                "firewall-cmd --reload",
            ],
        )

    def test_firewalld_without_anything_to_do_does_not_reload(self):
        with with_firewall("firewalld"):
            self.assertEqual(netinfo.firewall_commands(smb=False, ui_port=0), [])

    def test_nftables_puts_every_port_in_one_rule(self):
        with with_firewall("nftables"):
            commands = netinfo.firewall_commands("192.168.0.0/16", ui_port=8723)
        self.assertEqual(len(commands), 1)
        self.assertIn("{445, 139, 8723}", commands[0])
        self.assertIn("192.168.0.0/16", commands[0])

    def test_smb_only_leaves_the_ui_port_alone(self):
        with with_firewall("ufw"):
            commands = netinfo.firewall_commands("192.168.1.0/24", ui_port=0)
        self.assertEqual(commands, ["ufw allow from 192.168.1.0/24 to any app Samba"])

    def test_no_firewall_means_no_commands(self):
        with with_firewall(""):
            self.assertEqual(netinfo.firewall_commands(ui_port=8723), [])

    def test_a_default_scope_is_used_when_none_is_known(self):
        with with_firewall("ufw"):
            commands = netinfo.firewall_commands("", ui_port=8723)
        self.assertTrue(all("192.168.0.0/16" in c for c in commands))

    def test_open_firewall_is_a_no_op_without_a_firewall(self):
        with with_firewall(""):
            result = netinfo.open_firewall(ui_port=8723)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ran"], [])


class AddressTests(unittest.TestCase):
    def test_interfaces_are_wellformed(self):
        for row in netinfo.interfaces():
            self.assertIn("address", row)
            self.assertIn("network", row)
            self.assertIsInstance(row["primary"], bool)

    def test_exactly_one_primary_at_most(self):
        self.assertLessEqual(sum(1 for r in netinfo.interfaces() if r["primary"]), 1)

    def test_loopback_is_not_offered(self):
        self.assertFalse(any(r["address"].startswith("127.") for r in netinfo.interfaces()))

    def test_opl_settings_name_the_share_and_port(self):
        settings = netinfo.opl_settings("PS2SMB", "guest", "ps2")
        self.assertEqual(settings["share_name"], "PS2SMB")
        self.assertEqual(settings["server_port"], 445)
        self.assertEqual(settings["user"], "guest")

    def test_opl_settings_in_user_mode(self):
        settings = netinfo.opl_settings("GAMES", "user", "max")
        self.assertEqual(settings["user"], "max")
        self.assertIn("password", settings["password"].lower())


if __name__ == "__main__":
    unittest.main()
