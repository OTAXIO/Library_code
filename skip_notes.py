"""Short roster labels; full diagnostics belong in private reports and logs.

These are presentation labels, not new evidence. Only recognized old generated
notes are eligible for migration; arbitrary human notes are left untouched.
"""
import re

EXACT = {
    "wos未查询到": "wos未收录",
    "wos未收录": "wos未收录",
    "非交大": "非交大",
    "交大署名待核验": "交大署名待核验",
    "涉及作者或第一单位判断，暂不处理": "作者/单位待核验",
    "已补录/关联，作者或单位详情待核验": "作者/单位待核验",
    "已补录/关联，尚未认领，暂不结案": "认领待核验",
    "已补录/关联，作者编号待核验": "作者编号待核验",
    "已补录/关联，作者身份待核验": "作者身份待核验",
    "已补录/关联，涉及作者角色判断，暂不处理": "作者角色待核验",
    "已补录/关联，第一单位待核验，暂不处理": "第一单位待核验",
    "后台匹配状态已变化，待核验": "后台匹配已变化",
    "WOS 文献身份待核验": "文献身份待核验",
    "已补录/关联，人员或署名不能唯一认领": "认领对象待核验",
    "存在字段差异，打开编辑页；不凭差异文字覆盖元数据。": "元数据待核验",
    "SA 的 DOI 和 WOSID 并非同时为空，本条保留人工核验。": "SA标识待核验",
    "人员姓名与论文署名不能唯一对应。": "认领对象待核验",
    "网页待处理原因或匹配数与名单不一致，本条保留人工核验。": "名单与网页不一致",
    "网页没有可用的作者认领窗口。": "无认领窗口",
    "仅处理单匹配的作者认领或 DOI/WOSID 双缺失认领；本条保留人工核验。": "不适用自动认领",
}
SHORT = frozenset(EXACT.values()) | {"WOS控件未识别", "WOS检索超时", "WOS页面待核验",
    "WOS单篇页待核验", "WOS检索页待核验", "WOS登录/验证", "WOS结果待核验",
    "WOS导出待核验", "浏览器连接异常", "命令回执待核验", "待人工核验",
    "题名待核验", "标识待核验"}


def known_skip_note(note):
    """Return a recognized short label, or empty for a note we cannot interpret.

    In particular, a page timeout, missing link, or ambiguous result never becomes
    wos未收录. Nor does incomplete affiliation evidence become 非交大.
    """
    if not isinstance(note, str):
        return ""
    value = note.strip()
    if value in EXACT:
        return EXACT[value]
    if value in SHORT:
        return value
    if value.startswith("WOS 文献标识或归属证据待核验："):
        return "文献身份待核验"
    if value.startswith("人员或署名无法自动确认："):
        return "无认领窗口" if "没有可认领的作者行" in value else "认领对象待核验"
    if not value.startswith("WOS 下载未完成："):
        return ""
    message = value[len("WOS 下载未完成："):]
    # Old reports are admissible only as descriptions of their recorded problem.
    # They do not authorize retrying a write or changing workflow flags.
    message = re.sub(r"^(?:\[扩展 [0-9.]+\]\s*)?(?:\[WOS 已暂停\]\s*)?", "", message)
    if re.match(r"WOS\s*未找到记录(?:[；;。]|$)", message):
        return "wos未收录"
    rules = (
        ("文献检索按钮未唯一识别", "WOS控件未识别"),
        ("检索字段选择器未唯一识别", "WOS控件未识别"),
        ("WOS 检索结果超时", "WOS检索超时"),
        ("加载结果超时", "WOS检索超时"),
        ("未处于 WOS 核心合集单篇完整记录页", "WOS单篇页待核验"),
        ("请在 WOS 核心合集的文献页面操作", "WOS单篇页待核验"),
        ("请在 WOS 核心合集的文献检索页登录", "WOS检索页待核验"),
        ("登录或验证码需要人工处理", "WOS登录/验证"),
        ("WOS 网站报错或存在操作窗口", "WOS页面待核验"),
        ("WOS 结果不是可确认的唯一记录", "WOS结果待核验"),
        ("浏览器未连接", "浏览器连接异常"),
        ("命令超时", "命令回执待核验"),
    )
    for prefix, short in rules:
        if message.startswith(prefix):
            return short
    return ""


def brief_skip_note(note):
    """Label a newly recorded skip without copying a full exception into Excel."""
    return known_skip_note(note) or "待人工核验"
