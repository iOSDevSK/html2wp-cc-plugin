#!/usr/bin/env python3
"""Transparent artwork survives WebP optimization and responsive variants."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from PIL import Image

HERE = Path(__file__).resolve().parent

class Alpha(unittest.TestCase):
    def test_both_image_stages_preserve_all_png_transparency_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('rgba', 'la', 'palette', 'rgb_key'):
                im = Image.new('RGBA', (800, 400), (255, 255, 255, 0))
                im.paste((255, 255, 255, 255), (200, 100, 600, 300))
                if name == 'la':
                    im = im.convert('LA')
                elif name == 'palette':
                    im = Image.new('P', (800, 400), 0)
                    im.putpalette([0,0,0,255,255,255] + [0]*762)
                    im.paste(1, (200, 100, 600, 300))
                    im.info['transparency'] = 0
                elif name == 'rgb_key':
                    im = Image.new('RGB', (800, 400), (0,0,0))
                    im.paste((255,255,255), (200,100,600,300))
                    im.info['transparency'] = (0,0,0)
                im.save(root / (name + '.png'), compress_level=0)
            original = '<html><head><style>body{background:#293023}img{width:120px;height:auto}</style></head><body>'
            original += ''.join('<img src="' + n + '.png">' for n in ('rgba','la','palette','rgb_key')) + '</body></html>'
            (root/'index.html').write_text(original)
            # Direct responsive creation must preserve alpha even before image optimization.
            for stage in ('markup', 'images'):
                if stage == 'markup':
                    cmd = [sys.executable, str(HERE/'optimize-markup.py'), '--input', str(root), '--responsive', '--apply', '--out', str(root/'markup-report.json')]
                else:
                    # Restore page refs; remove generated files so image optimizer owns its outputs.
                    for f in root.glob('*-*.png'):f.unlink()
                    (root/'index.html').write_text(original)
                    cmd = [sys.executable, str(HERE/'optimize-images.py'), '--input', str(root), '--min-bytes', '0', '--apply', '--out', str(root/'images-report.json')]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name in ('rgba', 'la', 'palette', 'rgb_key'):
                    path = root/(name + ('-240.png' if stage == 'markup' else '.webp'))
                    self.assertTrue(path.is_file(), (stage,name,result.stdout))
                    with Image.open(path) as image:
                        self.assertIn('A', image.getbands(), (stage,name))
                        alpha = image.getchannel('A')
                        self.assertEqual(alpha.getpixel((0,0)), 0, (stage,name))
                        self.assertEqual(alpha.getpixel((image.width//2,image.height//2)), 255, (stage,name))

if __name__ == '__main__':
    unittest.main()
