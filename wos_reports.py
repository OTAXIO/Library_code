"""Private, complete batch reports; independent from the rolling 100-operation log."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import re
import uuid


def _text(value):
    # Treat titles and diagnostics as data, including text resembling Markdown.
    text = str(value if value is not None else "—")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}\b|Bearer\s+[A-Za-z0-9._~-]+", "[已隐藏凭据]", text)
    return text.replace("```", "｀｀｀")


def save_download_report(result, owner, scope, root):
    """Write an allowlisted UTF-8 report and return its unique absolute path.

    Never serialize the whole result: it can carry an internal roster object or
    unrelated bridge fields. All paper outcomes are retained without row limits.
    """
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone(timedelta(hours=8)))
    path = root / f"WOS-{now:%Y%m%d-%H%M%S-%f}-{uuid.uuid4().hex[:6]}.md"
    exported = result.get("exported", [])
    unconfirmed = result.get("unconfirmed", [])
    failed = result.get("failed", {})
    attempted = result.get("attempted", len(exported) + len(unconfirmed) + len(failed))
    remaining = result.get("remaining", 0)
    total = result.get("total", attempted + remaining)
    unavailable = result.get("not_exported", sum(bool(v.get("per_record")) for v in failed.values()))
    problems = result.get("session_failures", len(failed) - unavailable)
    lines = ["# WOS 下载结果", "", "```text",
             f"时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）",
             f"负责人：{_text(owner)}",
             f"浏览器插件：{_text(result.get('extension_version', '未记录'))}",
             f"范围：{'已跳过论文（是否识别为 2）' if scope == 'skipped' else '待补论文（不含跳过项）'}",
             f"论文总数：{total}；已尝试：{attempted}；尚未执行：{remaining}",
             f"文件已采纳：{len(exported)}；身份待核验：{len(unconfirmed)}",
             f"WOS 无可用记录：{unavailable}；页面/会话问题：{problems}",
             f"已回写跳过原因及是否识别=2：{result.get('skipped_saved', 0)} 行；未执行项不改状态",
             f"通信不可继续：{'是' if result.get('disconnected') else '否'}；本轮提前停止：{'是' if result.get('stopped') else '否'}",
             f"TXT 目录：{_text(result.get('inbox', '未提供'))}", "```", "",
             "下载文件不等于已入库，也不代表 SA 任务完成。无结果或多个结果不等于论文未发表。", ""]

    def section(title, records):
        lines.extend([f"## {title}", ""])
        if not records:
            lines.extend(["无。", ""])
            return
        for number, entry in enumerate(records, 1):
            lines.extend([f"### {number}. 原表第 {_text(entry.get('row'))} 行", "", "```text"])
            for key, label in (("sa_id", "名单 ID"), ("title", "题名"), ("doi", "DOI"),
                               ("file", "TXT 文件"), ("archive", "待核验存档"),
                               ("sha256", "SHA256"), ("error", "原因")):
                if key in entry:
                    lines.append(f"{label}：{_text(entry[key])}")
            lines.extend(["```", ""])

    section("已采纳的 TXT 文件", exported)
    section("已下载但身份待核验", unconfirmed)
    outcomes = [{**value, "sa_id": sa_id} for sa_id, value in failed.items()]
    section("WOS 无可用记录", [entry for entry in outcomes if entry.get("per_record")])
    section("页面或会话问题", [entry for entry in outcomes if not entry.get("per_record")])
    for key, label in (("source_error", "数据来源回写未完成"),
                       ("workflow_error", "跳过状态及原因回写未完成"),
                       ("classification_rebind_error", "分类索引刷新未完成")):
        if result.get(key):
            lines.extend([f"## {label}", "", "```text", _text(result[key]), "```", ""])
    lines.extend(["## 下一步", "", "在桌面“WOS 导入”检查文件。核实文献与本库缺失后，确认上传入库。",
                  "身份待核验项可以在导入页选择论文后进入单条核验，不需要重新检索。",
                  "通信中断时先核对浏览器状态，恢复连接后再继续。不要对结果不明的入库操作重复提交。", ""])
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
        stream.flush()
        os.fsync(stream.fileno())
    return path
