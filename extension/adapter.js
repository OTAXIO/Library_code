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
    if (!["search", "open_metadata", "open_claim", "link", "complete"].includes(command.action)) stop("不支持的命令");
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
