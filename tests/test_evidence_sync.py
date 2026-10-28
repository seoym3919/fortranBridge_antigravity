"""The evidence module must be the same in every plugin that ships it.

scripts/verification_evidence.py is one file copied by hand into scienceStack, cppBridge, fortranBridge and theoryExtractor
(each plugin installs on its own, so it cannot import from another). Two checks keep the copies from drifting; this test
file is identical in all four plugins.

1. The module's SHA-256 must equal PINNED_SHA256 below. Editing the module in one plugin therefore fails that plugin's
   tests until the pin is updated, and the pin is the same constant in all four test files.
2. When the sibling plugin directories sit next to this one (the development layout), their copies must be
   byte-identical to this one. In a standalone checkout (CI) the siblings are absent and this check is skipped.

To change the module on purpose: edit it in one plugin, copy it to the other three, set PINNED_SHA256 to the new value
printed by `sha256sum scripts/verification_evidence.py` in all four test files, and run all four suites.
"""

import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "scripts" / "verification_evidence.py"
PINNED_SHA256 = "51b812b3c7958162d7edfff235b8aa5d320f8cd141c5f79b480f8207c23356c3"
PLUGIN_DIRS = ("scienceStack_antigravity", "cppBridge_antigravity", "fortranBridge_antigravity", "theoryExtractor_antigravity")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class EvidenceModuleSync(unittest.TestCase):
    def test_module_matches_the_pinned_hash(self):
        self.assertEqual(sha256(MODULE), PINNED_SHA256,
                         "verification_evidence.py changed. If that was intended, copy it to the other three plugins and update "
                         "PINNED_SHA256 in all four tests/test_evidence_sync.py (see this file's docstring).")

    def test_sibling_plugins_hold_the_same_bytes(self):
        siblings = [ROOT.parent / name / "scripts" / "verification_evidence.py" for name in PLUGIN_DIRS
                    if (ROOT.parent / name).resolve() != ROOT]
        present = [path for path in siblings if path.is_file()]
        if not present:
            self.skipTest("no sibling plugin directories next to this one (standalone checkout)")
        for path in present:
            with self.subTest(sibling=path.parents[1].name):
                self.assertEqual(sha256(path), sha256(MODULE), f"{path.parents[1].name} has a different verification_evidence.py")


if __name__ == "__main__":
    unittest.main()
