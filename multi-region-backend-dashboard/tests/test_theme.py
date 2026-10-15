"""The white theme rewrites the dark pages' colours as they are served (theme.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import theme  # noqa: E402


def luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class Theme(unittest.TestCase):
    def test_page_background_becomes_white(self):
        for dark in ("#08101b", "#0a0f18", "#0a1019"):
            self.assertEqual(theme.light(dark), "#ffffff")

    def test_text_becomes_dark_and_panels_light(self):
        self.assertLess(luminance(theme.light("#e7eef7")), 0.2)    # body text
        self.assertGreater(luminance(theme.light("#101c2d")), 0.9)  # panel
        self.assertLess(luminance(theme.light("#8b9db6")), 0.45)   # secondary text stays readable

    def test_accents_keep_their_hue_but_deepen(self):
        green = theme.light("#38d9a0")
        self.assertLess(luminance(green), luminance("#38d9a0"))
        r, g, b = (int(green[i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(g, r)
        self.assertGreater(g, b)

    def test_apply_rewrites_css_svg_and_rgba(self):
        out = theme.apply('body{background:#08101b;color:#e7eef7;color-scheme: dark}'
                          '<rect fill="#101c2d"/> x{background:rgba(20,35,57,.55);'
                          'box-shadow:0 0 4px rgba(0,0,0,.8);outline:1px solid rgba(255,255,255,.03)}')
        self.assertIn("background:#ffffff", out)
        self.assertNotIn("#e7eef7", out)
        self.assertIn("color-scheme:light", out)
        self.assertIn("rgba(229,236,246,.55)", out)
        self.assertIn("rgba(0,0,0,0.28)", out)
        self.assertIn("rgba(0,0,0,.03)", out)

    def test_white_text_on_buttons_stays_white_other_white_turns_dark(self):
        out = theme.apply('.tabs a.on{background:var(--accent);color:#fff} p{stroke:"#fff";background-color:#fff}')
        self.assertIn("color:#ffffff", out)          # selected tab label
        self.assertNotIn('stroke:"#ffffff"', out)    # cable highlight is visible on white
        self.assertNotIn("background-color:#ffffff", out)

    def test_switch_and_ufm_tiles_are_solid_on_both_fabric_pages(self):
        root = Path(__file__).resolve().parents[2]
        for page in ("ice2-netbox-dashboard", "lon14-netbox-dashboard"):
            html = theme.solid_tiles(theme.apply((root / page / "assets" / "dashboard.html").read_text()))
            self.assertIn("light-tiles", html, page)
            self.assertIn("__tint(", html, page)
            for old, _ in theme.SOLID_FILLS + theme.SOLID_RECOLOUR:
                self.assertNotIn(old, html, page)

    def test_solid_tiles_leaves_other_pages_alone(self):
        self.assertEqual(theme.solid_tiles("<html><head></head><body>x</body></html>"),
                         "<html><head></head><body>x</body></html>")

    def test_ids_and_non_colours_are_left_alone(self):
        text = 'href="#rail" id="#abc-1" cable #79002 color:#fff;'
        out = theme.apply(text)
        self.assertIn('href="#rail"', out)
        self.assertIn("#79002", out)


if __name__ == "__main__":
    unittest.main()
