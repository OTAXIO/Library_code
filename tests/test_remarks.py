import copy
import unittest

from core import SafetyStop
from remarks import (CLAIMED, CORRESPONDENT_FIXED, SA_MISSING_IDS,
                     append_remark, valid_length, validate_note)


class RemarkTests(unittest.TestCase):
    def setUp(self):
        self.reason = "作者不一致；DOI和WOSID都不一致；通讯作者标记不一致"
        self.rows = [
            {"label": "认领状态", "sa": "测试员", "library": "已认领"},
            {"label": "DOI", "sa": "", "library": "10.example/demo"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:DEMO"},
            {"label": "作者信息", "sa": "是否通讯作者：是", "library": "署名：Demo\n是否通讯作者：否"},
        ]

    def validate(self, note):
        validate_note(note, self.rows, self.reason, 1)

    def test_short_claimed_remark_allowed(self):
        self.assertTrue(valid_length(CLAIMED))
        self.validate(CLAIMED)

    def test_unknown_short_remark_rejected(self):
        self.assertFalse(valid_length("完成"))
        self.assertFalse(valid_length(""))

    def test_unclaimed_cannot_claim_completion(self):
        self.rows[0]["library"] = "未认领"
        with self.assertRaises(SafetyStop):
            self.validate(CLAIMED)

    def test_claim_requires_author_mismatch_reason(self):
        self.reason = "DOI和WOSID都不一致"
        with self.assertRaises(SafetyStop):
            self.validate(CLAIMED)

    def test_multi_match_claim_is_rejected(self):
        with self.assertRaises(SafetyStop):
            validate_note(CLAIMED, self.rows, self.reason, 2)

    def test_both_sa_identifiers_missing(self):
        self.validate(SA_MISSING_IDS)

    def test_only_one_missing_rejected(self):
        for index in (1, 2):
            with self.subTest(index=index):
                rows = copy.deepcopy(self.rows)
                rows[index]["sa"] = "present"
                with self.assertRaises(SafetyStop):
                    validate_note(SA_MISSING_IDS, rows, self.reason, 1)

    def test_unknown_field_not_treated_as_empty(self):
        self.rows.pop(2)
        with self.assertRaises(SafetyStop):
            self.validate(SA_MISSING_IDS)

    def test_duplicate_field_rejected(self):
        self.rows.append(copy.deepcopy(self.rows[1]))
        with self.assertRaises(SafetyStop):
            self.validate(SA_MISSING_IDS)

    def test_correspondent_can_differ_from_sa_after_review(self):
        # The original paper, not agreement with SA, determines the correct flag.
        self.validate(CORRESPONDENT_FIXED)

    def test_unknown_correspondent_flag_rejected(self):
        self.rows[3]["library"] = "是否通讯作者：未知"
        with self.assertRaises(SafetyStop):
            self.validate(CORRESPONDENT_FIXED)

    def test_correspondent_reason_required(self):
        self.reason = "作者不一致"
        with self.assertRaises(SafetyStop):
            self.validate(CORRESPONDENT_FIXED)

    def test_merge_preserves_exact_text_and_deduplicates(self):
        note = append_remark("", CLAIMED)
        note = append_remark(note, SA_MISSING_IDS)
        note = append_remark(note, CORRESPONDENT_FIXED)
        self.assertEqual(note, "已认领；DOI和WOSID SA未提交；通讯作者修正")
        self.assertEqual(append_remark(note, CLAIMED), note)
        self.validate(note)

    def test_typed_preset_still_validated(self):
        self.rows[0]["library"] = "未认领"
        with self.assertRaises(SafetyStop):
            self.validate("经核对，已认领；原文证据见本地记录")


if __name__ == "__main__":
    unittest.main()
