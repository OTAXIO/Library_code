"""Automatic claim queue tests use synthetic records and a mocked browser."""
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from approval import COMBINED_MISSING_IDS_CLAIM
from claim_batch import automatic_claim_selection, run_claim_batch
from core import Record, SafetyStop
from bridge import BrowserRejected


def record(sa_id="demo-001", **changes):
    base = Record(2, "谭勋策", sa_id, "Synthetic", "", "", "00001", 1, "item-1",
                  "待处理", "作者不一致", "2")
    return replace(base, **changes)


def found(rec):
    row = {"saLzkId": rec.sa_id, "itemId": "item-1", "matchCount": 1,
           "markStatus": "待处理", "reason": "作者不一致", "gh": "00001", "remark": ""}
    comparison = [{"label": "认领状态", "sa": "测试员(00001)①", "library": "未认领"},
                  {"label": "作者信息", "sa": "测试员", "library": "Tester"}]
    return {"row": row, "comparison": comparison}


def prepared(search):
    data = {"staff_id": "00001", "sa_text": "测试员(00001)①", "item_id": "item-1",
            "person": {"id": "scholar-1", "wno": "00001", "name": "测试员",
                       "names": ["测试员", "Tester"]},
            "authors": [{"index": 0, "order": 1, "fullname": "Tester", "scholarId": "",
                         "eligible": True, "relations": []}]}
    return {"row": copy.deepcopy(search["row"]), "comparison": copy.deepcopy(search["comparison"]),
            "prepared": data, "suggested_index": 0}


class FakeRoster:
    def __init__(self, records):
        self.records = records
        self.assert_unchanged = Mock()


def persist_skips(roster, records):
    ids = {item.sa_id for item in records}
    updated = [replace(item, skipped=True, remark="2") if item.sa_id in ids else item
               for item in roster.records]
    return SimpleNamespace(roster=FakeRoster(updated))


class ClaimBatchTests(unittest.TestCase):
    def test_selection_requires_one_unique_suggested_signature(self):
        rec = record()
        search = found(rec)
        result = prepared(search)
        selected = automatic_claim_selection(rec, search["row"], search["comparison"], result)
        self.assertEqual(selected[2]["fullname"], "Tester")
        for change in (True, 9):
            bad = copy.deepcopy(result)
            bad["suggested_index"] = change
            with self.assertRaises(SafetyStop):
                automatic_claim_selection(rec, search["row"], search["comparison"], bad)
        bad = copy.deepcopy(result)
        bad["prepared"]["person"]["names"] = ["Another"]
        with self.assertRaises(SafetyStop):
            automatic_claim_selection(rec, search["row"], search["comparison"], bad)

    def test_skips_red_candidate_then_completes_next_without_manual_approval(self):
        skipped = record("demo-skip", reason="通讯作者不一致")
        target = record("demo-ok")
        roster = FakeRoster([skipped, target])
        search = found(target)
        search["row"]["saLzkId"] = target.sa_id
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        completed_roster = FakeRoster([replace(skipped), replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=completed_roster))
        audit = Mock()
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [skipped, target], bridge, audit=audit,
                                     skip_writer=persist_skips)
        self.assertEqual(result.completed_ids, ("demo-ok",))
        self.assertIn("demo-skip", result.skipped)
        self.assertFalse(result.halted)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "submit_claim"])
        self.assertTrue(bridge.call.call_args_list[-1].args[1]["confirmed"])
        close.assert_called_once()
        self.assertIn(("自动认领列表条目", "已跳过", "demo-skip"),
                      [call.args for call in audit.call_args_list])
        self.assertTrue(next(item for item in result.roster.records
                             if item.sa_id == "demo-skip").skipped)

    def test_unique_registered_alias_formatting_can_fill_an_absent_native_hint(self):
        rec=record();search=found(rec);result=prepared(search)
        result['prepared']['person']['names']=['Family, Given']
        result['prepared']['authors'][0]['fullname']='Family,Given'
        result['suggested_index']=None
        selected=automatic_claim_selection(rec,search['row'],search['comparison'],result)
        self.assertEqual(selected[2]['index'],0)
        self.assertEqual(selected[1]['wno'],'00001')
        for name in ('FAMILY, GIVEN','Ｆａｍｉｌｙ，Ｇｉｖｅｎ'):
            result['prepared']['authors'][0]['fullname']=name
            self.assertEqual(automatic_claim_selection(rec,search['row'],search['comparison'],result)[2]['index'],0)

    def test_unique_initial_or_one_character_alias_is_accepted_but_ambiguity_is_not(self):
        rec=record();search=found(rec);result=prepared(search)
        result['prepared']['person']['names']=['Family, Given']
        result['suggested_index']=None
        for name in ('Family, G','Family, Givin'):
            result['prepared']['authors'][0]['fullname']=name
            self.assertEqual(automatic_claim_selection(rec,search['row'],search['comparison'],result)[2]['index'],0)
        result['prepared']['authors'][0]['fullname']='Given, Family'
        with self.assertRaises(SafetyStop):automatic_claim_selection(rec,search['row'],search['comparison'],result)
        result['prepared']['authors'][0]['fullname']='Family,Given'
        second=copy.deepcopy(result['prepared']['authors'][0]);second.update(index=1,order=2,fullname='Family, Given')
        result['prepared']['authors'].append(second)
        with self.assertRaises(SafetyStop):automatic_claim_selection(rec,search['row'],search['comparison'],result)

    def test_unique_initial_and_transliteration_patterns(self):
        rec=record();search=found(rec);result=prepared(search);result['suggested_index']=None
        for aliases, author in ((['测试甲','Family, Given'],'Family,G'),
                                (['测试乙','Example, Liyang'],'Example,Liyng'),
                                (['测试丙','Person, Xi-Hao'],'Person,Xihao')):
            result['prepared']['person']['names']=aliases
            result['prepared']['authors']=[{'index':0,'order':1,'fullname':author,
                'scholarId':'','eligible':True,'relations':[]}]
            self.assertEqual(automatic_claim_selection(rec,search['row'],search['comparison'],result)[2]['index'],0)

    def test_unique_eligible_author_never_overrides_a_different_surname(self):
        rec=record();search=found(rec);result=prepared(search);result['suggested_index']=None
        result['prepared']['person']['names']=['陆佩佩','Lu, Peipei']
        result['prepared']['authors']=[{'index':7,'order':8,'fullname':'An,Qinlong',
            'scholarId':'','eligible':True,'relations':[]}]
        with self.assertRaises(SafetyStop):
            automatic_claim_selection(rec,search['row'],search['comparison'],result)

    def test_positive_non_institution_author_closes_task_without_a_claim(self):
        rec=record();search=found(rec);prep=prepared(search)
        prep['prepared']['authors'][0].update(eligible=False,non_institution=True,institution_orders=[1])
        prep['prepared']['institutions']=[{'order':1,'address':'Example Foreign University'}]
        prep['suggested_index']=None
        bridge=Mock();bridge.call.side_effect=[search,prep]
        updated=FakeRoster([replace(rec,done=True,skipped=False,remark='非交大作者')])
        with patch('approval.complete_non_institution_author',return_value=SimpleNamespace(
                completion=SimpleNamespace(roster=updated))) as close:
            result=run_claim_batch(FakeRoster([rec]),[rec],bridge,skip_writer=persist_skips)
        self.assertFalse(result.halted);self.assertEqual(result.completed_ids,(rec.sa_id,))
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list],['search','prepare_claim'])
        close.assert_called_once()

    def test_non_eligible_without_positive_institution_evidence_is_still_deferred(self):
        rec=record();search=found(rec);prep=prepared(search)
        prep['prepared']['authors'][0]['eligible']=False
        with self.assertRaises(SafetyStop):
            automatic_claim_selection(rec,search['row'],search['comparison'],prep,include_non_institution=True)

    def test_foreign_author_read_rejection_continues_and_is_not_an_unknown_write(self):
        rec=record();search=found(rec);prep=prepared(search)
        prep['prepared']['authors'][0].update(eligible=False,non_institution=True,institution_orders=[1])
        bridge=Mock();bridge.call.side_effect=[search,prep]
        with patch('approval.complete_non_institution_author',side_effect=BrowserRejected('complete',
                '[扩展 test] 【已暂停】非交大作者或单位资料已变化，未结案')):
            result=run_claim_batch(FakeRoster([rec]),[rec],bridge,skip_writer=persist_skips)
        self.assertFalse(result.halted);self.assertEqual(result.skipped[rec.sa_id],'非交大作者资料待核')

    def test_explicit_unchanged_backend_rejection_does_not_retry_or_stop_the_next_paper(self):
        first,second=record('demo-rejected'),record('demo-next');roster=FakeRoster([first,second])
        a,b=found(first),found(second);rejected={'row':a['row'],'rejected':True,'verified':False,
            'claim_response_protocol':1,'unchanged':True,'staff_id':'00001','scholar_id':'scholar-1',
            'order':1,'rejection':{'status':400,'code':400,'message':'Synthetic validation rejection'}}
        proof={'row':b['row'],'verified':True,'claimed':True,'staff_id':'00001',
               'scholar_id':'scholar-1','author':'Tester','order':1}
        bridge=Mock();bridge.call.side_effect=[a,prepared(a),rejected,b,prepared(b),proof]
        closed=FakeRoster([first,replace(second,done=True)])
        with patch('claim_batch.auto_complete_claim',return_value=SimpleNamespace(completion=SimpleNamespace(roster=closed))):
            result=run_claim_batch(roster,[first,second],bridge,skip_writer=persist_skips)
        self.assertFalse(result.halted);self.assertEqual(result.completed_ids,(second.sa_id,))
        self.assertEqual(result.skipped[first.sa_id],'后台拒绝认领')
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list].count('submit_claim'),2)

    def test_unknown_old_claim_can_be_read_but_is_never_submitted_again(self):
        for label in ('认领回执待核','认领结案待核','认领接口400待核','非交大作者结案待核'):
            rec=record(skipped=True,remark=label);roster=FakeRoster([rec]);bridge=Mock()
            bridge.call.return_value=found(rec)
            result=run_claim_batch(roster,[rec],bridge,retry_skipped=True,skip_writer=persist_skips)
            self.assertFalse(result.halted);self.assertEqual(result.skipped[rec.sa_id],label)
            self.assertEqual([c.args[0] for c in bridge.call.call_args_list],['search'])

    def test_submit_preflight_rejection_is_not_marked_as_an_unknown_write(self):
        rec=record();search=found(rec);bridge=Mock()
        bridge.call.side_effect=[search,prepared(search),BrowserRejected('submit_claim',
            '[扩展 test] 【已暂停】人员或作者数据已变化，请重新查找并确认'),search]
        result=run_claim_batch(FakeRoster([rec]),[rec],bridge,skip_writer=persist_skips)
        self.assertFalse(result.halted)
        self.assertNotIn('认领结果不确定',result.skipped[rec.sa_id])
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list],
            ['search','prepare_claim','submit_claim','search'])

    def test_ambiguous_author_closes_owned_drawer_and_continues(self):
        first, second = record("demo-a"), record("demo-b")
        roster = FakeRoster([first, second])
        search_a, search_b = found(first), found(second)
        ambiguous = prepared(search_a)
        ambiguous["suggested_index"] = None
        duplicate = copy.deepcopy(ambiguous['prepared']['authors'][0])
        duplicate.update(index=1, order=2)
        ambiguous['prepared']['authors'].append(duplicate)
        cleanup = copy.deepcopy(search_a)
        prep_b = prepared(search_b)
        proof = {"row": search_b["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search_a, ambiguous, cleanup, search_b, prep_b, proof]
        closed_roster = FakeRoster([first, replace(second, done=True)])
        with patch("claim_batch.auto_complete_claim",
                   return_value=SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))):
            result = run_claim_batch(roster, [first, second], bridge,
                                     skip_writer=persist_skips)
        self.assertIn(first.sa_id, result.skipped)
        self.assertEqual(result.completed_ids, (second.sa_id,))
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "search", "search", "prepare_claim", "submit_claim"])

    def test_missing_doi_and_wos_unclaimed_uses_combined_note(self):
        target = record("demo-combined", reason="DOI和WOSID都不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "", "library": "10.1000/example"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:000000000000001"},
        ])
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [target], bridge)
        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertEqual(close.call_args.kwargs["note"], COMBINED_MISSING_IDS_CLAIM)

    def test_combined_claim_allows_auxiliary_display_differences(self):
        target = record("demo-combined-extra", reason="DOI和WOSID都不一致;作者不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "", "library": "10.1016/j.oceaneng.2025.122998"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:001609529300006"},
            {"label": "发表年月", "sa": "2026-01", "library": "2026"},
            {"label": "作者单位", "sa": "船建", "library": "Shanghai Jiao Tong Univ"},
        ])
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [target], bridge)

        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertEqual(close.call_args.kwargs["note"], COMBINED_MISSING_IDS_CLAIM)

    def test_combined_case_requires_both_sa_identifiers_blank(self):
        target = record("demo-incomplete", reason="DOI和WOSID都不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "10.1000/present", "library": "10.1000/example"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:000000000000001"},
        ])
        bridge = Mock()
        bridge.call.return_value = search
        result = run_claim_batch(roster, [target], bridge, skip_writer=persist_skips)
        self.assertIn(target.sa_id, result.skipped)
        self.assertIn("并非同时为空", result.skipped[target.sa_id])
        bridge.call.assert_called_once_with('search', {'sa_id': target.sa_id})

    def test_missing_claim_window_is_red_skip_and_next_record_continues(self):
        first, second = record("demo-no-window"), record("demo-next")
        roster = FakeRoster([first, second])
        search_a, search_b = found(first), found(second)
        prep_b = prepared(search_b)
        proof = {"row": search_b["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search_a, SafetyStop("[扩展 0.3.17] 作者认领页面结构已变化，请更新扩展"),
                                   search_b, prep_b, proof]
        closed_roster = FakeRoster([first, replace(second, done=True)])
        with patch("claim_batch.auto_complete_claim",
                   return_value=SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))):
            result = run_claim_batch(roster, [first, second], bridge,
                                     skip_writer=persist_skips)
        self.assertIn(first.sa_id, result.skipped)
        self.assertIn("没有可用的作者认领窗口", result.skipped[first.sa_id])
        self.assertEqual(result.completed_ids, (second.sa_id,))
        self.assertFalse(result.halted)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "search", "prepare_claim", "submit_claim"])

    def test_uncertain_submit_marks_red_and_halts_without_next_record(self):
        first, second = record("demo-a"), record("demo-b")
        roster = FakeRoster([first, second])
        search = found(first)
        bridge = Mock()
        bridge.call.side_effect = [search, prepared(search), SafetyStop("timeout after submit")]
        result = run_claim_batch(roster, [first, second], bridge,
                                 skip_writer=persist_skips)
        self.assertTrue(result.halted)
        self.assertIn(first.sa_id, result.skipped)
        self.assertEqual(result.checked, 1)
        self.assertEqual(result.completed_ids, ())
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "submit_claim"])

    def test_skip_marker_write_failure_halts_and_preserves_reason(self):
        target = record("demo-write-two", reason="通讯作者不一致")
        roster = FakeRoster([target])
        writer = Mock(side_effect=SafetyStop("Excel 正在使用名单"))
        result = run_claim_batch(roster, [target], Mock(), skip_writer=writer)
        self.assertTrue(result.halted)
        self.assertFalse(result.roster.records[0].skipped)
        self.assertIn("Excel 数字 2 写入失败", result.skipped[target.sa_id])
        writer.assert_called_once()

    def test_normal_mode_ignores_numeric_two_and_retry_mode_accepts_it(self):
        target = record("demo-red", skipped=True, remark="2")
        roster = FakeRoster([target])
        bridge = Mock()
        normal = run_claim_batch(roster, [target], bridge)
        self.assertEqual(normal.checked, 0)
        bridge.call.assert_not_called()

        search = found(target)
        bridge.call.side_effect = [search, SafetyStop("作者认领页面结构已变化")]
        retry = run_claim_batch(roster, [target], bridge, retry_skipped=True,
                                skip_writer=persist_skips)
        self.assertEqual(retry.checked, 1)
        self.assertIn(target.sa_id, retry.skipped)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim"])

    def test_already_claimed_pending_row_is_closed_without_duplicate_claim(self):
        target = record("demo-claimed")
        roster = FakeRoster([target])
        search = found(target)
        search["comparison"][0]["library"] = "已认领"
        bridge = Mock()
        bridge.call.side_effect = [search, {**search, 'verified': True, 'claimed': True,
            'staff_id': '00001', 'scholar_id': 'scholar-1', 'order': 1}]
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.complete_claim", return_value=closure) as close, \
                patch("claim_batch.auto_complete_claim") as new_claim:
            result = run_claim_batch(roster, [target], bridge)
        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertFalse(result.halted)
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list], ['search', 'inspect_claim'])
        close.assert_called_once()
        self.assertEqual(close.call_args.kwargs["note"], "已认领")
        new_claim.assert_not_called()

    def test_same_name_different_staff_is_not_accepted_as_claimed(self):
        target = record('demo-wrong-staff')
        roster = FakeRoster([target])
        search = found(target)
        search['comparison'][0]['library'] = '已认领'
        bridge = Mock()
        bridge.call.side_effect = [search, {**search, 'verified': True, 'claimed': True,
            'staff_id': '00002', 'scholar_id': 'wrong-scholar', 'order': 1}]
        with patch('claim_batch.complete_claim') as close:
            result = run_claim_batch(roster, [target], bridge, skip_writer=persist_skips)
        close.assert_not_called()
        self.assertEqual(result.completed_ids, ())
        self.assertIn('工号', result.skipped[target.sa_id])

    def test_retry_malformed_identity_card_is_one_record_review_and_next_claim_continues(self):
        first,second=record('bad-card',skipped=True),record('next',skipped=True)
        roster=FakeRoster([first,second]);search=found(second);prep=prepared(search)
        proof={'row':search['row'],'verified':True,'claimed':True,'staff_id':'00001',
               'scholar_id':'scholar-1','author':'Tester','order':1}
        bridge=Mock();bridge.call.side_effect=[BrowserRejected('search',
            '[扩展 test] 【已暂停】新版认领卡片字段不完整，不能推断作者或人员编号'),found(first),search,prep,proof]
        closure=SimpleNamespace(completion=SimpleNamespace(roster=FakeRoster([first,replace(second,done=True)])))
        with patch('claim_batch.auto_complete_claim',return_value=closure):
            result=run_claim_batch(roster,[first,second],bridge,retry_skipped=True,skip_writer=persist_skips)
        self.assertFalse(result.halted)
        self.assertEqual(result.completed_ids,(second.sa_id,))
        self.assertEqual(result.skipped[first.sa_id],'认领信息待核')

    def test_malformed_card_followed_by_login_expiry_preserves_existing_note_and_halts(self):
        target=record('expired',skipped=True,remark='保留旧备注');roster=FakeRoster([target]);bridge=Mock()
        bridge.call.side_effect=[BrowserRejected('search','【已暂停】新版认领卡片字段不完整'),
                                BrowserRejected('status','【已暂停】已离开比对结果页或登录失效')]
        writer=Mock()
        result=run_claim_batch(roster,[target],bridge,retry_skipped=True,skip_writer=writer)
        self.assertTrue(result.halted)
        self.assertIn('登录',result.reason)
        self.assertEqual(result.skipped,{})
        writer.assert_not_called()
        self.assertEqual(target.remark,'保留旧备注')

    def test_unknown_read_timeout_is_still_a_batch_stop(self):
        target=record('timeout',skipped=True);roster=FakeRoster([target]);bridge=Mock()
        bridge.call.side_effect=SafetyStop('命令超时，结果不明')
        result=run_claim_batch(roster,[target],bridge,retry_skipped=True,skip_writer=persist_skips)
        self.assertTrue(result.halted)
        self.assertIn('命令超时',result.reason)

    def test_cancel_and_limit_are_enforced(self):
        records = [record(f"demo-{index:03}") for index in range(101)]
        roster = FakeRoster(records)
        result = run_claim_batch(roster, records, Mock(), cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.checked, 0)
        with self.assertRaises(SafetyStop):
            run_claim_batch(roster, records, Mock(), limit=101)

    def test_pause_during_person_lookup_prevents_claim_and_does_not_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        stopped = False
        def reply(action, payload):
            nonlocal stopped
            if action == "search":
                return search
            if action == "prepare_claim":
                stopped = True
                return prepared(search)
            raise AssertionError("No write allowed after pause: " + action)
        bridge.call.side_effect = reply
        writer = Mock()
        result = run_claim_batch(roster, [rec], bridge, cancel=lambda: stopped, skip_writer=writer)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.skipped, {})
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list], ["search", "prepare_claim"])
        writer.assert_not_called()

    def test_roster_change_during_person_lookup_blocks_claim_without_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        def reply(action, payload):
            if action == "search":
                return search
            if action == "prepare_claim":
                roster.assert_unchanged.side_effect = SafetyStop("名单已改变")
                return prepared(search)
            raise AssertionError("Stale roster must not be submitted: " + action)
        bridge.call.side_effect = reply
        writer = Mock()
        result = run_claim_batch(roster, [rec], bridge, skip_writer=writer)
        self.assertTrue(result.halted)
        self.assertEqual(result.skipped, {})
        self.assertIn("名单已改变", result.reason)
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list], ["search", "prepare_claim"])
        writer.assert_not_called()

    def test_pause_after_confirmed_claim_does_not_start_closure_or_mark_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        stopped = False
        def reply(action, payload):
            nonlocal stopped
            if action == "search":
                return search
            if action == "prepare_claim":
                return prepared(search)
            if action == "submit_claim":
                stopped = True
                return {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                        "scholar_id": "scholar-1", "author": "Tester", "order": 1}
            raise AssertionError(action)
        bridge.call.side_effect = reply
        writer = Mock()
        with patch("claim_batch.auto_complete_claim") as close:
            result = run_claim_batch(roster, [rec], bridge, cancel=lambda: stopped, skip_writer=writer)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.skipped, {})
        close.assert_not_called()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
