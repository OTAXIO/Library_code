"""Short labels must never turn a technical failure into a coverage conclusion."""
import unittest

from skip_notes import brief_skip_note, known_skip_note


class SkipNoteTests(unittest.TestCase):
    def test_requested_zero_and_verified_non_sjtu_labels(self):
        self.assertEqual(brief_skip_note('wos未查询到'), 'wos未收录')
        self.assertEqual(brief_skip_note('非交大'), '非交大')
        self.assertEqual(brief_skip_note('交大署名待核验'), '交大署名待核验')

    def test_short_specific_labels_do_not_include_pipeline_preamble(self):
        self.assertEqual(brief_skip_note('已补录/关联，第一单位待核验，暂不处理'), '第一单位待核验')
        self.assertEqual(brief_skip_note('已补录/关联，作者编号待核验'), '作者编号待核验')
        self.assertEqual(brief_skip_note('已补录/关联，涉及作者角色判断，暂不处理'), '作者角色待核验')

    def test_old_timeout_and_missing_control_are_not_unindexed(self):
        prefix = 'WOS 下载未完成：[扩展 0.3.29] [WOS 已暂停] '
        self.assertEqual(known_skip_note(prefix + 'WOS 检索结果超时；未把页面问题当作未查询到。'), 'WOS检索超时')
        self.assertEqual(known_skip_note(prefix + '文献检索按钮未唯一识别（当前检索区域识别到 0 个）'), 'WOS控件未识别')
        self.assertEqual(known_skip_note(prefix + '未处于 WOS 核心合集单篇完整记录页'), 'WOS单篇页待核验')
        self.assertEqual(known_skip_note(prefix + 'WOS 未找到记录；不标记完成'), 'wos未收录')
        self.assertEqual(known_skip_note(prefix + 'WOS 未找到记录链接；页面尚未就绪'), '')

    def test_unknown_human_notes_are_not_eligible_for_migration(self):
        for note in ('人工结论：该文还有其他问题', '这个没找到，先等等', '这可能非交大',
                     '已认领', 'WOS补充入库；已核验平台关联'):
            self.assertEqual(known_skip_note(note), '')
        self.assertEqual(brief_skip_note('未知结构异常；完整细节应保存到日志'), '待人工核验')


if __name__ == '__main__':
    unittest.main()
