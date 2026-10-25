import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from maintenance.run_owned_tasks import QuotaStop, review_reason


class OwnedTaskTests(unittest.TestCase):
    def test_event_wait_checks_quota_while_existing_pipeline_waits(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'quota.json'
            path.write_text(json.dumps({'remaining_percent':60,'observed_at':100}),encoding='utf8')
            stop=QuotaStop(path,now=lambda:100)
            self.assertFalse(stop.wait(0));self.assertFalse(stop.wait(.005))
            def lower():
                time.sleep(.02)
                path.write_text(json.dumps({'remaining_percent':49,'observed_at':100}),encoding='utf8')
            thread=threading.Thread(target=lower);thread.start()
            self.assertTrue(stop.wait(.5));thread.join()
            self.assertIn('50%',stop.reason)
    def test_quota_threshold_freshness_and_manual_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'quota.json';pause=Path(folder)/'pause'
            for remaining,observed,expected in [(65,100,False),(50,100,True),(49.9,100,True),(65,-90,True),(65,101,True),(True,100,True)]:
                path.write_text(json.dumps({'remaining_percent':remaining,'observed_at':observed}),encoding='utf8')
                self.assertEqual(QuotaStop(path,now=lambda:100).is_set(),expected)
            path.write_text('{broken',encoding='utf8');self.assertTrue(QuotaStop(path).is_set())
            path.write_text(json.dumps({'remaining_percent':65,'observed_at':100}),encoding='utf8')
            pause.touch();self.assertTrue(QuotaStop(path,pause,now=lambda:100).is_set())

    def test_red_reason_never_confuses_transport_failure_with_paper_failure(self):
        for message in ['HTTP 429','TLS 连接异常','命令超时','浏览器未连接','需要重新登录','上次认领回执未确认','操作窗口待核；保留原断点','网页有未关闭的窗口，请人工处理后再预检']:
            self.assertEqual(review_reason(message),'')
        self.assertEqual(review_reason('本库作者冲突（未导入）'),'本库作者冲突')
        self.assertEqual(review_reason('通讯作者待核验；保留原断点，已继续后续条目。'),'通讯作者待核验')
        self.assertEqual(review_reason('匹配数非 0，已跳过；未修改备注。'),'')
        self.assertEqual(review_reason('文献题名待核验，未自动认领或结案。'),'题名待核验')


if __name__=='__main__':unittest.main()
