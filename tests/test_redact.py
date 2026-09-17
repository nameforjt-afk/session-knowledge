from __future__ import annotations

import unittest

from sessionmcp.redact import (
    REDACTION_PREFIX,
    classify_key,
    extract_assignments,
    redact,
    scan_for_leaks,
)


class RedactionTests(unittest.TestCase):
    def test_secret_assignment_is_removed_from_text(self) -> None:
        original = "OPENAI_API_KEY=prod-secret-value-987654321"

        clean, findings = redact(original)

        self.assertNotIn("prod-secret-value-987654321", clean)
        self.assertIn(REDACTION_PREFIX, clean)
        self.assertEqual(["OPENAI_API_KEY"], [finding.label for finding in findings])

    def test_placeholder_marker_inside_real_secret_does_not_bypass_redaction(self) -> None:
        for marker in ("foo", "bar", "todo"):
            with self.subTest(marker=marker):
                secret = f"prod-{marker}-secret-123456"

                clean, findings = redact(f"API_KEY={secret}")

                self.assertNotIn(secret, clean)
                self.assertEqual(["API_KEY"], [finding.label for finding in findings])

    def test_common_literal_secrets_are_removed_without_variable_names(self) -> None:
        original = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"

        clean, findings = redact(original)

        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", clean)
        self.assertEqual("bearer", findings[0].label)

    def test_redaction_is_idempotent(self) -> None:
        once, _ = redact("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890")

        twice, findings = redact(once)

        self.assertEqual(once, twice)
        self.assertEqual([], findings)

    def test_identifiers_remain_searchable(self) -> None:
        original = "TABLE_ID=tbl_abcdefghijklmnop"

        clean, findings = redact(original)

        self.assertEqual(original, clean)
        self.assertEqual([], findings)
        self.assertEqual("identifier", classify_key("TABLE_ID"))

    def test_extract_assignments_supports_shell_and_json(self) -> None:
        text = (
            'export DISCORD_TOKEN="discord-secret-value-123456"\n'
            '{"client_id": "client-identifier-987654"}'
        )

        assignments = extract_assignments(text)

        by_name = {assignment.key_name: assignment for assignment in assignments}
        self.assertEqual({"DISCORD_TOKEN", "client_id"}, set(by_name))
        self.assertEqual("secret", by_name["DISCORD_TOKEN"].kind)
        self.assertEqual("identifier", by_name["client_id"].kind)

    def test_scan_for_leaks_accepts_redacted_output(self) -> None:
        clean, _ = redact("API_KEY=prod-secret-value-987654321")

        self.assertEqual([], scan_for_leaks(clean))


if __name__ == "__main__":
    unittest.main()
