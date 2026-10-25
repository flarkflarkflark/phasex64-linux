#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 flarkflarkflark
"""i18n completeness tests for the PHASE X64 panel.

Two checks, both independent of any external PO-parsing library (none
required by this project otherwise, so this test parses the simple .po/
.pot format itself rather than add a dependency for it):

1. The committed po/phasex64-panel.pot actually matches what xgettext
   extracts from the current phasex64-panel source - so a new _() string
   never silently goes untemplated.
2. po/nl.po has a non-empty msgstr for every msgid in the .pot - so
   nothing is silently left untranslated.

Requires `xgettext` on PATH for check 1 (same requirement as po/build.sh
and the PKGBUILD); skips that one check if it's missing rather than
failing the whole file.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
POT = HERE / "po" / "phasex64-panel.pot"
NL_PO = HERE / "po" / "nl.po"
SCRIPT = HERE / "phasex64-panel"

QUOTED = re.compile(r'^"(.*)"\s*$')


def po_decode(raw):
    """Undo PO string escaping (\\n \\t \\" \\\\) - the only escapes our
    own po/pot files use; everything else (e.g. the literal "≥"/"·"/"−∞"
    characters in some msgids) is already plain UTF-8 in the file, not
    escaped, so this must NOT use a blanket codec like 'unicode_escape'
    (which assumes latin-1 input and would mangle those)."""
    out, i = [], 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse_po(path):
    """{msgid: msgstr} - minimal parser for our own simple po/pot files
    (no msgctxt, no plural forms)."""
    entries = {}
    msgid_parts = msgstr_parts = None
    mode = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("msgid "):
            if msgid_parts is not None:
                entries[po_decode("".join(msgid_parts))] = po_decode("".join(msgstr_parts or []))
            msgid_parts, msgstr_parts, mode = [QUOTED.match(line[6:]).group(1)], None, "msgid"
        elif line.startswith("msgstr "):
            msgstr_parts, mode = [QUOTED.match(line[7:]).group(1)], "msgstr"
        else:
            m = QUOTED.match(line)
            if m and mode == "msgid":
                msgid_parts.append(m.group(1))
            elif m and mode == "msgstr":
                msgstr_parts.append(m.group(1))
    if msgid_parts is not None:
        entries[po_decode("".join(msgid_parts))] = po_decode("".join(msgstr_parts or []))
    return entries


@unittest.skipUnless(shutil.which("xgettext"), "requires the gettext tools (xgettext)")
class PotUpToDateTest(unittest.TestCase):
    def test_pot_matches_current_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh.pot"
            subprocess.run(
                ["xgettext", "--language=Python", "--from-code=UTF-8", "--keyword=_",
                 "--package-name=phasex64-panel", "--package-version=0.9.2",
                 "-o", str(fresh), str(SCRIPT)],
                check=True, capture_output=True)
            fresh_ids = set(parse_po(fresh)) - {""}
        committed_ids = set(parse_po(POT)) - {""}
        missing = fresh_ids - committed_ids
        self.assertEqual(missing, set(),
                          "source has _() strings not in po/phasex64-panel.pot - "
                          "regenerate it (see po/README.md)")


class TranslationCompletenessTest(unittest.TestCase):
    def test_nl_po_translates_every_pot_entry(self):
        pot_ids = set(parse_po(POT)) - {""}
        nl = parse_po(NL_PO)
        untranslated = sorted(mid for mid in pot_ids if not nl.get(mid, "").strip())
        self.assertEqual(untranslated, [],
                          f"{len(untranslated)} string(s) in po/nl.po have no translation")

    def test_nl_po_has_no_stale_entries(self):
        # Not a correctness bug by itself (msgmerge leaves these on
        # purpose while the template is in flux), but worth catching
        # rather than letting po/nl.po silently accumulate dead entries.
        pot_ids = set(parse_po(POT)) - {""}
        nl_ids = set(parse_po(NL_PO)) - {""}
        stale = nl_ids - pot_ids
        self.assertEqual(stale, set(),
                          f"po/nl.po has {len(stale)} msgid(s) no longer in the .pot - "
                          "run msgmerge (see po/README.md)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
