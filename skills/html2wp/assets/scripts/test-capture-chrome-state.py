#!/usr/bin/env python3
"""capture-chrome.py keeps a reveal the runtime already played out of the
captured chrome: spa-in / spa-done are the runtime's state, and baked into the
header part they would stop it from ever revealing again.

  python3 test-capture-chrome-state.py
"""
import re
import unittest
from pathlib import Path

SRC = Path(__file__).with_name('capture-chrome.py').read_text()
ns = {'re': re}
exec(SRC[SRC.index('REVEAL_STATE ='):SRC.index('written = []')], ns)
strip = ns['strip_reveal_state']


class RevealStateTest(unittest.TestCase):
    def test_state_classes_go_design_classes_stay(self):
        self.assertEqual(strip('<header class="sticky top-0 spa-in spa-done" data-spa-reveal="r0"><a class=\'spa-in\'>x</a></header>'),
                         '<header class="sticky top-0" data-spa-reveal="r0"><a class=\'\'>x</a></header>')

    def test_lookalikes_and_other_markup_untouched(self):
        html = '<b class="spa-inx">y</b><p data-note="spa-in">z</p><i class="a  b">w</i>'
        self.assertEqual(strip(html), html)


if __name__ == '__main__':
    unittest.main()
