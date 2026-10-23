"""Tests for the Samba config generator and the smb.conf include block."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps2share import samba  # noqa: E402
from ps2share.config import Config  # noqa: E402


def make_config(**overrides) -> Config:
    config = Config()
    config.share_path = "/srv/ps2"
    config.force_user = "ps2"
    for key, value in overrides.items():
        setattr(config, key, value)
    config.normalise()
    return config


class RenderTests(unittest.TestCase):
    def test_enables_the_dialect_and_auth_opl_needs(self):
        text = samba.render_config(make_config())
        self.assertIn("server min protocol = NT1", text)
        self.assertIn("ntlm auth = yes", text)
        self.assertIn("server signing = disabled", text)
        self.assertIn("[PS2SMB]", text)
        self.assertIn("path = /srv/ps2", text)

    def test_guest_mode_maps_unknown_users_to_the_guest_account(self):
        text = samba.render_config(make_config(access_mode="guest"))
        self.assertIn("map to guest = Bad User", text)
        self.assertIn("guest ok = yes", text)
        self.assertNotIn("valid users", text)

    def test_user_mode_locks_the_share_to_one_account(self):
        text = samba.render_config(make_config(access_mode="user", username="ps2"))
        self.assertIn("map to guest = Never", text)
        self.assertIn("guest ok = no", text)
        self.assertIn("valid users = ps2", text)

    def test_smb1_only_caps_the_max_protocol(self):
        self.assertIn("server max protocol = NT1", samba.render_config(make_config(smb1_only=True)))
        self.assertIn("server max protocol = SMB3", samba.render_config(make_config(smb1_only=False)))

    def test_netbios_toggle_controls_port_139(self):
        self.assertIn("smb ports = 445 139", samba.render_config(make_config(enable_netbios=True)))
        self.assertIn("smb ports = 445", samba.render_config(make_config(enable_netbios=False)))
        self.assertNotIn("139", samba.render_config(make_config(enable_netbios=False)))

    def test_read_only_share(self):
        self.assertIn("read only = yes", samba.render_config(make_config(writable=False)))
        self.assertIn("read only = no", samba.render_config(make_config(writable=True)))

    def test_performance_block_is_optional(self):
        self.assertIn("use sendfile = yes", samba.render_config(make_config(tuned_performance=True)))
        self.assertNotIn("use sendfile", samba.render_config(make_config(tuned_performance=False)))

    def test_oplocks_are_off_because_opl_cannot_answer_a_break(self):
        text = samba.render_config(make_config())
        self.assertIn("oplocks = no", text)
        self.assertIn("level2 oplocks = no", text)
        self.assertIn("strict locking = no", text)

    def test_wide_links_toggle(self):
        self.assertIn("wide links = yes", samba.render_config(make_config(wide_links=True)))
        self.assertIn("wide links = no", samba.render_config(make_config(wide_links=False)))

    def test_share_name_reaches_the_section_header(self):
        self.assertIn("[GAMES]", samba.render_config(make_config(share_name="GAMES")))


class IncludeBlockTests(unittest.TestCase):
    ORIGINAL = "[global]\n   workgroup = HOME\n\n[printers]\n   path = /var/spool/samba\n"

    def test_adds_the_block_once(self):
        added = samba.add_include(self.ORIGINAL, "/etc/samba/ps2share.conf")
        self.assertTrue(samba.has_include(added))
        self.assertIn("include = /etc/samba/ps2share.conf", added)
        self.assertEqual(added.count(samba.BEGIN_MARKER), 1)

        again = samba.add_include(added, "/etc/samba/ps2share.conf")
        self.assertEqual(again.count(samba.BEGIN_MARKER), 1)

    def test_keeps_the_original_config_intact(self):
        added = samba.add_include(self.ORIGINAL, "/etc/samba/ps2share.conf")
        self.assertIn("workgroup = HOME", added)
        self.assertIn("[printers]", added)

    def test_removing_the_block_restores_the_original(self):
        added = samba.add_include(self.ORIGINAL, "/etc/samba/ps2share.conf")
        removed = samba.remove_include(added)
        self.assertFalse(samba.has_include(removed))
        self.assertEqual(removed.strip(), self.ORIGINAL.strip())

    def test_retargeting_replaces_rather_than_duplicates(self):
        added = samba.add_include(self.ORIGINAL, "/etc/samba/old.conf")
        moved = samba.add_include(added, "/etc/samba/new.conf")
        self.assertEqual(moved.count(samba.BEGIN_MARKER), 1)
        self.assertIn("new.conf", moved)
        self.assertNotIn("old.conf", moved)

    def test_remove_is_a_no_op_when_the_block_is_absent(self):
        self.assertEqual(samba.remove_include(self.ORIGINAL), self.ORIGINAL)


class UnknownParameterTests(unittest.TestCase):
    def test_picks_parameter_names_out_of_testparm_output(self):
        output = (
            'Unknown parameter encountered: "read raw"\n'
            'Ignoring unknown parameter "write raw"\n'
            "Loaded services file OK.\n"
        )
        self.assertEqual(samba.unknown_parameters(output), ["read raw", "write raw"])

    def test_no_findings_on_clean_output(self):
        self.assertEqual(samba.unknown_parameters("Loaded services file OK.\n"), [])

    def test_comments_out_rejected_parameters_only(self):
        text = "[global]\n   read raw = yes\n   ntlm auth = yes\n"
        stripped = samba.strip_parameters(text, ["read raw"])
        self.assertIn("#   (unsupported by this Samba build) read raw = yes", stripped)
        self.assertIn("   ntlm auth = yes", stripped)

    def test_matching_ignores_spacing_and_case(self):
        text = "[global]\n   Server   Min Protocol = NT1\n"
        stripped = samba.strip_parameters(text, ["server min protocol"])
        self.assertIn("(unsupported by this Samba build)", stripped)

    def test_nothing_to_strip_returns_the_input(self):
        text = "[global]\n   ntlm auth = yes\n"
        self.assertEqual(samba.strip_parameters(text, []), text)


class ForceUserTests(unittest.TestCase):
    def test_explicit_setting_wins(self):
        self.assertEqual(samba.resolve_force_user(make_config(force_user="media")), "media")

    def test_falls_back_to_nobody_for_an_unknown_path(self):
        config = make_config(force_user="", share_path="/definitely/not/here")
        self.assertEqual(samba.resolve_force_user(config), "nobody")

    def test_never_picks_root_from_a_root_owned_folder(self):
        # /etc is root-owned on every system this runs on.
        config = make_config(force_user="", share_path="/etc", access_mode="guest")
        self.assertEqual(samba.resolve_force_user(config), "nobody")
        self.assertNotIn("guest account = root", samba.render_config(config))


class ApplyGuardTests(unittest.TestCase):
    def test_refuses_without_a_folder(self):
        outcome = samba.apply(make_config(share_path=""))
        self.assertFalse(outcome.ok)
        self.assertIn("Pick a folder", outcome.message)

    def test_refuses_a_path_that_is_not_a_directory(self):
        outcome = samba.apply(make_config(share_path="/etc/hostname"))
        self.assertFalse(outcome.ok)
        self.assertIn("not a directory", outcome.message)

    def test_dry_run_writes_nothing_and_returns_the_text(self):
        target = "/etc/samba/ps2share.conf"
        before = None
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as handle:
                before = handle.read()

        outcome = samba.apply(make_config(share_path="/tmp"), dry_run=True)
        self.assertTrue(outcome.ok)
        self.assertIn("[PS2SMB]", outcome.config_text)

        if before is None:
            self.assertFalse(os.path.exists(target))
        else:
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)


if __name__ == "__main__":
    unittest.main()
