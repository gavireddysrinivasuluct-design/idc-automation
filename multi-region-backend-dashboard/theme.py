"""Light (white) theme for every page the dashboard serves, applied as the page is served.

The pages are written dark: colours live in their stylesheets, inline styles, SVG attributes
and JavaScript templates alike. Rather than keep two copies, `apply()` rewrites every colour
literal (#rgb, #rrggbb, rgba(255,255,255,a)) by rule, keeping each colour's hue:

Colours written as rgb()/rgba() get the same mapping with their transparency kept; white
overlays become faint dark overlays and black shadows are softened.

* near-black page backgrounds  -> white
* dark panels, lines, tints    -> light greys and pastels
* light text                   -> dark text; grey secondary text -> darker grey
* bright accents / status      -> deeper shades that read on white

`--dark` on the service turns this off and serves the pages as written.
"""

from __future__ import annotations

import colorsys
import re
from functools import lru_cache

HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])")
RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*([\d.]+)\s*)?\)")
DARK_SCHEME = re.compile(r"color-scheme\s*:\s*dark")
# White as a text colour sits on a coloured button or badge (e.g. the selected tab): it stays
# white. White anywhere else (cable highlights, selection outlines) turns dark on a white page.
WHITE_TEXT = re.compile(r"(?<![-\w])color\s*:\s*#fff(?:fff)?(?![0-9a-zA-Z])", re.I)
KEEP_WHITE = "color:\x00white\x00"


def _rgb(text: str) -> tuple[float, float, float]:
    h = text[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


@lru_cache(maxsize=None)
def light(colour: str) -> str:
    """The light-theme counterpart of one dark-theme colour."""
    r, g, b = _rgb(colour)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    chroma = max(r, g, b) - min(r, g, b)  # how far from grey (HLS saturation overstates pale colours)
    if l < 0.085:                         # page background / near-black
        return "#ffffff"
    if l < 0.40:                          # panels, borders, dark tints -> near-white greys / pastels
        nl = 1 - (0.32 if chroma < 0.12 else 0.45) * l
        return _hex(*colorsys.hls_to_rgb(h, nl, min(s, 0.5)))
    if chroma < 0.20:                     # greys: light text -> dark text, muted stays muted
        nl = 0.15 if l > 0.85 else (1 - l if l > 0.72 else l * 0.62)
        return _hex(*colorsys.hls_to_rgb(h, nl, min(s, 0.25)))
    nl = max(0.32, min(l * 0.75, 0.45))   # saturated accents: deepen so they read on white
    return _hex(*colorsys.hls_to_rgb(h, nl, min(s, 0.78)))


def _rgb_sub(m: re.Match) -> str:
    r, g, b = (min(255, int(v)) for v in m.group(1, 2, 3))
    alpha = m.group(4)
    if (r, g, b) == (0, 0, 0):          # shadows: keep, but lighter on a white page
        return "rgba(0,0,0,%s)" % (round(float(alpha) * 0.35, 3) if alpha else "0.35")
    if (r, g, b) == (255, 255, 255) and alpha:  # white haze on dark -> faint dark haze on white
        return "rgba(0,0,0,%s)" % alpha
    new = light("#%02x%02x%02x" % (r, g, b))
    nr, ng, nb = (int(new[i:i + 2], 16) for i in (1, 3, 5))
    return "rgba(%d,%d,%d,%s)" % (nr, ng, nb, alpha) if alpha else "rgb(%d,%d,%d)" % (nr, ng, nb)


def apply(text: str) -> str:
    text = WHITE_TEXT.sub(KEEP_WHITE, text)
    text = HEX.sub(lambda m: light(m.group(0).lower()), text)
    text = RGB.sub(_rgb_sub, text)
    text = text.replace(KEEP_WHITE, "color:#ffffff")
    return DARK_SCHEME.sub("color-scheme:light", text)


# Switch and UFM tiles on the ICE2 / LON14 fabric drawing: an opaque light tint of the switch
# colour (not a see-through wash), full-colour border, dark / coloured labels as on the rest of
# the page. Both pages draw the tiles the same way.
SOLID_FILLS = (
    ("el('rect', {x, y, width:270, height:96, rx:10, fill:'url(#g-sp)'",              # UFM
     "el('rect', {x, y, width:270, height:96, rx:10, fill:__tint(SP)"),
    ("el('rect', {x, y:SY, width:SW, height:SH, rx:6, fill:'url(#g-sp)'",            # spines
     "el('rect', {x, y:SY, width:SW, height:SH, rx:6, fill:__tint(SP)"),
    ("el('rect', {x, y:LY, width:LW, height:LH, rx:5, fill:`url(#g-${r ? 'r' + r : 'lf'})`",  # leaves
     "el('rect', {x, y:LY, width:LW, height:LH, rx:5, fill:__tint(c)"),
)

# When cabling data arrives, a leaf is recoloured by the rail its GPU cables show (optional:
# only on pages that do this).
SOLID_RECOLOUR = (
    ("bx.setAttribute('fill', `url(#g-r${d.rail})`)", "bx.setAttribute('fill', __tint(c))"),
)

TINT = 0.16  # share of the switch colour in a tile's background; the rest is white

TINT_JS = ("<script id=\"light-tiles\">function __tint(c){var h=String(c).replace('#','');"
           "if(h.length===3)h=h.replace(/./g,'$&$&');if(!/^[0-9a-f]{6}$/i.test(h))return c;"
           "var o='#';for(var i=0;i<6;i+=2){var v=parseInt(h.substr(i,2),16);"
           "o+=('0'+Math.round(255-(255-v)*%s).toString(16)).slice(-2);}return o;}</script>" % TINT)


def solid_tiles(html: str) -> str:
    """For a page already passed through apply(): opaque light switch / UFM tiles."""
    if not all(old in html for old, _ in SOLID_FILLS):
        return html  # not the fabric drawing (or it changed): leave the page alone
    for old, new in SOLID_FILLS + SOLID_RECOLOUR:
        html = html.replace(old, new)
    # The spine's cable-count bar sits behind its name: keep it pale so the name reads; bold labels;
    # the spine-leaf mesh visible at rest.
    extra = TINT_JS + ("<style id=\"light-tiles-css\">.tile[aria-label^=\"spine\"] rect[fill-opacity]{fill-opacity:.3}"
                       ".tile .vt{font-weight:700}"
                       # 2,304 spine-leaf cables at 1.2% each glow on black but vanish on white
                       ".mesh:not(.on){opacity:.10}</style>")
    return html.replace("</head>", extra + "</head>", 1) if "</head>" in html else extra + html
