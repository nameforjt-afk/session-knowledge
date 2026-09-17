from __future__ import annotations

import unittest

from sessionmcp.tokenize import build_query, snippet_around, tokenize


class TokenizationTests(unittest.TestCase):
    def test_cjk_text_expands_to_adjacent_bigrams(self) -> None:
        self.assertEqual("部署 署流 流程", tokenize("部署流程"))

    def test_latin_tokens_are_normalized_without_losing_separators(self) -> None:
        self.assertEqual(
            "database_url api.example.com",
            tokenize("DATABASE_URL api.example.com"),
        )

    def test_single_cjk_character_uses_like_fallback(self) -> None:
        query = build_query("卡")

        self.assertEqual("", query.match)
        self.assertEqual(("卡",), query.like_terms)

    def test_multiple_query_words_use_and_semantics(self) -> None:
        query = build_query("部署 timeout")

        self.assertEqual('"部署" "timeout"', query.match)
        self.assertEqual((), query.like_terms)

    def test_snippet_centers_the_first_matching_term(self) -> None:
        text = "prefix " * 40 + "needle" + " suffix" * 40

        snippet = snippet_around(text, ["needle"], width=80)

        self.assertIn("needle", snippet)
        self.assertTrue(snippet.startswith("…"))
        self.assertTrue(snippet.endswith("…"))


if __name__ == "__main__":
    unittest.main()
