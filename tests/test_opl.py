"""Tests for the OPL layout, image scanning and ISO9660 parsing."""

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps2share import opl  # noqa: E402

SECTOR = 2048


def _dir_record(name: bytes, extent: int, size: int, is_dir: bool) -> bytes:
    """One ISO9660 directory record."""
    record = bytearray(33 + len(name))
    record[1] = 0
    struct.pack_into("<I", record, 2, extent)
    struct.pack_into(">I", record, 6, extent)
    struct.pack_into("<I", record, 10, size)
    struct.pack_into(">I", record, 14, size)
    record[25] = 2 if is_dir else 0
    struct.pack_into("<H", record, 28, 1)
    struct.pack_into(">H", record, 30, 1)
    record[32] = len(name)
    record[33:] = name
    if len(record) % 2:
        record.append(0)
    record[0] = len(record)
    return bytes(record)


def build_iso(path: str, game_id: str = "SLUS_216.08", label: str = "GRANTURISMO4") -> None:
    """A minimal but structurally valid ISO9660 image holding SYSTEM.CNF."""
    system_cnf = (
        f"BOOT2 = cdrom0:\\{game_id};1\r\n"
        "VER = 1.00\r\n"
        "VMODE = NTSC\r\n"
    ).encode("ascii")

    root_lba, cnf_lba = 18, 19

    root = bytearray()
    root += _dir_record(b"\x00", root_lba, SECTOR, True)
    root += _dir_record(b"\x01", root_lba, SECTOR, True)
    root += _dir_record(b"SYSTEM.CNF;1", cnf_lba, len(system_cnf), False)
    root += bytes(SECTOR - len(root))

    pvd = bytearray(SECTOR)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = label.ljust(32).encode("ascii")[:32]
    pvd[156:190] = _dir_record(b"\x00", root_lba, SECTOR, True)[:34]

    terminator = bytearray(SECTOR)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1

    with open(path, "wb") as handle:
        handle.write(bytes(16 * SECTOR))
        handle.write(bytes(pvd))
        handle.write(bytes(terminator))
        handle.write(bytes(root))
        handle.write(system_cnf.ljust(SECTOR, b"\x00"))


class IsoParsingTests(unittest.TestCase):
    def test_reads_game_id_and_volume_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = os.path.join(tmp, "game.iso")
            build_iso(iso)
            game_id, volume = opl.read_iso_metadata(iso)
            self.assertEqual(game_id, "SLUS_216.08")
            self.assertEqual(volume, "GRANTURISMO4")

    def test_handles_a_file_that_is_not_an_iso(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "not-an-image.iso")
            with open(junk, "wb") as handle:
                handle.write(b"\x00" * (20 * SECTOR))
            self.assertEqual(opl.read_iso_metadata(junk), ("", ""))

    def test_handles_a_missing_file(self):
        self.assertEqual(opl.read_iso_metadata("/nonexistent/x.iso"), ("", ""))

    def test_normalises_several_boot_line_spellings(self):
        self.assertEqual(opl._normalise_game_id("SLUS_216.08"), "SLUS_216.08")
        self.assertEqual(opl._normalise_game_id("\\SCES_12345"), "SCES_123.45")
        self.assertEqual(opl._normalise_game_id("rubbish"), "")


class LayoutTests(unittest.TestCase):
    def test_creates_every_opl_folder_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = opl.ensure_layout(tmp)
            self.assertEqual(sorted(created), sorted(opl.OPL_FOLDERS))
            for name in opl.OPL_FOLDERS:
                self.assertTrue(os.path.isdir(os.path.join(tmp, name)))
            self.assertEqual(opl.ensure_layout(tmp), [])

    def test_health_reports_missing_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            health = opl.share_health(tmp)
            self.assertFalse(health["ok"])
            self.assertTrue(any("Missing OPL folders" in p for p in health["problems"]))
            opl.ensure_layout(tmp)
            self.assertTrue(opl.share_health(tmp)["ok"])

    def test_health_without_a_folder(self):
        health = opl.share_health("")
        self.assertFalse(health["ok"])


class AccessCheckTests(unittest.TestCase):
    """The 'connects but lists nothing' trap: an un-traversable parent."""

    def test_no_problems_for_a_world_traversable_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            share = os.path.join(tmp, "games")
            os.makedirs(share)
            os.chmod(share, 0o777)  # explicit: makedirs' mode is masked by umask
            self.assertEqual(opl.access_problems(share, "nobody"), [])

    def test_spots_a_parent_that_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = os.path.join(tmp, "locked")
            share = os.path.join(locked, "games")
            os.makedirs(share)
            os.chmod(share, 0o777)
            os.chmod(tmp, 0o755)
            os.chmod(locked, 0o700)
            problems = opl.access_problems(share, "nobody")
            self.assertEqual(len(problems), 1)
            self.assertIn("cannot pass through", problems[0])
            self.assertIn("chmod o+x", problems[0])
            self.assertIn(locked, problems[0])

    def test_spots_a_share_the_account_cannot_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            share = os.path.join(tmp, "games")
            os.makedirs(share, mode=0o755)
            self.assertEqual(opl.access_problems(share, "nobody", writable=False), [])
            problems = opl.access_problems(share, "nobody", writable=True)
            self.assertEqual(len(problems), 1)
            self.assertIn("cannot write", problems[0])

    def test_unknown_account_is_reported(self):
        problems = opl.access_problems("/tmp", "definitely-not-a-user")
        self.assertIn("no Linux account", problems[0])

    def test_root_can_always_reach_everything(self):
        self.assertEqual(opl.access_problems("/root", "root"), [])

    def test_nothing_to_check_without_a_path_or_account(self):
        self.assertEqual(opl.access_problems("", "nobody"), [])
        self.assertEqual(opl.access_problems("/tmp", ""), [])

    def test_health_surfaces_the_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            locked = os.path.join(tmp, "locked")
            share = os.path.join(locked, "games")
            os.makedirs(share)
            os.chmod(share, 0o777)
            os.chmod(tmp, 0o755)
            os.chmod(locked, 0o700)
            opl.ensure_layout(share)
            health = opl.share_health(share, serve_as="nobody")
            self.assertFalse(health["ok"])
            self.assertTrue(any("cannot pass through" in p for p in health["problems"]))


class NameWarningTests(unittest.TestCase):
    def test_flags_long_names(self):
        warnings = opl.name_warnings("A" * 40 + ".iso")
        self.assertTrue(any("40 characters" in w for w in warnings))

    def test_accepts_an_ordinary_name(self):
        self.assertEqual(opl.name_warnings("Gran Turismo 4.iso"), [])

    def test_flags_awkward_characters(self):
        warnings = opl.name_warnings("Okami ✨.iso")
        self.assertTrue(any("characters OPL may not render" in w for w in warnings))


class ScannerTests(unittest.TestCase):
    def test_finds_images_and_groups_split_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            opl.ensure_layout(tmp)
            build_iso(os.path.join(tmp, "DVD", "Gran Turismo 4.iso"))
            for part in ("00", "01", "02"):
                with open(os.path.join(tmp, "CD", f"Ico.iso.{part}"), "wb") as handle:
                    handle.write(b"\x00" * 1024)

            scanner = opl.GameScanner(os.path.join(tmp, "cache.json"))
            games = {game.name: game for game in scanner.scan(tmp)}

            self.assertIn("Gran Turismo 4.iso", games)
            self.assertEqual(games["Gran Turismo 4.iso"].game_id, "SLUS_216.08")
            self.assertEqual(games["Gran Turismo 4.iso"].folder, "DVD")

            self.assertIn("Ico.iso", games)
            self.assertEqual(games["Ico.iso"].kind, "split")
            self.assertEqual(games["Ico.iso"].parts, 3)
            self.assertEqual(games["Ico.iso"].size, 3072)

    def test_cache_avoids_a_second_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            opl.ensure_layout(tmp)
            iso = os.path.join(tmp, "DVD", "game.iso")
            build_iso(iso)
            cache = os.path.join(tmp, "cache.json")

            scanner = opl.GameScanner(cache)
            self.assertEqual(scanner.scan(tmp)[0].game_id, "SLUS_216.08")
            self.assertTrue(os.path.isfile(cache))

            # Corrupt the image: a cached scan must not go back to the disk.
            with open(iso, "r+b") as handle:
                handle.seek(16 * SECTOR)
                handle.write(b"\x00" * 16)
            self.assertEqual(opl.GameScanner(cache).scan(tmp)[0].game_id, "SLUS_216.08")

    def test_ignores_files_that_are_not_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            opl.ensure_layout(tmp)
            with open(os.path.join(tmp, "DVD", "readme.txt"), "w", encoding="utf-8") as handle:
                handle.write("hello")
            self.assertEqual(opl.GameScanner().scan(tmp), [])


class MemoryCardTests(unittest.TestCase):
    def test_lists_virtual_memory_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            opl.ensure_layout(tmp)
            with open(os.path.join(tmp, "VMC", "slot1.bin"), "wb") as handle:
                handle.write(b"\x00" * (8 * 1024 * 1024))
            cards = opl.list_memory_cards(tmp)
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["name"], "slot1.bin")
            self.assertEqual(cards[0]["slots"], 1.0)


if __name__ == "__main__":
    unittest.main()
