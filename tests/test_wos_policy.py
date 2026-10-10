"""Evidence-only affiliation decisions; no real papers, APIs or user workbook."""
import unittest

from automation import classify, identity, parse_wos
from core import SafetyStop
from tests.test_automation import record, result, sample
from wos_policy import WOSPolicyStop, affiliation_status, zero_result_note


class WOSPolicyTests(unittest.TestCase):
    def candidate(self, **changes):
        return parse_wos(sample(**changes))

    def test_explicit_sjtu_affiliation_variants(self):
        for institution in ('Shanghai Jiao Tong Univ', 'Shanghai Jiaotong University',
                            'Shanghai JiaoTong Univ', '上海交通大学'):
            with self.subTest(institution=institution):
                self.assertEqual(affiliation_status(self.candidate(C1=institution + ', China')), 'sjtu')

    def test_non_sjtu_requires_all_authors_mapped_to_actual_institutions(self):
        c = self.candidate(AF='Test, Alice; Other, Bob',
                           C1='[Test, Alice] Other Univ, Country; [Other, Bob] Another Institute, Country')
        self.assertEqual(affiliation_status(c), 'non_sjtu')
        # The candidate belongs to the right paper before a negative is adopted.
        with self.assertRaises(WOSPolicyStop) as caught:
            identity(record(), c)
        self.assertEqual(caught.exception.note, '非交大')

    def test_incomplete_unknown_or_unmapped_addresses_are_not_non_sjtu(self):
        for value in ('', 'Other University', '[Someone Else] Other Univ, Country',
                      '[Alice Test] ... Other Univ', '[Alice Test] China', '[Alice Test]',
                      '[Alice Test] Unknown', '[Alice Test] Other Univ; …'):
            with self.subTest(value=value):
                self.assertEqual(affiliation_status(self.candidate(C1=value)), 'unknown')
        # A partial mapping must not misclassify the uncovered coauthor.
        self.assertEqual(affiliation_status(self.candidate(AF='Alice Test; Bob Other',
                             C1='[Alice Test] Other Univ, Country')), 'unknown')

    def test_cached_sjtu_boolean_cannot_override_the_exported_address(self):
        c = self.candidate(C1='[Alice Test] Other Univ, Country')
        c['sjtu'] = True
        self.assertEqual(affiliation_status(c), 'non_sjtu')
        with self.assertRaises(WOSPolicyStop):
            identity(record(), c)

    def test_conflicting_and_weak_identity_never_get_a_non_sjtu_note(self):
        c = self.candidate(C1='[Alice Test] Other Univ, Country')
        for r in (record(doi='10.1234/conflict'), record(title='A different paper'), record(doi='')):
            with self.subTest(record=r):
                with self.assertRaises(SafetyStop) as caught:
                    identity(r, c)
                self.assertNotIsInstance(caught.exception, WOSPolicyStop)

    def test_author_order_and_first_institution_are_deferred_but_ordinary_claim_is_unchanged(self):
        for reason in ('第一作者不一致', '通讯作者错误', '交大是否第一单位不一致', '作者顺序待核验'):
            with self.subTest(reason=reason):
                self.assertEqual(classify(record(reason=reason), result()).route, 'manual')
                self.assertEqual(classify(record(), result(reason=reason)).route, 'manual')
                with self.assertRaises(WOSPolicyStop):
                    identity(record(reason=reason), self.candidate())
        self.assertEqual(classify(record(reason='作者不一致'), result()).route, 'wos')

    def test_only_confirmed_zero_diagnostic_maps_to_requested_note(self):
        for message in ('WOS 未找到记录', '[扩展 0.4.1] [WOS 已暂停] WOS 未找到记录；这不等于未发表',
                        '[WOS 已暂停] WOS 未找到记录。这不自动标记完成'):
            self.assertEqual(zero_result_note(message), 'wos未查询到')
        for message in ('未找到记录链接', 'WOS 未找到记录链接，页面未就绪', 'WOS 检索结果超时',
                        'WOS 结果不是可确认的唯一记录', 'Oops, something went wrong!', '浏览器未连接',
                        'WOS 检索页保留上一条零结果，需刷新检索页',
                        'WOS 刷新后的检索页尚未就绪，上一条提示未清除或输入区仍在加载；未提交当前论文检索'):
            self.assertEqual(zero_result_note(message), '')


if __name__ == '__main__':
    unittest.main()
