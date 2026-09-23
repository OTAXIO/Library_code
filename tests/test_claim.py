import unittest

from claim import sa_claim_source
from core import SafetyStop


class ClaimSourceTests(unittest.TestCase):
    def field(self, value):
        return [{"label": "认领状态", "sa": value, "library": "未认领"}]

    def test_preserves_leading_zeros_and_long_id(self):
        for identifier in ("00001", "123456789012345678901234567890"):
            self.assertEqual(sa_claim_source(self.field(f"测试员({identifier})①"))[1], identifier)

    def test_fullwidth_parentheses(self):
        self.assertEqual(sa_claim_source(self.field("测试员（ 00001 ）")), ("测试员（ 00001 ）", "00001"))

    def test_refuses_missing_or_ambiguous_fields(self):
        for value in (None, [], self.field("A(1)") * 2, [{"label": "其他", "sa": "A(1)"}]):
            with self.subTest(value=value), self.assertRaises(SafetyStop):
                sa_claim_source(value)

    def test_refuses_ambiguous_or_non_text_numbers(self):
        for value in ("", "姓名", "A(1); B(2)", "A(1e20)", "A(１２３)", "A()", "A(abc)", "A(1)(机构)", "A((1)", "A（1)"):
            with self.subTest(value=value), self.assertRaises(SafetyStop):
                sa_claim_source(self.field(value))

    def test_only_uses_sa_column(self):
        with self.assertRaises(SafetyStop):
            sa_claim_source([{"label": "认领状态", "sa": "", "library": "测试员(00001)"}])
