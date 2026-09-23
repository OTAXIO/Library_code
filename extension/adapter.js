/* Executed only on the paired page, in the page's JS world. Uses the existing
 * dataCompare component and its own UI methods; no tokens, axios, crypto, arbitrary
 * URLs, or generic API endpoints are exposed to the desktop process. */
async function runSACommand(command) {
  let submitted = false;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const stop = message => { throw new Error(message); };
  const check = () => {
    const u = new URL(location.href);
    if (u.hostname !== "admin.ir.lib.sjtu.edu.cn" || !["http:", "https:"].includes(u.protocol) ||
        u.hash.split("?")[0] !== "#/dataCompare/list") stop("已离开比对结果页或登录失效，请人工返回并重新核验");
    if (!Number.isFinite(command.expires) || Date.now() >= command.expires - 2500)
      stop("操作时限已到，停止后续动作；请核验已发出请求的结果");
  };
  const wait = async (predicate, label, timeout=35000) => {
    const deadline = Math.min(Date.now() + timeout, command.expires - 2500);
    while (Date.now() < deadline) {
      check();
      if (predicate()) return;
      await sleep(120);
    }
    stop(label + "超时。不要重复提交，先检查网页");
  };
  const visible = element => !!element && element.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden";
  const visibleAll = selector => [...document.querySelectorAll(selector)].filter(visible);
  const plain = value => {
    const el = document.createElement("textarea");
    el.innerHTML = String(value ?? "").replace(/<br\s*\/?\s*>/gi, "\n").replace(/<[^>]*>/g, "");
    return el.value;
  };
  // Chrome's extension messaging may reorder object keys. Compare content, not
  // serialization order, while preserving meaningful array order and types.
  const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  const equal = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
  const keys = ["id", "saLzkId", "itemId", "matchCount", "markStatus", "reason", "title", "titleValue",
    "doi", "doiValue", "wos", "wosValue", "claimStatus", "gh", "qr", "updateTime", "updateUsername", "remark"];
  const snap = row => {
    for (const key of ["id", "saLzkId", "itemId", "gh"]) {
      if (row[key] != null && typeof row[key] !== "string") stop("网页编号不是文本，可能已丢失精度；停止写入");
    }
    if (!["待处理", "已处理"].includes(row.markStatus)) stop("未知标记状态，需人工处理");
    if (!Number.isInteger(Number(row.matchCount)) || Number(row.matchCount) < 0) stop("匹配数量格式异常");
    const result = {};
    for (const key of keys) {
      const value = row[key] ?? "";
      if (!["string", "boolean", "number"].includes(typeof value)) stop("网页字段类型发生变化，停止操作");
      result[key] = value;
    }
    return result;
  };
  try {
    check();
    if (!["search", "open_metadata", "open_claim", "prepare_claim", "submit_claim", "link", "complete"].includes(command.action)) stop("不支持的命令");
    if (typeof command.sa_id !== "string" || !command.sa_id || command.sa_id.length > 160) stop("名单 ID 不合法");
    const roots = [...document.querySelectorAll("*")].map(el => el.__vue__).filter(Boolean);
    const found = new Set(), visited = new Set();
    const visit = vm => {
      if (!vm || visited.has(vm)) return;
      visited.add(vm);
      if (vm.$options?.name === "dataCompare") found.add(vm);
      for (const child of vm.$children || []) visit(child);
    };
    roots.forEach(visit);
    if (found.size !== 1) stop("未找到唯一的比对页面组件，可能未登录或页面已升级");
    const vm = [...found][0];
    if (!vm.searchForm || !Array.isArray(vm.tableData) || !vm.page || typeof vm.getData !== "function") stop("页面结构不兼容");
    const drawer = vm.$refs?.compareDetailDrawer;
    if (!drawer || typeof drawer.show !== "function") stop("未识别到比对详情组件");
    // Never close or overwrite a user editing form. Only our read-only drawer is closed.
    if (visibleAll(".el-dialog, .el-message-box").length) stop("网页存在未关闭的编辑/确认弹窗，请先人工处理并关闭");
    const previousClaim = drawer.$refs?.claimDetail;
    if (previousClaim?.drawer) {
      if (previousClaim.__saAssistant !== command.sa_id || previousClaim.loading ||
          previousClaim.tableData?.metadata?.author?.some(author => author.data))
        stop("认领窗口存在人工操作或未保存选择，请先人工处理并关闭");
      previousClaim.drawer = false;
      await vm.$nextTick();
      await wait(() => visibleAll(".el-drawer").length <= 1, "收起已查找的认领窗口", 5000);
      await wait(() => !drawer.dialogLoading, "等待详情刷新", 10000);
    }
    if (drawer.dialogVisible) {
      if (drawer.dialogLoading) stop("详情仍在加载，请等待后重查");
      drawer.dialogVisible = false;
      await vm.$nextTick();
      await wait(() => !visibleAll(".el-drawer").length, "关闭只读详情", 5000);
    }
    if (vm.loading) stop("列表仍在加载，请等待后重查");
    const fresh = async () => {
      check();
      const defaults = {saLzkId: command.sa_id, matchCount: null, claimStatus: "", markStatus: "",
        titleValue: "", doiValue: "", wosValue: "", qr: "", gh: "", reason: "", markType: null,
        updateUsername: "", createTimeRange: [], updateTimeRange: []};
      if (Object.keys(vm.searchForm).sort().join() !== Object.keys(defaults).sort().join()) stop("检索字段发生变化，需更新适配器");
      for (const [key, value] of Object.entries(defaults)) vm.searchForm[key] = value;
      vm.page.currentPage = 1;
      const previous = vm.tableData;
      // getData catches server errors internally; require a newly assigned result array.
      const request = vm.getData();
      if (!request || typeof request.then !== "function") stop("查询方法结构发生变化");
      let finished = false, failure;
      request.then(() => { finished = true; }, error => { failure = error; finished = true; });
      await wait(() => finished && !vm.loading, "查询记录");
      if (failure || vm.tableData === previous) stop("查询未取得新数据，可能登录失效或后台报错");
      if (Number(vm.page.total) !== 1 || vm.tableData.length !== 1) stop("名单 ID 未返回唯一结果，停止自动操作");
      const row = vm.tableData[0];
      if (row.saLzkId !== command.sa_id) stop("网页结果与名单 ID 不完全相同");
      snap(row);
      return row;
    };
    let row = await fresh();
    const before = snap(row);
    if (command.action !== "search") {
      if (!command.expected || Object.keys(command.expected).sort().join() !== [...keys].sort().join() ||
          !keys.every(key => before[key] === command.expected[key]))
        stop("网页数据在核验后已变化。请只读重查并再次核验，不能覆盖新数据");
    }
    const showDetail = async () => {
      check();
      vm.showItemId(row);
      await vm.$nextTick();
      await wait(() => drawer.dialogVisible && !drawer.dialogLoading, "读取对比详情");
      if (drawer.currentSaLzkId !== command.sa_id) stop("详情 ID 不一致");
      const values = Number(row.matchCount) === 1 ? drawer.compareData : drawer.saLzkCompareData;
      if (!Array.isArray(values) || !values.length) stop("详情为空，不能据此继续修改");
      return values.map(value => ({label: plain(value.label), sa: plain(value.compareLeftValue ?? value.leftValue),
        library: plain(value.compareRightValue ?? value.rightValue)}));
    };
    if (command.action === "search") {
      const comparison = await showDetail();
      return {ok: true, data: {row: before, comparison}};
    }
    if (["prepare_claim", "submit_claim"].includes(command.action)) {
      const submitting = command.action === "submit_claim";
      if (submitting && command.confirmed !== true) stop("缺少本条作者认领的人工确认");
      if (row.markStatus !== "待处理") stop("该记录已处理，不自动认领");
      const ids = String(row.itemId || "").replace(/^,/, "").split(",").filter(Boolean);
      if (Number(row.matchCount) !== 1 || ids.length !== 1) stop("自动认领需要唯一匹配条目");
      const comparison = await showDetail();
      const fields = comparison.filter(field => field.label === "认领状态");
      if (fields.length !== 1) stop("未找到唯一的认领状态字段");
      const saText = fields[0].sa.trim();
      const groups = [...saText.matchAll(/\(([^()]*)\)|（([^（）]*)）/g)];
      if (groups.length !== 1 || (saText.match(/[()（）]/g) || []).length !== 2) stop("SA 括号编号为空或不唯一");
      const staffId = (groups[0][1] || groups[0][2] || "").trim();
      if (!/^[0-9]{1,40}$/.test(staffId)) stop("SA 括号编号不是完整数字文本");
      if (command.sa_text !== saText || command.staff_id !== staffId) stop("SA 提交编号或姓名在读取后发生变化，请重新定位");
      if ((row.gh && row.gh !== staffId) || (command.roster_staff_id && command.roster_staff_id !== staffId))
        stop("SA 括号编号与名单/网页工号不一致，请人工核对");
      const claim = drawer.$refs?.claimDetail;
      if (!claim || !["getItemDetail", "handleSelect", "handleClaim", "getShows"].every(key => typeof claim[key] === "function"))
        stop("作者认领页面结构已变化，请更新扩展");
      const oldTable = claim.tableData;
      drawer.handleClaim();
      await wait(() => claim.drawer && !claim.loading && claim.tableData !== oldTable, "打开作者认领页");
      const guard = () => {
        check();
        if (!claim.drawer || claim.ids !== ids[0] || claim.tableData?.id !== ids[0] || claim.auth !== true ||
            drawer.currentSaLzkId !== command.sa_id || !Array.isArray(claim.tableData?.metadata?.author))
          stop("认领页面目标、权限或作者数据不一致");
      };
      guard();
      claim.__saAssistant = command.sa_id;
      claim.activeName = "author";
      await vm.$nextTick();
      const authorsSnapshot = () => claim.tableData.metadata.author.map((author, index) => {
        if (typeof author.fullname !== "string" || !author.fullname.trim() ||
            !/^[1-9][0-9]*$/.test(String(author.order)) ||
            (author.scholarId != null && typeof author.scholarId !== "string") ||
            (author.id != null && typeof author.id !== "string")) stop("作者行编号或署名格式未知");
        const relations = claim.tableData.itemAuthorRelationVOs?.[String(author.order)] ?? [];
        if (!Array.isArray(relations) || relations.some(relation => typeof relation.scholarId !== "string" ||
            !Number.isInteger(relation.status) || relation.status < 0 || relation.status > 10)) stop("作者认领关系格式未知");
        return {index, id: author.id ?? "", order: author.order, fullname: author.fullname,
          scholarId: author.scholarId ?? "", eligible: author.belongToCurrentInstitution !== false &&
            claim.getShows(author.institutionOrderNums) === true,
          relations: relations.map(r => ({scholarId: r.scholarId, status: r.status}))};
      });
      const authors = authorsSnapshot();
      if (new Set(authors.map(author => String(author.order))).size !== authors.length) stop("作者序号重复，不能自动认领");
      if (claim.tableData.metadata.author.some(author => author.data)) stop("已有未提交的学者选择，请人工核对");
      const available = authors.filter(author => author.eligible && !author.scholarId);
      if (!available.length) stop("没有可认领的作者行，请人工核验现有认领");
      // Selecting a row here only opens the search dialog. No scholar is bound
      // until the second, explicitly confirmed command revalidates everything.
      const target = submitting ? authors.find(author => author.index === command.author_index) : available[0];
      if (!target || !target.eligible || target.scholarId) stop("所选作者不可认领或已有认领，不能覆盖");
      claim.handleSelect(target.index, claim.index, claim.tableData.metadata.author[target.index]);
      await vm.$nextTick();
      const modal = claim.$refs?.authorModal;
      if (!modal || !["getScholarData", "sureAuthor"].every(key => typeof modal[key] === "function") ||
          !modal.dialogModalVisible || modal.authorIndex !== target.index || !modal.queryForm)
        stop("选择学者窗口结构不兼容");
      const pickerDialogs = visibleAll(".el-dialog").filter(element =>
        modal.$el && (modal.$el === element || modal.$el.contains(element)));
      if (pickerDialogs.length !== 1) stop("无法唯一识别程序打开的选择作者窗口，请人工处理");
      const pickerDialog = pickerDialogs[0];
      const waitForPickerClose = async () => {
        await wait(() => {
          guard();
          if (modal.dialogModalVisible) stop("选择作者窗口被重新打开，请人工处理");
          // Only our known dialog may be fading out. Never wait out or dismiss
          // an unrelated modal, which could need the user's decision.
          if (claim.activeName !== "author" || visibleAll(".el-dialog, .el-message-box").some(element => element !== pickerDialog))
            stop("网页出现其他操作窗口，请人工处理");
          return !visible(pickerDialog);
        }, "等待选择作者窗口关闭", 5000);
      };
      // showDialog starts an initial name search. Let it finish before changing
      // filters so its late response cannot masquerade as the ID lookup.
      await wait(() => !modal.loading, "等待初始人员查询");
      guard();
      const allowedFilters = ["id", "institutionId", "wno", "typeIdentity", "name", "scholarId"];
      if (Object.keys(modal.queryForm).some(key => !allowedFilters.includes(key))) stop("人员查询字段已变化");
      modal.queryForm = {id: claim.tableData.metadata.author[target.index].id,
        institutionId: "", wno: staffId, typeIdentity: "", name: ""};
      modal.page = {current: 1, size: 10};
      await vm.$nextTick();
      const oldPeople = modal.tableData;
      const request = modal.getScholarData();
      if (!request || typeof request.then !== "function") stop("人员查询方法不兼容");
      let finished = false, failure;
      request.then(() => {finished = true;}, error => {failure = error; finished = true;});
      await wait(() => finished && !modal.loading, "按工号/学号查询");
      guard();
      if (failure || modal.tableData === oldPeople || !Array.isArray(modal.tableData)) stop("人员查询没有返回新数据");
      if (Number(modal.total) !== 1 || modal.tableData.length !== 1) stop("工号/学号未返回唯一人员，请人工处理");
      const person = modal.tableData[0];
      if (typeof person.wno !== "string" || person.wno !== staffId || typeof person.id !== "string" || !person.id)
        stop("查询人员的工号/学号不完全一致，不能认领");
      if (person.aliases != null && !Array.isArray(person.aliases)) stop("学者别名格式未知");
      const names = [person.nameCn, person.nameEn, ...(person.aliases || []).map(alias => alias.nameAlias)]
        .filter(name => typeof name === "string" && name.trim()).map(name => name.trim());
      const name = person.nameCn || person.nameEn;
      if (typeof name !== "string" || !name.trim()) stop("人员姓名为空");
      if (authors.some(author => author.scholarId === person.id || author.relations.some(r => r.scholarId === person.id && r.status >= 6)))
        stop("该人员已存在认领关系，请人工核验，不重复提交");
      if (JSON.stringify(authorsSnapshot()) !== JSON.stringify(authors)) stop("查找期间作者数据发生变化");
      const prepared = {item_id: ids[0], staff_id: staffId, sa_text: saText,
        person: {id: person.id, wno: person.wno, name, names: [...new Set(names)]}, authors};
      if (!submitting) {
        modal.dialogModalVisible = false;
        await vm.$nextTick();
        await waitForPickerClose();
        const exact = available.filter(author => names.includes(author.fullname.trim()));
        return {ok: true, data: {row: before, comparison, prepared,
          suggested_index: exact.length === 1 ? exact[0].index : null}};
      }
      if (!equal(prepared, command.prepared)) stop("人员或作者数据已变化，请重新查找并确认");
      if (target.relations.some(r => r.scholarId === person.id && r.status >= 1 && r.status <= 5))
        stop("该作者已有非本人作品记录，需人工核查，不能自动覆盖");
      if (target.relations.some(r => r.status >= 6)) stop("该作者已有其他有效认领关系，不能自动覆盖");
      if (!modal.dialogModalVisible || modal.authorIndex !== target.index || modal.queryForm.wno !== staffId ||
          modal.queryForm.name !== "" || modal.queryForm.institutionId !== "" || modal.queryForm.typeIdentity !== "")
        stop("人员查询窗口被修改，请重新查找");
      modal.sureAuthor(person);
      await vm.$nextTick();
      await waitForPickerClose();
      guard();
      const selected = claim.tableData.metadata.author[target.index];
      if (modal.dialogModalVisible || selected.data?.scholarId !== person.id || selected.data?.wno !== staffId ||
          selected.data?.name !== name || JSON.stringify(authorsSnapshot()) !== JSON.stringify(authors))
        stop("选择人员后的作者行校验失败");
      if (claim.activeName !== "author" || visibleAll(".el-dialog, .el-message-box").length)
        stop("网页出现其他操作窗口，请人工处理");
      const beforeWrite = claim.tableData;
      submitted = true;
      claim.__saAssistant = null; // Never silently close an uncertain write.
      claim.handleClaim(selected); // Exactly one existing UI submit, never batch.
      await wait(() => !claim.loading && claim.tableData !== beforeWrite, "认领结果回读");
      guard();
      const after = authorsSnapshot();
      const confirmed = after.filter(author => author.order === target.order && author.fullname === target.fullname && author.scholarId === person.id);
      if (confirmed.length !== 1 || after.filter(author => author.scholarId === person.id).length !== 1)
        stop("认领结果未匹配目标作者与学者，需人工核验");
      return {ok: true, data: {row: before, claimed: true, verified: true,
        staff_id: staffId, scholar_id: person.id, author: target.fullname, order: target.order}};
    }
    if (["open_metadata", "open_claim"].includes(command.action)) {
      const ids = String(row.itemId || "").replace(/^,/, "").split(",").filter(Boolean);
      if (Number(row.matchCount) !== 1 || ids.length !== 1) stop("编辑或认领需要唯一匹配条目；多条匹配请人工选择");
      await showDetail();
      check();
      if (command.action === "open_metadata") drawer.editItem(ids[0]);
      else drawer.handleClaim();
      return {ok: true, data: {row: before, manual: true}};
    }
    const presetNotes = ["已认领", "DOI和WOSID SA未提交", "通讯作者修正"];
    const note = typeof command.note === "string" ? command.note.trim() : "";
    const noteParts = note.split(/[；;]/).map(part => part.trim()).filter(Boolean);
    const knownShortNote = noteParts.length > 0 && noteParts.every(part => presetNotes.includes(part));
    if (!command.reviewed || !note || (note.length < 6 && !knownShortNote) || note.length > 2000)
      stop("缺少人工核验及处理备注");
    if (row.markStatus !== "待处理") stop("记录已经处理，禁止再次切换状态");
    const selected = presetNotes.filter(preset => note.includes(preset));
    if (selected.length) {
      const comparison = await showDetail();
      const field = (rows, label, side) => {
        if (!Array.isArray(rows)) stop("缺少上次核验的详情，请重新查询");
        const matches = rows.filter(item => item.label === label);
        if (matches.length !== 1 || typeof matches[0][side] !== "string") stop("备注所需详情字段不完整：" + label);
        return matches[0][side].trim();
      };
      const needed = new Set();
      if (selected.includes("已认领")) {
        needed.add("认领状态"); needed.add("作者信息");
        if (Number(row.matchCount) !== 1 || field(comparison, "认领状态", "library") !== "已认领")
          stop("尚未回读到唯一条目已认领，不能提交“已认领”备注");
      }
      if (selected.includes("DOI和WOSID SA未提交")) {
        needed.add("DOI"); needed.add("WOS记录号");
        if (field(comparison, "DOI", "sa") || field(comparison, "WOS记录号", "sa"))
          stop("SA 的 DOI 和 WOS ID 并非同时为空，不能提交双缺失备注");
      }
      if (selected.includes("通讯作者修正")) {
        needed.add("作者信息");
        if (!/是否通讯作者\s*[：:]\s*[是否](?:\s|$)/.test(field(comparison, "作者信息", "library")))
          stop("本库通讯作者标记未知，请先修正并重新核验");
      }
      for (const label of needed) for (const side of ["sa", "library"]) {
        if (field(comparison, label, side) !== field(command.expected_comparison, label, side))
          stop("备注相关详情在确认后发生变化，请重新查询和核验");
      }
      drawer.dialogVisible = false;
      await vm.$nextTick();
      await wait(() => !visibleAll(".el-drawer").length, "关闭只读详情", 5000);
    }
    if (command.action === "complete") {
      const modal = vm.$refs.compareStatusDialog;
      if (!modal || typeof modal.show !== "function" || typeof modal.handleConfirm !== "function") stop("状态弹窗结构不兼容");
      vm.handleEdit(row);
      await vm.$nextTick();
      if (!modal.dialogVisible || modal.currentRow?.saLzkId !== command.sa_id || modal.currentRow.markStatus !== "待处理") stop("状态弹窗的目标不一致");
      modal.editForm.remark = command.note.trim();
      await vm.$nextTick();
      check();
      submitted = true;
      modal.handleConfirm();
      await wait(() => !modal.dialogVisible, "提交处理状态");
    } else {
      if (!/^[0-9]{1,40}$/.test(command.item_id || "")) stop("平台唯一号必须是完整数字文本，不支持多号合并");
      if (String(row.itemId || "").replace(/^,/, "") === command.item_id) stop("平台唯一号没有变化，无需重复写入");
      vm.handleEditItem(row);
      await wait(() => visibleAll(".el-message-box").length === 1, "平台号编辑框", 6000);
      const modal = visibleAll(".el-message-box")[0];
      if (!modal.textContent.includes("请输入匹配条目的平台唯一号")) stop("出现未识别的弹窗，不填写或确认");
      const inputs = [...modal.querySelectorAll("input")].filter(visible);
      const buttons = [...modal.querySelectorAll("button")].filter(button => visible(button) && button.textContent.trim() === "确定");
      if (inputs.length !== 1 || buttons.length !== 1 || buttons[0].disabled) stop("平台号弹窗控件不唯一");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(inputs[0], command.item_id);
      inputs[0].dispatchEvent(new Event("input", {bubbles: true}));
      await vm.$nextTick();
      check();
      const oldData = vm.tableData;
      submitted = true;
      buttons[0].click();
      await wait(() => !visible(modal) && vm.tableData !== oldData && !vm.loading, "保存平台号");
    }
    // A successful toast alone is insufficient. Read back the actual record.
    await wait(() => !vm.loading, "等待列表刷新");
    row = await fresh();
    const after = snap(row);
    if (command.action === "complete") {
      if (row.markStatus !== "已处理" || String(row.remark ?? "").trim() !== command.note.trim())
        stop("状态或备注回读不一致，写入结果需人工核验");
    } else if (String(row.itemId || "").replace(/^,/, "") !== command.item_id) stop("平台唯一号回读不一致，需人工核验");
    return {ok: true, data: {row: after, verified: true}};
  } catch (error) {
    return {ok: false, error: (submitted ? "【已发出写入，禁止自动重试】" : "【已暂停】") + error.message};
  }
}
if (typeof module !== "undefined") module.exports = {runSACommand};
