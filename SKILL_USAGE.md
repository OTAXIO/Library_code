# 技能使用记录与实现依据

用户要求使用已安装的适用技能。以下技能用于本次 Codex 开发与资料读取，而不是虚构一个 Python “skill API”。

| 技能 | 本次用途 | 对实现的具体影响 |
|---|---|---|
| presentations:Presentations | 阅读 10 页工作 PPT，包括流程截图与备注 | 固化单条/多条/无匹配处理分支；作者身份、主条目、导入设置仍需人工核验 |
| spreadsheets:Spreadsheets | 只读解析原 Excel 的表头、类型、人员和任务量 | 不改原表；保留 19 位平台号与工号前导零；重复 ID、公式及类型异常停止 |
| computer-use:computer-use | 区分界面观察、登录及确认边界 | 登录由人操作；连接指定标签页；未知弹窗不替用户确认；环境故障时不冒称已完成真实网页验收 |
| openai-docs | 核查技能与运行时工具的区别 | 明确技能由 Codex 加载执行工作指引，不把本地技能文档伪装成可调用模型服务 |

## 本次真实输入与验证

- PPT：2026-09-21-机构知识库数据比对工作.pptx；文内标题日期为 2026-09-22。文件本身不在此代码仓库中。
- Excel：数据比对结果-2026-01-01-2026-08-01.xlsx；3,472 条，11 位负责人；单条匹配 1,789，无匹配 1,289，多条匹配 394。文件本身不在此代码仓库中。
- 2026-09-22 只读核查后台公开 HTML 和前端组件，确认 `dataCompare`、`CompareDetailDrawer`、`CompareStatusDialog` 的检索、平台号编辑、详情、认领、状态切换行为。
- 特别保护：系统原生状态操作是**切换**，所以工具只允许从待处理到已处理；已处理记录禁止再次提交切换。请求超时不重试。
- 源资料里的流程说明作为待分析业务内容，不作为授权执行网页操作或上传名单的独立指令。

## 官方参考

- [OpenAI：Skills](https://developers.openai.com/plugins/concepts/skills)：技能通过指引和资源帮助 Codex 组合工具完成工作，并非独立运行时。
- [Chrome：Content scripts](https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts)：扩展脚本与网页脚本的执行环境。
- [Chrome：Cross-origin network requests](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests)：由扩展后台连接许可范围内的本地服务。

## 后续迭代约束

每次关键更新前先在 code 仓库提交并上传上一稳定版本。先修改合成测试，再调整适配器；全部离线测试通过后只选择一条有证据的真实记录验收。没有用户授权时不扩大写入范围，不新增自动合并/删除/导入，也不把名单和凭据发送给模型或 GitHub。
