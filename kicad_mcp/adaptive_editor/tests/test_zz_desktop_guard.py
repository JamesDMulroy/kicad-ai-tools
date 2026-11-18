"""Runs last (module order): the whole-tree owner-desktop guard started by this package saw no visible
window from any process of this run and was never blind."""
import os
import unittest

from tests import GUARD


@unittest.skipIf(os.name != "nt", "the owner-desktop guard is Windows-only")
class DesktopGuardTests(unittest.TestCase):
    def test_no_window_reached_the_owner_desktop_and_the_guard_never_went_blind(self):
        record = GUARD.record()
        print(f"\nunittest desktop guard: {record}")
        self.assertGreater(record["samples"], 0)
        self.assertEqual(record["unavailable_samples"], 0, record)
        self.assertEqual(record["hits"], [], record)
