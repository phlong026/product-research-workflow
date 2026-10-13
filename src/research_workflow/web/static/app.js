"use strict";
const $ = (sel, el = document) => el.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};
const store = { get token() { try { return sessionStorage.getItem("token") || ""; } catch { return ""; } },
                set token(v) { try { v ? sessionStorage.setItem("token", v) : sessionStorage.removeItem("token"); } catch {} } };

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { Authorization: "Bearer " + store.token, ...(opts.headers || {}) } });
  if (res.status === 401) { store.token = ""; render(); throw new Error("登录已失效"); }
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.blob();
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body));
  return body;
}
const post = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
const flash = (el, msg, bad = false) => { el.textContent = msg; el.className = bad ? "err" : "muted"; };

let state = { user: null, project: null, timer: null };

async function render() {
  clearInterval(state.timer);
  const app = $("#app"); app.replaceChildren();
  $("#logout").hidden = !store.token;
  if (!store.token) return renderLogin(app);
  try { state.user = await api("/api/me"); } catch { return renderLogin(app); }
  $("#who").textContent = `${state.user.name}（${state.user.role === "admin" ? "管理员" : "成员"}）`;
  $("#logout").onclick = () => { store.token = ""; state.project = null; render(); };
  state.project ? renderProject(app) : renderProjects(app);
}

function renderLogin(app) {
  const input = h("input", { type: "password", placeholder: "粘贴管理员发给你的访问令牌", autocomplete: "off" });
  const msg = h("div", { class: "muted" });
  app.append(h("section", {}, h("h2", {}, "登录"), input,
    h("p", {}, h("button", { onclick: async () => { store.token = input.value.trim(); try { await api("/api/me"); render(); } catch (e) { store.token = ""; flash(msg, e.message, true); } } }, "进入")), msg));
}

async function renderProjects(app) {
  const list = h("div", {}, "加载中…");
  const product = h("input", { type: "text", placeholder: "产品名，如 制冰机" });
  const market = h("input", { type: "text", value: "US" });
  const keyword = h("input", { type: "text", placeholder: "主关键词（可选）" });
  const msg = h("div", {});
  app.append(
    h("section", {}, h("h2", {}, "我的研究项目"), list),
    h("section", {}, h("h2", {}, "新建项目"),
      h("div", { class: "row" }, h("div", {}, h("label", {}, "产品"), product), h("div", {}, h("label", {}, "站点"), market), h("div", {}, h("label", {}, "主关键词"), keyword)),
      h("p", {}, h("button", { onclick: async () => { try { const r = await post("/api/projects", { product: product.value, marketplace: market.value, primary_keyword: keyword.value || null }); state.project = r.project_id; render(); } catch (e) { flash(msg, e.message, true); } } }, "创建")), msg),
    state.user.role === "admin" ? adminPanel() : null);
  try {
    const projects = await api("/api/projects");
    list.replaceChildren(projects.length ? h("table", {}, h("tr", {}, ["产品", "站点", "状态", "当前步骤", ""].map(t => h("th", {}, t))),
      projects.map(p => h("tr", {}, h("td", {}, p.product), h("td", {}, p.marketplace), h("td", {}, p.status), h("td", {}, p.current_step || ""),
        h("td", {}, h("button", { class: "secondary", onclick: () => { state.project = p.project_id; render(); } }, "打开"))))) : h("p", { class: "muted" }, "还没有项目"));
  } catch (e) { list.textContent = e.message; }
}

function adminPanel() {
  const name = h("input", { type: "text", placeholder: "用户名" });
  const role = h("select", {}, h("option", { value: "member" }, "成员"), h("option", { value: "admin" }, "管理员"));
  const out = h("pre", { hidden: true }); const msg = h("div", {});
  return h("section", {}, h("h2", {}, "用户管理（管理员）"),
    h("div", { class: "row" }, h("div", {}, h("label", {}, "用户名"), name), h("div", {}, h("label", {}, "角色"), role)),
    h("p", {}, h("button", { onclick: async () => { try { const u = await post("/api/users", { name: name.value, role: role.value }); out.hidden = false; out.textContent = `用户 ${u.name} 的令牌（只显示一次）:\n${u.token}`; flash(msg, "已创建"); } catch (e) { flash(msg, e.message, true); } } }, "创建用户并生成令牌")), out, msg);
}

const ACTIONS = [
  { id: "ingest", title: "1. 导入并清洗月度数据", fields: [
      { n: "file", t: "files", l: "已上传的月度数据文件（可多选）" }, { n: "month", t: "text", l: "月份 YYYY-MM（单文件时）" },
      { n: "node", t: "list", l: "类目节点（可选，每行一个；多节点同月自动合并）" }, { n: "top_n", t: "int", l: "保留 TopN", v: 100 } ] },
  { id: "enrich", title: "2. 商品补齐（表2：卖点/卖家所属地）", fields: [{ n: "details_file", t: "file", l: "补齐明细文件（也用于上传修改后的表2）" }] },
  { id: "classify", title: "3. 商品分类", fields: [
      { n: "label", t: "list", l: "分类标签，每行 key=中文名" }, { n: "rule", t: "list", l: "关键词规则（规则分类用），每行 key=词1,词2" },
      { n: "provider", t: "select", l: "分类方式", o: ["rules", "llm"] } ] },
  { id: "analyze", title: "4. 市场/竞争/新品分析", fields: [
      { n: "pair", t: "list", l: "份额分解对，每行 YYYY-MM:YYYY-MM" }, { n: "new_products_file", t: "file", l: "新品跟踪表（可选）" },
      { n: "promotion_threshold", t: "int", l: "推广阈值（腰部销量，可选）" } ] },
  { id: "dossier", title: "5. 生成研究资料包（含 Excel 工作簿）", fields: [] },
  { id: "table-export", title: "导出过程表供人工检查", fields: [{ n: "table", t: "int", l: "表号 1=清洗Top 2=补齐 3=分类", v: 1 }, { n: "format", t: "select", l: "格式", o: ["xlsx", "csv"] }] },
  { id: "table-import", title: "上传人工修改后的表1", fields: [{ n: "table", t: "int", l: "表号", v: 1 }, { n: "file", t: "file", l: "修改后的表1" }] },
  { id: "evidence-add", title: "登记外部研究证据", fields: [
      { n: "slot", t: "text", l: "证据槽位，如 market_size_us / tiktok_units_us_overall" }, { n: "claim", t: "text", l: "证据内容" },
      { n: "value", t: "text", l: "数值（槽位要求数值时必填）" }, { n: "unit", t: "text", l: "单位 units/usd/pct（TikTok 销量只接受台数）" },
      { n: "period", t: "text", l: "周期，如 2025" }, { n: "source_name", t: "text", l: "来源名称" }, { n: "source_url", t: "text", l: "来源 URL" } ] },
  { id: "finding-add", title: "登记深挖结论", fields: [{ n: "module", t: "text", l: "模块，如 market_growth" }, { n: "text", t: "text", l: "结论（须引用证据，不含 Yes/No）" }, { n: "evidence", t: "list", l: "证据 ID，每行一个" }] },
];

function actionForm(a, uploads, pid, onDone) {
  const inputs = {}; const msg = h("div", { class: "muted" });
  const fields = a.fields.map(f => {
    let el;
    if (f.t === "files") el = h("select", { multiple: true, size: 4 }, uploads.map(u => h("option", { value: u.id }, `${u.filename}（${u.size}B）`)));
    else if (f.t === "file") el = h("select", {}, h("option", { value: "" }, "—"), uploads.map(u => h("option", { value: u.id }, u.filename)));
    else if (f.t === "list") el = h("textarea", { rows: 3 });
    else if (f.t === "select") el = h("select", {}, f.o.map(o => h("option", { value: o }, o)));
    else el = h("input", { type: f.t === "int" ? "number" : "text", value: f.v ?? "" });
    inputs[f.n] = { el, f };
    return h("div", {}, h("label", {}, f.l), el);
  });
  const liveBox = ["ingest", "enrich"].includes(a.id) ? h("input", { type: "checkbox" }) : null;
  const collect = () => {
    const p = {};
    for (const [n, { el, f }] of Object.entries(inputs)) {
      if (f.t === "files") { const v = [...el.selectedOptions].map(o => o.value); if (v.length) p[n] = v; }
      else if (f.t === "list") { const v = el.value.split("\n").map(s => s.trim()).filter(Boolean); if (v.length) p[n] = v; }
      else if (f.t === "int") { if (el.value !== "") p[n] = parseInt(el.value, 10); }
      else if (el.value) p[n] = el.value;
    }
    if (liveBox && liveBox.checked) p.live = true;
    return p;
  };
  return h("details", {}, h("summary", {}, a.title), fields,
    liveBox ? h("label", {}, liveBox, "通过供应商接口实时取数（需服务端已开启并登录）") : null,
    h("p", {}, h("button", { onclick: async () => { try { const r = await post(`/api/projects/${pid}/jobs`, { command: a.id, params: collect() }); flash(msg, `已提交任务 ${r.job_id.slice(0, 8)}`); onDone(); } catch (e) { flash(msg, e.message, true); } } }, "提交")), msg);
}

async function renderProject(app) {
  const pid = state.project;
  const box = h("div", {}, "加载中…");
  app.append(h("p", {}, h("button", { class: "secondary", onclick: () => { state.project = null; render(); } }, "← 返回项目列表")), box);
  async function load() {
    try {
      const [status, uploads, jobs, exports_] = await Promise.all([api(`/api/projects/${pid}/status`), api(`/api/projects/${pid}/uploads`), api(`/api/projects/${pid}/jobs`), api(`/api/projects/${pid}/exports`)]);
      const active = jobs.some(j => j.status === "queued" || j.status === "running");
      box.replaceChildren(
        statusCard(status), pendingCard(status, pid, load), uploadCard(uploads, pid, load),
        h("section", {}, h("h2", {}, "研究步骤"), ACTIONS.map(a => actionForm(a, uploads, pid, load))),
        jobsCard(jobs), downloadsCard(exports_, pid), await membersCard(pid));
      clearInterval(state.timer);
      if (active) state.timer = setInterval(load, 2500);
    } catch (e) { box.replaceChildren(h("p", { class: "err" }, e.message)); }
  }
  load();
}

function statusCard(s) {
  const st = s.state || {};
  return h("section", {}, h("h2", {}, `${s.project?.product || ""}（${s.project?.marketplace || ""}）`),
    h("p", {}, "下一步建议：", h("span", { class: "badge" }, s.next), "　已完成步骤：", (st.completed_steps || []).join("、") || "无"));
}

function pendingCard(s, pid, reload) {
  const cp = s.pending_checkpoint;
  if (!cp) return h("section", {}, h("h2", {}, "待人工确认"), h("p", { class: "muted" }, "当前没有待确认的检查点"));
  const edits = h("textarea", { rows: 2, placeholder: "分类修改，每行 ASIN=标签（仅分类检查点）" });
  const select = h("input", { type: "text", placeholder: "确认的研究支线 id，逗号分隔（仅路线检查点，可留空）" });
  const msg = h("div", {});
  const send = async approve => { try { await post(`/api/projects/${pid}/resolve`, { checkpoint: cp.checkpoint_id, approve, edits: edits.value.split("\n").map(x => x.trim()).filter(Boolean), select: select.value || null }); flash(msg, approve ? "已提交确认" : "已提交驳回"); reload(); } catch (e) { flash(msg, e.message, true); } };
  return h("section", {}, h("h2", {}, `待人工确认：${cp.step_key}`), h("p", {}, cp.prompt),
    h("pre", {}, JSON.stringify(cp.payload, null, 2).slice(0, 8000)),
    cp.step_key === "classification_review" ? edits : null, cp.step_key.includes("route") ? select : null,
    h("p", {}, h("button", { onclick: () => send(true) }, "确认通过"), " ", h("button", { class: "secondary", onclick: () => send(false) }, "驳回重做")),
    h("p", { class: "muted" }, "裁决人将记录为当前登录用户；系统不会代替人做最终商业判断。"), msg);
}

function uploadCard(uploads, pid, reload) {
  const file = h("input", { type: "file" }); const msg = h("div", { class: "muted" });
  return h("section", {}, h("h2", {}, "上传文件"), file,
    h("p", {}, h("button", { onclick: async () => { const f = file.files[0]; if (!f) return; try { await api(`/api/projects/${pid}/uploads?filename=${encodeURIComponent(f.name)}`, { method: "PUT", body: f }); flash(msg, "上传成功"); reload(); } catch (e) { flash(msg, e.message, true); } } }, "上传")),
    msg, uploads.length ? h("table", {}, h("tr", {}, ["文件", "大小", "SHA256", "上传人"].map(t => h("th", {}, t))), uploads.map(u => h("tr", {}, h("td", {}, u.filename), h("td", {}, u.size), h("td", {}, u.sha256.slice(0, 12)), h("td", {}, u.uploaded_by)))) : null);
}

function jobsCard(jobs) {
  return h("section", {}, h("h2", {}, "任务进度"), jobs.length ? h("table", {}, h("tr", {}, ["命令", "状态", "提交人", "时间", "结果/错误"].map(t => h("th", {}, t))),
    jobs.map(j => h("tr", {}, h("td", {}, j.command), h("td", { class: j.status }, j.status), h("td", {}, j.created_by), h("td", {}, j.created_at.replace("T", " ").slice(0, 19)),
      h("td", {}, j.error ? h("span", { class: "err" }, j.error) : h("code", {}, JSON.stringify(j.result || {}).slice(0, 160)))))) : h("p", { class: "muted" }, "暂无任务"));
}

function downloadsCard(ex, pid) {
  const dl = async (path, name) => { const blob = await api(path); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click(); URL.revokeObjectURL(a.href); };
  return h("section", {}, h("h2", {}, "下载"),
    ex.exports.map(n => h("div", {}, h("a", { href: "#", onclick: e => { e.preventDefault(); dl(`/api/projects/${pid}/exports/${encodeURIComponent(n)}`, n); } }, n))),
    ex.dossier_job_ids.map(j => h("div", {}, h("a", { href: "#", onclick: e => { e.preventDefault(); dl(`/api/projects/${pid}/dossiers/${j}.zip`, `dossier-${j.slice(0, 8)}.zip`); } }, `研究资料包 ${j.slice(0, 8)}.zip`))),
    !ex.exports.length && !ex.dossier_job_ids.length ? h("p", { class: "muted" }, "尚无可下载文件") : null);
}

async function membersCard(pid) {
  const members = await api(`/api/projects/${pid}/members`);
  const isOwner = members.some(m => m.name === state.user.name && m.role === "owner") || state.user.role === "admin";
  const name = h("input", { type: "text", placeholder: "用户名" });
  const role = h("select", {}, ["editor", "viewer", "owner"].map(r => h("option", { value: r }, r)));
  const msg = h("div", {});
  return h("section", {}, h("h2", {}, "项目成员"),
    h("table", {}, members.map(m => h("tr", {}, h("td", {}, m.name), h("td", {}, m.role)))),
    isOwner ? h("div", {}, h("div", { class: "row" }, h("div", {}, name), h("div", {}, role)),
      h("p", {}, h("button", { onclick: async () => { try { await api(`/api/projects/${pid}/members`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: name.value, role: role.value }) }); render(); } catch (e) { flash(msg, e.message, true); } } }, "添加/修改成员")), msg) : null);
}

render();
