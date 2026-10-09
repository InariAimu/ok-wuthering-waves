import unittest

from src.echo.inventory import match_echo_name_tokens
from src.echo.name_catalog import (
    ECHO_NAMES_ZH_CN,
    EchoNameRecognitionError,
    match_echo_name,
)


class TestEchoNameCatalog(unittest.TestCase):
    def test_official_snapshot_is_unique_and_complete(self):
        self.assertEqual(len(ECHO_NAMES_ZH_CN), 199)
        self.assertEqual(len(set(ECHO_NAMES_ZH_CN)), 199)

    def test_normalizes_width_spacing_and_middle_dot(self):
        match = match_echo_name(" 霁息兽尊・首 ")

        self.assertEqual(match.canonical, "霁息兽尊·首")
        self.assertEqual(match.method, "exact")

    def test_unique_truncated_prefix_is_safe(self):
        match = match_echo_name("千傀重")

        self.assertEqual(match.canonical, "千傀重楼")
        self.assertEqual(match.method, "unique_prefix")

    def test_ambiguous_truncated_prefix_requires_retry(self):
        with self.assertRaises(EchoNameRecognitionError) as caught:
            match_echo_name("霁息兽尊·")

        self.assertEqual(caught.exception.reason, "ambiguous prefix")
        self.assertEqual(
            set(caught.exception.alternatives),
            {"霁息兽尊·首", "霁息兽尊·身"},
        )

    def test_high_confidence_ocr_typo_is_corrected(self):
        match = match_echo_name("不熄猎于")

        self.assertEqual(match.canonical, "不熄猎手")
        self.assertEqual(match.method, "fuzzy")
        self.assertGreaterEqual(match.similarity, 0.75)

    def test_unrelated_name_is_rejected(self):
        with self.assertRaises(EchoNameRecognitionError):
            match_echo_name("完全错误名称")

    def test_token_parser_removes_merged_level_before_matching(self):
        match = match_echo_name_tokens((
            {"text": "共鸣回响·芙露德莉斯+25", "x": 10, "y": 10},
        ))

        self.assertEqual(match.canonical, "共鸣回响·芙露德莉斯")


if __name__ == "__main__":
    unittest.main()
