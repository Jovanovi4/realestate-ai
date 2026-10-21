"""Contrast checks for the graphite workspace and its interactive states.

These test colour pairs from the shipped CSS, not rendered page geometry.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from .landing_colors import contrast


class DarkPaletteTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = (Path(settings.BASE_DIR) / "static/css/rieltor-ui.css").read_text()
        cls.palette = {}
        cls.palettes = {}
        for mode, selector in (("light", ":root"), ("dark", ':root[data-bs-theme="dark"]')):
            block = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", cls.css).group(1)
            cls.palette.update(re.findall(r"(--[\w-]+):\s*([^;]+);", block))
            cls.palettes[mode] = dict(cls.palette)

    def color(self, name):
        value = self.palette[name].strip()
        visited = {name}
        while value.startswith("var("):
            name = value[4:-1]
            self.assertNotIn(name, visited, "Circular palette reference")
            visited.add(name)
            value = self.palette[name].strip()
        self.assertRegex(value, r"^#[0-9a-fA-F]{6}$")
        return value

    def assert_contrast(self, foreground, background, minimum):
        with self.subTest(foreground=foreground, background=background):
            self.assertGreaterEqual(contrast(self.color(foreground), self.color(background)), minimum)

    def test_body_labels_and_placeholders_on_their_surfaces(self):
        for surface in ("--ui-canvas", "--ui-surface", "--ui-surface-subtle", "--ui-field-bg"):
            for text in ("--ui-ink", "--ui-muted", "--ui-muted-strong"):
                self.assert_contrast(text, surface, 4.5)
        self.assert_contrast("--ui-field-placeholder", "--ui-field-bg", 4.5)

    def test_selected_rows_and_filter_counts_keep_small_text_readable(self):
        for text in ("--ui-ink", "--ui-muted-strong", "--ui-muted-on-selected", "--ui-accent"):
            for surface in ("--ui-surface-selected", "--ui-surface-hover", "--ui-accent-soft"):
                self.assert_contrast(text, surface, 4.5)

    def test_buttons_remain_readable_when_hovered_or_pressed(self):
        for background in ("--ui-primary-fill", "--ui-primary-fill-hover"):
            self.assert_contrast("--ui-on-accent", background, 4.5)
        for background in ("--ui-danger", "--ui-danger-hover"):
            self.assert_contrast("--ui-on-danger", background, 4.5)
        for background in ("--ui-surface", "--ui-surface-selected", "--ui-neutral-active", "--ui-outline-active"):
            self.assert_contrast("--ui-muted-strong", background, 4.5)
        self.assert_contrast("--ui-danger-hover", "--ui-danger-active-soft", 4.5)

    def test_status_labels_and_overdue_text_keep_their_semantic_colours(self):
        for kind in ("danger", "success", "warning", "info"):
            for background in (f"--ui-{kind}-soft", "--ui-surface", "--ui-surface-selected"):
                self.assert_contrast(f"--ui-{kind}", background, 4.5)

    def test_control_boundaries_and_focus_against_adjacent_surfaces(self):
        # Card separators are decorative; input and selection boundaries are not.
        for boundary in ("--ui-border-strong", "--ui-outline-border", "--ui-choice-active-border", "--ui-focus-ring"):
            for surface in ("--ui-canvas", "--ui-surface", "--ui-field-bg", "--ui-surface-selected"):
                self.assert_contrast(boundary, surface, 3)
        self.assert_contrast("--ui-field-error-border", "--ui-field-bg", 3)

    def test_check_marks_and_partial_selection_on_the_checkbox_fill(self):
        for surface in ("--ui-field-bg", "--ui-surface", "--ui-surface-selected"):
            self.assert_contrast("--ui-control-active", surface, 3)
        rules = re.findall(r'\[data-bs-theme="dark"\] \.form-check-input[^}]+}', self.css)
        marks = [color for rule in rules for color in re.findall(r"(?:stroke|fill)='%23([0-9a-fA-F]{6})'", rule)]
        self.assertEqual(len(marks), 3)  # check, partial selection, radio dot
        for mark in marks:
            self.assertGreaterEqual(contrast("#" + mark, self.color("--ui-control-active")), 3)

    def test_bootstrap_rgb_utilities_match_component_colours_in_both_themes(self):
        # Bootstrap utilities use RGB channels, while components use colour tokens.
        # A stale RGB value can leave links blue or body backgrounds light.
        roles = (
            "body-color", "body-bg", "emphasis-color", "secondary-color", "secondary-bg",
            "tertiary-color", "tertiary-bg", "primary", "secondary", "danger", "success",
            "warning", "info", "link-color", "link-hover-color",
        )
        for mode, palette in self.palettes.items():
            self.palette = palette
            for role in roles:
                with self.subTest(mode=mode, role=role):
                    value = self.color(f"--bs-{role}")[1:]
                    expected = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
                    channels = tuple(int(channel.strip()) for channel in palette[f"--bs-{role}-rgb"].split(","))
                    self.assertEqual(channels, expected)

    def test_bootstrap_emphasis_text_and_subtle_backgrounds_keep_status_contrast(self):
        for role in ("primary", "secondary", "danger", "success", "warning", "info"):
            self.assert_contrast(f"--bs-{role}-text-emphasis", f"--bs-{role}-bg-subtle", 4.5)

    def test_all_deal_stage_badges_have_readable_text_in_both_themes(self):
        for mode, palette in self.palettes.items():
            self.palette = palette
            for role in ("info", "warning", "success", "danger", "stage-viewing", "stage-negotiation", "stage-reserved"):
                with self.subTest(mode=mode, stage=role):
                    self.assert_contrast("--ui-on-status", f"--ui-{role}", 4.5)

    def test_sidebar_and_gallery_controls_on_dark_surfaces(self):
        for text in ("--ui-sidebar-text", "--ui-sidebar-muted"):
            for background in ("--ui-sidebar-bg", "--ui-sidebar-hover"):
                self.assert_contrast(text, background, 4.5)
        self.assert_contrast("--ui-muted-on-selected", "--ui-sidebar-active", 4.5)
        for text in ("--ui-muted-strong", "--ui-danger", "--ui-danger-hover"):
            self.assert_contrast(text, "--ui-photo-control-bg", 4.5)
        self.assert_contrast("--ui-photo-control-border", "--ui-photo-control-bg", 3)

    def test_switch_thumb_uses_a_circle_instead_of_checkbox_tick(self):
        for state, background in (("", "--ui-field-bg"), (":checked", "--ui-control-active")):
            selector = f'[data-bs-theme="dark"] .form-switch .form-check-input{state}'
            rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", self.css).group(1)
            self.assertIn('%3ccircle', rule)
            mark = re.search(r"fill='%23([0-9a-fA-F]{6})'", rule).group(1)
            self.assertGreaterEqual(contrast("#" + mark, self.color(background)), 3)
