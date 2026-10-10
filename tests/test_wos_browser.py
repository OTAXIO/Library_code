import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import SafetyStop
from tests.test_automation import sample
from tests.test_wos_batch import record
from wos_browser import (BrowserSkillWOS, DEFAULTS, WOSBrowserStop, read_settings,
                         select_transport, validate_settings)
from wos_batch import export, preflight
from automation import ImportStore


class SettingsTests(unittest.TestCase):
    def test_explicit_profile_and_exact_origin_are_required(self):
        self.assertEqual(validate_settings(DEFAULTS),DEFAULTS)
        for changes in ({'transport':'other'},{'transport':'browser-skill'},
                        {'instance_id':'../unsafe'},{'origin':'https://example.invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings({**DEFAULTS,**changes})
        self.assertEqual(validate_settings({**DEFAULTS,'transport':'browser-skill','instance_id':'demo-profile'})['instance_id'],'demo-profile')

    def test_default_preserves_original_transport_and_bad_config_never_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            self.assertEqual(read_settings(root),DEFAULTS)
            (root/'wos_browser.json').write_text('{"transport":"other"}')
            with self.assertRaises(SafetyStop):
                read_settings(root)

    def test_selected_skill_does_not_require_or_modify_sa_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            (base/'runtime').mkdir()
            (base/'runtime/wos_browser.json').write_text(json.dumps({**DEFAULTS,'transport':'browser-skill','instance_id':'demo-profile'}))
            with patch('wos_browser.BrowserSkillWOS') as factory:
                select_transport(None,base)
            self.assertEqual(factory.call_args.args[1],'demo-profile')
            (base/'runtime/wos_browser.json').unlink()
            with self.assertRaisesRegex(SafetyStop,'原插件尚未连接'):
                select_transport(None,base)


class FakeBrowser(BrowserSkillWOS):
    def __init__(self, base, states=None, observe=None):
        super().__init__(base,'synthetic-profile','https://webofscience.clarivate.cn',cli='synthetic-bsk')
        self.states=list(states or [])
        self.visits=[]
        self.actions=[]
        self.downloads=0
        self.observed=observe or {'text':'L1 modal\n  @e3 button "Export"\n  @e9 button "Export [has-submenu]"'}

    def _start(self):
        self.session,self.tab_id='synthetic-session',1
        self.version='0.3.2'

    def _ready_search(self):
        return {'core_search_route':True,'query_input_count':1}

    def _search_page(self):
        self.visits.append(self.origin+'/wos/woscc/basic-search')

    def _open_record(self,url):
        self.visits.append(url)

    def _navigate(self,url):
        self.visits.append(url)

    def _page_action(self,action,query):
        self.actions.append(action)
        return {'ready':True}

    def _probe(self,action='wos_diagnose'):
        return self.states.pop(0)

    def _observe(self):
        return self.observed

    def _run(self,args,**kwargs):
        if args[0]!='download':
            raise AssertionError(args)
        self.downloads+=1
        path=Path(args[args.index('--out')+1])
        path.write_bytes(sample())
        return {'path':str(path),'byte_size':path.stat().st_size}


class BrowserFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.url='https://webofscience.clarivate.cn/wos/woscc/full-record/WOS:000123456789012'
        self.query={'sa_id':'demo-001','title':'Synthetic paper','doi':'10.1234/test','wos':''}

    def test_one_real_capture_path_after_one_search_and_read_only_checks(self):
        bridge=FakeBrowser(self.base,[{'ok':True,'data':{'state':'single','navigate_url':self.url}},
                                    {'ok':True,'data':{'state':'record','record_url':self.url}}])
        self.assertEqual(preflight(bridge)['transport'],'browser-skill')
        bridge.call('wos_search',self.query)
        result=bridge.call('wos_export',self.query)
        self.assertEqual(bridge.downloads,1)
        self.assertEqual(bridge.actions,['wos_start_search','wos_prepare_export','wos_check_export'])
        self.assertEqual(Path(result['path']).read_bytes(),sample())
        self.assertEqual(result['sa_id'],'demo-001')
        with self.assertRaisesRegex(WOSBrowserStop,'已提交'):
            bridge.call('wos_export',self.query)
        self.assertEqual(bridge.downloads,1)

    def test_zero_result_is_a_paper_outcome_and_next_paper_may_search(self):
        bridge=FakeBrowser(self.base,[{'ok':True,'data':{'state':'zero'}},
                                    {'ok':True,'data':{'state':'record','record_url':self.url}}])
        with self.assertRaisesRegex(SafetyStop,'未找到记录'):
            bridge.call('wos_search',self.query)
        self.assertFalse(bridge.blocked)
        bridge.call('wos_search',{**self.query,'sa_id':'next'})
        self.assertEqual(bridge.actions,['wos_start_search','wos_start_search'])

    def test_multiple_or_wrong_identifier_never_exports_arbitrary_paper(self):
        for data,query in (({'state':'multiple'},self.query),
                           ({'state':'record','record_url':self.url},{**self.query,'wos':'WOS:000123456789013'})):
            bridge=FakeBrowser(self.base,[{'ok':True,'data':data}])
            with self.assertRaises(SafetyStop):
                bridge.call('wos_search',query)
            self.assertEqual(bridge.downloads,0)

    def test_foreign_link_or_page_error_blocks_the_batch_not_all_remaining_rows(self):
        for state in ({'ok':True,'data':{'state':'single','navigate_url':'https://example.invalid/paper'}},
                      {'ok':False,'error':'登录或验证码需要人工处理'}):
            bridge=FakeBrowser(self.base,[state])
            results=export([record(),record(row=3,sa_id='next')],bridge,
                           ImportStore(self.base/'archives'),self.base/'inbox')
            self.assertTrue(results['page_blocked'])
            self.assertTrue(results['stopped'])
            self.assertFalse(results['disconnected'])
            self.assertEqual(results['attempted'],1)
            self.assertEqual(results['remaining'],1)
            self.assertNotIn('next',results['failed'])

    def test_changed_modal_buttons_fail_without_a_download(self):
        bridge=FakeBrowser(self.base,observe={'text':'L1 modal\n  @e3 button "Export"\n  @e4 button "Export"'})
        bridge.session='synthetic-session'
        bridge.query=self.query
        bridge.record_url=self.url
        with self.assertRaisesRegex(WOSBrowserStop,'最终导出按钮'):
            bridge.call('wos_export',self.query)
        self.assertEqual(bridge.downloads,0)

    def test_transport_cannot_upload_import_push_or_read_other_sites(self):
        bridge=FakeBrowser(self.base)
        for action in ('import_upload','import_execute','import_push','inspect','claim_confirm'):
            with self.assertRaisesRegex(SafetyStop,'不支持后台'):
                bridge.call(action,self.query)
        self.assertEqual(bridge.actions,[])

    def test_unknown_effect_is_not_retried_and_autostart_stays_disabled(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge.deadline=0
        response=Mock(returncode=1,stdout='{"code":"timeout","message":"PRIVATE_BODY"}')
        with patch('wos_browser.subprocess.run',return_value=response) as invoked:
            with self.assertRaises(WOSBrowserStop) as caught:
                bridge._run(['browsers'])
        self.assertNotIn('PRIVATE_BODY',str(caught.exception))
        self.assertEqual(invoked.call_count,1)
        self.assertEqual(invoked.call_args.kwargs['env']['BSK_AUTO_START'],'0')
        self.assertNotIn('shell',invoked.call_args.kwargs)

    def test_healthy_initial_form_is_reused_without_navigation(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge._probe=Mock(return_value={'core_search_route':True,'query_input_count':1})
        bridge._ready_search=Mock()
        bridge._navigate=Mock()
        bridge._click_navigation=Mock()
        bridge._search_page()
        bridge._ready_search.assert_called_once()
        bridge._navigate.assert_not_called()
        bridge._click_navigation.assert_not_called()

    def test_zero_result_resets_form_through_visible_spa_navigation(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge._probe=Mock(return_value={'zero_result':True})
        bridge._ready_search=Mock()
        bridge._navigate=Mock()
        bridge._click_navigation=Mock()
        bridge._search_page()
        self.assertEqual(bridge._click_navigation.call_count,2)
        bridge._navigate.assert_not_called()

    def test_navigation_waits_for_commit_and_checks_timeout_without_replay(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge.session,bridge.tab_id='synthetic-session',1
        url=bridge.origin+'/wos/woscc/basic-search'
        bridge._observe=Mock()
        bridge._run=Mock(side_effect=[WOSBrowserStop('timeout',code='timeout'),
                                     {'activity':{'state':'idle'}},{'tabs':[{'tab_id':1,'url':url}]}])
        bridge._navigate(url)
        args=bridge._run.call_args_list[0].args[0]
        self.assertIn('commit',args)
        self.assertEqual(bridge._run.call_count,3)
        bridge._observe.assert_called_once()
        bridge._run=Mock(side_effect=[WOSBrowserStop('timeout',code='timeout'),
                                     {'activity':{'state':'running'}},{'tabs':[{'tab_id':1,'url':'about:blank'}]}])
        bridge._observe.reset_mock()
        with self.assertRaisesRegex(WOSBrowserStop,'尚未确认'):
            bridge._navigate(url)
        bridge._observe.assert_not_called()

    def test_blank_search_reloads_once_before_search_but_never_after_submission(self):
        for pending in (False,True):
            bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
            bridge.session,bridge.tab_id='synthetic-session',1
            bridge.search_pending=pending
            bridge.deadline=100
            clock=[0]
            bridge._observe=Mock()
            bridge._run=Mock(return_value={})
            bridge._probe=Mock(side_effect=lambda *args: {'core_search_route':True,'query_input_count':1 if bridge.reload_used else 0})
            with patch('wos_browser.time.monotonic',side_effect=lambda:clock[0]), \
                 patch('wos_browser.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
                if pending:
                    with self.assertRaisesRegex(WOSBrowserStop,'不可自动刷新'):
                        bridge._ready_search()
                    bridge._run.assert_not_called()
                else:
                    self.assertEqual(bridge._ready_search()['query_input_count'],1)
                    self.assertEqual(bridge._run.call_count,1)
                    self.assertEqual(bridge._run.call_args.args[0][0],'reload')
                    self.assertIn('commit',bridge._run.call_args.args[0])

    def test_site_timeout_is_not_a_missing_paper_and_does_not_reload_forever(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge.deadline=100
        bridge._run=Mock()
        bridge._probe=Mock(return_value={'site_timeout':True,'core_search_route':True,'query_input_count':0})
        with patch('wos_browser.time.monotonic',return_value=0), self.assertRaisesRegex(WOSBrowserStop,'不是文献|未记作未查询到'):
            bridge._ready_search()
        bridge._run.assert_not_called()

    def test_cleanup_still_exports_retained_evidence_after_debugger_auto_detaches(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge.session,bridge.tab_id='synthetic-session',1
        bridge.capturing=True
        bridge._run=Mock(side_effect=[WOSBrowserStop('already detached'),{},{}])
        bridge.close()
        self.assertEqual([call.args[0][:2] for call in bridge._run.call_args_list],
                         [['debug','stop'],['debug','export'],['session','stop']])
        self.assertTrue(all(call.kwargs.get('cleanup') for call in bridge._run.call_args_list))
        self.assertIsNone(bridge.session)

    def test_native_link_navigation_preserves_shared_reader_variants_and_no_full_reload(self):
        bridge=BrowserSkillWOS(self.base,'synthetic-profile',DEFAULTS['origin'],cli='synthetic-bsk')
        bridge.session,bridge.tab_id='synthetic-session',1
        bridge._observe=Mock()
        bridge._run=Mock(return_value={})
        bridge._open_record(self.url)
        args=bridge._run.call_args.args[0]
        self.assertEqual(args[0],'click')
        selector=args[-1]
        self.assertIn('a[href="/wos/woscc/full-record/WOS:000123456789012/"]',selector)
        self.assertIn('a[routerlink="/wos/woscc/full-record/WOS%3A000123456789012"]',selector)
        self.assertNotIn('navigate',args)
        self.assertEqual(bridge._observe.call_count,2)
        with self.assertRaises(WOSBrowserStop):
            bridge._validate_record(None)


if __name__=='__main__':
    unittest.main()
