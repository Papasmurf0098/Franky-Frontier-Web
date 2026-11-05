"""Rules the dashboard's design must keep. The reasons are in AGENT.md."""

import re
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "site" / "index.html").read_text(encoding="utf-8")


def token(name):
    match = re.search(r"--%s:\s*(#[0-9a-fA-F]{6}|rgba\([^)]*\))" % re.escape(name), HTML)
    if not match:
        raise AssertionError("missing design token --%s" % name)
    return match.group(1)


def parse(color):
    if color.startswith("#"):
        return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5)), 1.0
    parts = [float(part) for part in color[5:-1].split(",")]
    return tuple(parts[:3]), parts[3]


def over(color, backdrop):
    rgb, alpha = parse(color)
    return tuple(alpha * rgb[i] + (1 - alpha) * backdrop[i] for i in range(3))


def luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# The brightest the page background gets: where the violet and turquoise glows overlap.
BRIGHTEST_BACKDROP = (70, 75, 125)


class Contrast(unittest.TestCase):
    """Glass changes with what's behind it, so text is checked against the worst case."""

    def test_text_is_readable_on_every_glass_surface(self):
        for surface in ("glass", "glass-card", "glass-bar"):
            behind = over(token(surface), BRIGHTEST_BACKDROP)
            for text in ("text", "text-soft", "text-faint", "turquoise-400", "violet-300", "amber", "coral"):
                ratio = contrast(parse(token(text))[0], behind)
                self.assertGreaterEqual(ratio, 4.5, "--%s on --%s is only %.2f:1" % (text, surface, ratio))

    def test_ink_is_readable_on_turquoise_buttons_and_labels(self):
        ratio = contrast(parse(token("ink-on-turquoise"))[0], parse(token("turquoise-400"))[0])
        self.assertGreaterEqual(ratio, 4.5)


class Banner(unittest.TestCase):
    def test_has_a_visible_pause_control(self):
        # WCAG 2.2.2: moving content needs a way to pause it. Hover alone doesn't count.
        self.assertIn('id="tickerToggle"', HTML)
        self.assertIn('aria-label="Pause headlines"', HTML)

    def test_respects_reduced_motion(self):
        block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n    \}", HTML, re.S).group(1)
        self.assertIn(".ticker-track", block)
        self.assertIn("animation: none", block)

    def test_copies_are_hidden_from_screen_readers_and_the_tab_key(self):
        self.assertIn('copy.setAttribute("aria-hidden", "true")', HTML)
        self.assertIn("link.tabIndex = -1", HTML)

    def test_keyboard_users_can_skip_the_banner(self):
        self.assertLess(HTML.index('class="skip-link"'), HTML.index('id="ticker"'))

    def test_does_not_claim_to_be_live(self):
        # Sources are checked every 12 or 24 hours, so "live" would be untrue.
        self.assertNotIn(">Live<", HTML)


class OnDevice(unittest.TestCase):
    def test_loads_nothing_from_outside(self):
        for banned in ("<link", "<img", "@import", "fonts.googleapis", "cdn.", "<script src"):
            self.assertNotIn(banned, HTML)

    def test_glass_has_fallbacks(self):
        self.assertIn("prefers-reduced-transparency", HTML)
        self.assertIn("@supports not", HTML)
        self.assertIn("-webkit-backdrop-filter", HTML)

    def test_blur_is_not_used_on_list_cards(self):
        card = re.search(r"\n    \.card \{(.*?)\}", HTML, re.S).group(1)
        self.assertNotIn("backdrop-filter", card)


class Dates(unittest.TestCase):
    def test_date_only_values_are_read_as_local_dates(self):
        # new Date("2026-11-03") is UTC midnight, which shows as Nov 2 in the Americas.
        self.assertNotIn("new Date(item.date)", HTML)
        self.assertIn("function parseDay", HTML)


if __name__ == "__main__":
    unittest.main()
