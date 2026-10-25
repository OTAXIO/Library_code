"""Separate an acknowledged per-record read failure from unknown commands.

Advancing never replays a write or discards its journal. Transport timeouts,
login/session errors, changed rosters and unfinished commands remain batch stops.
"""
import re
import time
from bridge import BrowserRejected
from core import SafetyStop
from roster_annotations import annotate_roster

READ_ACTIONS={'status','search','import_excel_check','import_excel_scan','import_excel_probe',
              'import_excel_resolve','import_check','import_scan','import_resolve','import_capabilities','wos_diagnose'}
GLOBAL=re.compile(r'登录|验证码|会话|未连接|断开|通信|命令超时|仍在执行|正在执行|回执|未返回结果|请求失败|HTTP|TLS|'
                  r'网络|限流|频率|请.*(?:重载|加载.*扩展)|协议|浏览器结果格式|连接服务',re.I)
MISSING=re.compile(r'(?:未找到本条导入批次|未找到原批次 ID)（完整扫描 \d+ 条，同说明 \d+ 条）；原提交结果不明，保留断点，未重传$')


class UnconfirmedBatch(SafetyStop):
    """A completed original-batch scan found none; NOT permission to upload."""


class RecordReadback:
    def __init__(self):self.action=None;self.returned=False

    def call(self, bridge, action, payload, timeout):
        self.action=action;self.returned=False
        try:answer=bridge.call(action,payload,timeout=timeout)
        except BrowserRejected as error:
            self.returned=True
            # A close animation can outlive the adapter's short wait. Only a
            # completed, explicitly pre-write rejection of this read permits
            # one fresh read. Never replay a delivered write or an unknown ACK.
            if (action != 'search' or error.action != action or
                    not re.search(r'关闭只读详情超时[。.]本次命令未提交写入', str(error)) or
                    GLOBAL.search(str(error)) or
                    isinstance(getattr(bridge,'pending',None),dict) or
                    getattr(bridge,'online',True) is False):
                raise
            time.sleep(3)
            self.returned=False
            try:answer=bridge.call(action,payload,timeout=timeout)
            except BrowserRejected:
                self.returned=True
                raise
        self.returned=True
        return answer

    def isolated_note(self, error, retry_skipped=False):
        message=str(error)
        if isinstance(error,UnconfirmedBatch) and self.returned and self.action in {'import_scan','import_excel_scan'}:
            return '批次待核'
        # A complete negative read is not a new failed submission. Its original
        # upload/import intent remains intact and cannot be retried as new.
        if (isinstance(error,BrowserRejected) and error.action==self.action and self.returned
                and self.action in {'import_check','import_excel_check'} and MISSING.search(message)):
            return '批次待核'
        if '此条存在 WOS 写入断点' in message or '此条存在 Excel 入库写入断点' in message:
            return '原流程待核'
        if (not retry_skipped or not self.returned or self.action not in READ_ACTIONS or GLOBAL.search(message)):
            return None
        if isinstance(error,BrowserRejected) and error.action!=self.action:return None
        if '批次' in message:return '批次待核'
        if '认领' in message or '作者' in message:return '作者待核'
        if 'DOI' in message or '题名' in message or '本库' in message:return '文献匹配待核'
        if '窗口' in message or '向导' in message:return '操作窗口待核'
        return '本条待核'


def park_record(roster, record, note, bridge, stop):
    """Only a local note; never changes 1/2 or any upload/import/claim state."""
    roster.assert_unchanged()
    if stop.is_set():raise SafetyStop('已请求暂停；未执行项目保持原样。')
    if isinstance(getattr(bridge,'pending',None),dict):raise SafetyStop('浏览器命令仍在执行，不能继续下一条。')
    if getattr(bridge,'online',True) is False:raise SafetyStop('浏览器通信已断开，保留断点。')
    current=next(r for r in roster.records if r.sa_id==record.sa_id)
    return annotate_roster(roster,notes={record.sa_id:note}).roster if not current.done else roster
