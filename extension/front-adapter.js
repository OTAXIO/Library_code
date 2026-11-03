/* Read-only search on the public library frontend (www.ir.lib.sjtu.edu.cn).
 * The site encrypts its retrieval requests, so this never calls its API. It
 * reuses the mounted simpleSearch component: the header search box emits
 * "handleSearch", and only a successful response emits "searchItemList" with
 * the decrypted page. A failed request emits nothing, so "0 results" is only
 * reported from that event, never from an empty or stale list. No clicks, no
 * navigation, no writes; tokens and cookies are never read or returned. */
async function runFrontCommand(command) {
  const fail = text => {throw new Error(text);};
  const FIELDS = ["doi", "wosId", "title"];
  const PAGE_SIZE = 50;
  const plain = value => {
    // Highlighting wraps hits in <span class='highlight'>; decode entities too.
    const box = document.createElement("textarea");
    box.innerHTML = String(value ?? "").replace(/<[^>]*>/g, "");
    return box.value.replace(/\s+/g, " ").trim();
  };
  const first = value => Array.isArray(value) ? (value.length === 1 ? plain(value[0]) : value.length ? null : "") : plain(value);
  let off = () => {};
  try {
    const url = new URL(location.href);
    if (!["http:", "https:"].includes(url.protocol) || url.hostname !== "www.ir.lib.sjtu.edu.cn" ||
        url.username || url.password) fail("不是交大机构库前台");
    if (command?.action !== "front_search") fail("不支持的前台命令");
    if (!Number.isFinite(command.expires) || Date.now() >= command.expires - 4000) fail("前台检索命令已过期");
    if (!FIELDS.includes(command.field)) fail("前台检索字段不受支持");
    const text = typeof command.text === "string" ? command.text.trim() : "";
    if (!text || text.length > 600 || /[\x00-\x1f]/.test(text)) fail("前台检索词无效");
    if (url.pathname !== "/simpleSearch") fail("前台页不在检索结果页，请重新绑定");
    const wait = async (fn, label) => {
      while (Date.now() < command.expires - 5000) {
        const value = fn();
        if (value) return value;
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      fail(label + "超时，未确认检索结果");
    };
    const list = await wait(() => {
      const vm = document.querySelector(".article-box")?.__vue__;
      const page = vm?.$parent;
      return vm && typeof page?.getItemData === "function" && page.form && page.page && vm.$EventBus &&
        page.loading === false ? vm : null;
    }, "前台检索页加载");
    const page = list.$parent, bus = list.$EventBus;
    // Signed-in only: an anonymous frontend redirects details to login and
    // may hide items. The token itself is not read beyond its presence.
    if (!list.$store?.state?.token) fail("前台未登录，请在该标签页登录后重新检索");
    const institution = list.$store.state.mainId;
    if (typeof institution !== "string" || !/^\d{1,40}$/.test(institution)) fail("前台本库机构未识别");
    let answer = null;
    const handler = (data, _aggregations, query) => {
      if (query?.field === command.field && query?.text === text) answer = {data, query};
    };
    bus.$on("searchItemList", handler);
    off = () => bus.$off("searchItemList", handler);
    page.page = {...page.page, current: 1, size: PAGE_SIZE};
    bus.$emit("handleSearch", {field: command.field, text});
    await wait(() => answer && page.loading === false, "前台检索");
    off();
    const result = answer.data?.page;
    if (answer.query.institutionId !== institution) fail("前台检索未限定在本库");
    if (!result || !Number.isInteger(result.total) || result.total < 0 || !Array.isArray(result.records) ||
        result.records.length > PAGE_SIZE || result.records.length > result.total)
      fail("前台检索结果结构未识别");
    const items = result.records.map(record => {
      const meta = record?.metadata;
      if (!record || typeof record.id !== "string" || !/^\d{1,40}$/.test(record.id) || !meta || typeof meta !== "object")
        fail("前台成果条目结构未识别");
      const title = first(meta.title), doi = first(meta.doi), wos = first(meta.wosId);
      if (title === null || doi === null || wos === null) fail("前台成果有多个题名或编号，未自动判断");
      return {id: record.id, title, doi, wos, model: typeof record.modelName === "string" ? record.modelName : "",
        deleted: record.isDeleted !== 0};
    });
    return {ok: true, data: {front_search_protocol: 1, verified: true, field: command.field, text,
      total: result.total, complete: result.total <= items.length, items}};
  } catch (error) {
    off();
    return {ok: false, error: "【已暂停】" + error.message};
  }
}
if (typeof module !== "undefined") module.exports = {runFrontCommand};
