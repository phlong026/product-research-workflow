/* 选品研究工作台前端(原生 JS,无构建、无外链)。
 *
 * 安全约定:
 *  - 一切来自服务器/用户的文本只经 textContent / createTextNode 渲染,绝不拼接 HTML。
 *  - 各人工检查点只在用户点击「确认」「驳回」时才提交 resolve 作业,前端不做任何自动批准。
 *  - 不生成、不显示任何商业结论;最终决定由人工在资料包报告中填写。
 */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const session = { me: null, csrf: "" };
  let wb = null; // 当前项目工作台上下文;切换页面时被替换,旧的轮询据此自行终止
  let routeToken = 0;
  let uid = 0;

  const MONTH_RE = /^\d{4}-\d{2}$/;
  const MONTHS_RE = /^\d{4}-\d{2}(,\d{4}-\d{2})*$/;
  const KEY_RE = /^[A-Za-z0-9_.:-]{1,64}$/;
  const ROLE_NAMES = { owner: "所有者", editor: "编辑者", viewer: "只读", admin: "管理员", researcher: "研究员" };
  const COMMAND_NAMES = {
    ingest: "数据导入", "discover-nodes": "节点发现", enrich: "商品补齐", classify: "分类", analyze: "分析",
    resolve: "检查点裁决", dossier: "生成资料包", "export-table": "导出表格", "import-table": "导入修改后的表",
  };
  const TABLE_NAMES = { 1: "表1 · 月度清洗数据", 2: "表2 · 唯一 ASIN 的地区/卖点", 3: "表3 · 分类结果" };
  const STATUS_NAMES = { queued: "排队中", running: "运行中", succeeded: "成功", failed: "失败" };
  const PAIR_RE = /^\d{4}-\d{2}:\d{4}-\d{2}$/;
  const PERIOD_RE = /^\d{4}-\d{2}:\d{4}-\d{2}:\d{4}-\d{2}:\d{4}-\d{2}$/;
  const STEPS = [
    { key: "ingest", name: "数据导入", checkpoint: false },
    { key: "data_review", name: "数据检查点", checkpoint: true },
    { key: "enrich", name: "商品补齐", checkpoint: false, optional: true },
    { key: "enrichment_review", name: "补齐检查点", checkpoint: true, optional: true },
    { key: "classify", name: "分类", checkpoint: false },
    { key: "classification_review", name: "分类检查点", checkpoint: true },
    { key: "analyze", name: "分析", checkpoint: false },
    { key: "research_route_review", name: "路线检查点", checkpoint: true },
    { key: "dossier", name: "资料包", checkpoint: false },
  ];
  const NEXT_INDEX = { ingest: 0, enrich: 2, classify: 4, analyze: 6, dossier: 8, done: 9 };
  const POLL_MS = 2000;
  const PAGE_SIZE = 50;

  // ---------------------------------------------------------------- DOM 工具

  const PROPS = new Set(["value", "checked", "disabled", "selected", "indeterminate"]);

  function append(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (PROPS.has(key)) el[key] = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    append(el, children);
    return el;
  }

  function field(labelText, control, hint) {
    if (!control.id) control.id = "f" + (++uid);
    const hintEl = hint ? h("span", { class: "hint", id: control.id + "-hint" }, hint) : null;
    if (hintEl) control.setAttribute("aria-describedby", hintEl.id);
    return h("div", { class: "field" }, h("label", { for: control.id }, labelText), control, hintEl);
  }

  function button(label, opts) {
    const o = opts || {};
    return h("button", {
      type: o.type || "button",
      class: "btn" + (o.kind ? " " + o.kind.split(" ").map((k) => "btn-" + k).join(" ") : ""),
      "data-write": o.write ? "1" : null,
      disabled: !!o.disabled,
      onclick: o.onclick,
    }, label);
  }

  function badge(text, kind) {
    return h("span", { class: "badge" + (kind ? " badge-" + kind : "") }, text);
  }

  function notice(kind, title, body) {
    const role = kind === "danger" ? "alert" : "status";
    return h("div", { class: "notice notice-" + kind, role },
      title ? h("strong", null, title) : null, body);
  }

  function setNotice(container, kind, text) {
    container.replaceChildren();
    if (text) container.append(notice(kind, null, text));
  }

  function formatTime(value) {
    return value ? String(value).replace("T", " ").slice(0, 19) : "-";
  }

  function roleName(role) {
    return ROLE_NAMES[role] || role;
  }

  // ---------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail);
      this.status = status;
    }
  }

  function detailText(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((d) => (d && d.msg) || JSON.stringify(d)).join(";");
    return JSON.stringify(detail);
  }

  async function api(method, path, opts) {
    const o = opts || {};
    const headers = {};
    let body = o.body;
    if (method !== "GET") headers["X-CSRF-Token"] = session.csrf;
    if (o.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(o.json);
    }
    let res;
    try {
      res = await fetch(path, { method, headers, body, credentials: "same-origin" });
    } catch (err) {
      throw new ApiError(0, "网络错误:无法连接服务器");
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      data = null;
    }
    if (res.status === 401 && !path.startsWith("/api/auth/login")) {
      sessionExpired();
      throw new ApiError(401, "未登录或会话已过期");
    }
    if (!res.ok) throw new ApiError(res.status, detailText(data && data.detail) || "请求失败(" + res.status + ")");
    return data;
  }

  function sessionExpired() {
    if (!session.me) return;
    session.me = null;
    session.csrf = "";
    stopView();
    renderLogin("会话已过期,请重新登录");
  }

  // ---------------------------------------------------------------- 页面外壳

  function stopView() {
    routeToken += 1;
    if (wb && wb.timer) clearTimeout(wb.timer);
    wb = null;
  }

  function mount(...nodes) {
    app.replaceChildren(...nodes);
  }

  function topbar() {
    const me = session.me;
    return h("header", { class: "topbar" },
      h("span", { class: "brand" }, h("a", { href: "#/" }, "选品研究工作台")),
      h("span", { class: "spacer" }),
      h("span", { class: "who" }, me.username + "(" + roleName(me.role) + ")"),
      button("退出登录", { onclick: logout }));
  }

  function page(title, ...content) {
    const heading = h("h1", { tabindex: "-1" }, title);
    mount(topbar(), h("main", { class: "page" }, heading, ...content));
    document.title = title + " - 选品研究工作台";
    return heading;
  }

  async function logout() {
    try {
      await api("POST", "/api/auth/logout");
    } catch (err) {
      // 无论服务端是否成功,本地都回到登录页;会话令牌已不可用
    }
    session.me = null;
    session.csrf = "";
    stopView();
    renderLogin("");
  }

  // ---------------------------------------------------------------- 登录

  function renderLogin(message) {
    document.title = "登录 - 选品研究工作台";
    const msg = h("div");
    if (message) setNotice(msg, "warn", message);
    const user = h("input", { type: "text", name: "username", autocomplete: "username", required: true });
    const pass = h("input", { type: "password", name: "password", autocomplete: "current-password", required: true });
    const submit = button("登录", { type: "submit", kind: "primary" });
    const form = h("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          const me = await api("POST", "/api/auth/login", { json: { username: user.value.trim(), password: pass.value } });
          session.me = { username: me.username, role: me.role };
          session.csrf = me.csrf_token;
          location.hash = "#/";
          route();
        } catch (err) {
          setNotice(msg, "danger", err.message);
          pass.value = "";
          pass.focus();
        } finally {
          submit.disabled = false;
        }
      },
    }, field("用户名", user), field("口令", pass), msg, submit);
    mount(h("main", { class: "login-wrap" }, h("div", { class: "card login-card" },
      h("h1", null, "选品研究工作台"),
      h("p", { class: "muted" }, "登录后创建研究项目、完成各人工检查点并下载资料包。"), form)));
    user.focus();
  }

  // ---------------------------------------------------------------- 项目列表

  async function renderProjects() {
    const token = ++routeToken;
    const heading = page("研究项目", h("p", { class: "muted" }, "加载中…"));
    heading.focus();
    let list;
    try {
      list = await api("GET", "/api/projects");
    } catch (err) {
      if (token === routeToken) page("研究项目", notice("danger", "加载失败", err.message));
      return;
    }
    if (token !== routeToken) return;
    page("研究项目", createProjectCard(), session.me.role === "admin" ? createUserCard() : null, projectTable(list.projects));
  }

  function projectTable(projects) {
    const body = projects.length
      ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["产品", "站点", "状态", "我的角色", "创建时间"].map((t) => h("th", null, t)))),
        h("tbody", null, projects.map((p) => h("tr", null,
          h("td", null, h("a", { href: "#/p/" + encodeURIComponent(p.project_id) }, p.product)),
          h("td", null, p.marketplace), h("td", null, p.status),
          h("td", null, roleName(p.role)), h("td", { class: "mono-cell" }, formatTime(p.created_at)))))))
      : h("p", { class: "muted" }, "还没有可见的项目,先在上方创建一个。");
    return h("section", { class: "card", "aria-labelledby": "projects-title" }, h("h2", { id: "projects-title" }, "我的项目"), body);
  }

  function createProjectCard() {
    const msg = h("div");
    const product = h("input", { type: "text", required: true, maxlength: "200" });
    const market = h("select", null, ["US", "UK", "DE", "FR", "IT", "ES", "JP", "CA", "AU", "MX", "IN"].map(
      (code) => h("option", { value: code }, code)));
    const keyword = h("input", { type: "text", maxlength: "200" });
    const submit = button("创建项目", { type: "submit", kind: "primary" });
    const form = h("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        const json = { product: product.value.trim(), marketplace: market.value };
        if (keyword.value.trim()) json.primary_keyword = keyword.value.trim();
        try {
          const out = await api("POST", "/api/projects", { json });
          location.hash = "#/p/" + encodeURIComponent(out.project_id);
        } catch (err) {
          setNotice(msg, "danger", err.message);
          submit.disabled = false;
        }
      },
    }, h("div", { class: "grid" },
      field("产品名", product), field("站点", market),
      field("主关键词(可选)", keyword, "MCP 直取数据时默认使用")), msg, submit);
    return h("section", { class: "card", "aria-labelledby": "new-project-title" },
      h("h2", { id: "new-project-title" }, "新建项目"), form);
  }

  function createUserCard() {
    const msg = h("div");
    const name = h("input", { type: "text", required: true, maxlength: "64", pattern: "[A-Za-z0-9_.\\-]{1,64}", autocomplete: "off" });
    const pass = h("input", { type: "password", required: true, minlength: "12", autocomplete: "new-password" });
    const role = h("select", null, h("option", { value: "researcher" }, "研究员"), h("option", { value: "admin" }, "管理员"));
    const submit = button("创建账号", { type: "submit", kind: "primary" });
    const form = h("form", {
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          const out = await api("POST", "/api/admin/users", {
            json: { username: name.value.trim(), password: pass.value, role: role.value },
          });
          setNotice(msg, "ok", "已创建账号 " + out.username + "(" + roleName(out.role) + ")");
          name.value = "";
          pass.value = "";
        } catch (err) {
          setNotice(msg, "danger", err.message);
        } finally {
          submit.disabled = false;
        }
      },
    }, h("div", { class: "grid" },
      field("用户名", name, "字母、数字、_ . -"), field("初始口令", pass, "至少 12 个字符"), field("角色", role)), msg, submit);
    return h("details", { class: "card" }, h("summary", null, "创建账号(仅管理员)"), form);
  }

  // ---------------------------------------------------------------- 工作台

  async function renderWorkbench(pid) {
    const token = ++routeToken;
    if (wb && wb.timer) clearTimeout(wb.timer);
    wb = null;
    page("项目工作台", h("p", { class: "muted" }, "加载中…"));
    let detail;
    let jobs;
    try {
      [detail, jobs] = await Promise.all([
        api("GET", "/api/projects/" + encodeURIComponent(pid)),
        api("GET", "/api/projects/" + encodeURIComponent(pid) + "/jobs"),
      ]);
    } catch (err) {
      if (token === routeToken) {
        page("项目工作台", notice("danger", "无法打开项目", err.message), h("a", { href: "#/" }, "返回项目列表"));
      }
      return;
    }
    if (token !== routeToken) return;
    const els = {
      stepper: h("div"), jobbar: h("section", { class: "card", "aria-label": "作业状态" }),
      msg: h("div"), main: h("div"), history: h("div"), aside: h("aside"),
    };
    wb = { pid, token, detail, els, jobs: jobs.jobs, canWrite: detail.role !== "viewer", busy: false, timer: null, jobId: null };
    const heading = page(detail.project.product + " · " + detail.project.marketplace,
      els.stepper, els.jobbar, els.msg,
      h("div", { class: "two-col" }, h("div", null, els.main, els.history), els.aside));
    heading.focus();
    renderAll();
    const latest = jobs.jobs[0];
    if (latest && (latest.status === "queued" || latest.status === "running")) trackJob(latest.job_id);
    else if (latest) showJob(latest);
    else showIdle(null);
  }

  async function reloadProject(flash) {
    const mine = wb;
    try {
      const base = "/api/projects/" + encodeURIComponent(mine.pid);
      const [detail, jobs] = await Promise.all([api("GET", base), api("GET", base + "/jobs")]);
      mine.detail = detail;
      mine.jobs = jobs.jobs;
    } catch (err) {
      if (wb === mine) setNotice(mine.els.msg, "danger", err.message);
      return;
    }
    if (wb !== mine) return;
    mine.canWrite = mine.detail.role !== "viewer";
    renderAll();
    setNotice(mine.els.msg, flash ? "ok" : "", flash || "");
  }

  function currentIndex(detail) {
    const cp = detail.pending_checkpoint;
    if (cp) {
      const key = cp.step_key === "route_review" ? "research_route_review" : cp.step_key;
      return STEPS.findIndex((s) => s.key === key);
    }
    return NEXT_INDEX[detail.next] === undefined ? 0 : NEXT_INDEX[detail.next];
  }

  function renderAll() {
    const { detail, els } = wb;
    const current = currentIndex(detail);
    const completed = new Set((detail.state && detail.state.completed_steps) || []);
    const stateKey = { enrich: "enrich_asins" };
    els.stepper.replaceChildren(h("ol", { class: "stepper", "aria-label": "研究流程步骤" },
      STEPS.map((step, i) => {
        // 可选步骤没有执行(状态里没有完成记录)就如实显示“已跳过”,不打勾
        const skipped = Boolean(step.optional) && i < current && !completed.has(stateKey[step.key] || step.key);
        return h("li", {
          class: [i < current && !skipped ? "done" : "", skipped ? "skipped" : "", i === current ? "current" : "",
            step.checkpoint ? "checkpoint" : ""].join(" ").trim(),
          "aria-current": i === current ? "step" : null,
        }, h("span", { class: "num" }, "步骤 " + (i + 1)), h("span", { class: "name" }, step.name),
        h("span", { class: "kind" }, skipped ? "已跳过(可选)"
          : (step.optional ? "可选" : "") + (step.checkpoint ? "人工检查点" : "自动步骤")));
      })));
    renderMain();
    renderHistory();
    renderAside();
    applyBusy();
  }

  function renderMain() {
    const { detail, els, canWrite } = wb;
    const cp = detail.pending_checkpoint;
    const parts = [];
    if (!canWrite) parts.push(notice("info", "只读成员", "你可以查看进度与检查点内容,但不能提交任何操作。"));
    const slot = h("div");
    parts.push(slot);
    els.main.replaceChildren(...parts);
    if (cp) {
      const key = cp.step_key === "route_review" ? "research_route_review" : cp.step_key;
      if (key === "data_review") slot.append(dataReviewPanel(cp));
      else if (key === "enrichment_review") slot.append(enrichmentReviewPanel(cp));
      else if (key === "classification_review") slot.append(classificationPanel(cp));
      else if (key === "research_route_review") slot.append(routePanel(cp));
      else slot.append(unknownCheckpointPanel(cp));
    } else if (detail.next === "ingest") {
      slot.append(canWrite ? ingestPanel() : readonlyStep("数据导入"));
    } else if (detail.next === "enrich") {
      slot.append(canWrite ? enrichPanel() : readonlyStep("商品补齐"));
      const tables = tablesBlock([2]);
      if (tables) slot.append(h("section", { class: "card" }, tables));
    } else if (detail.next === "classify") {
      if (canWrite) slot.append(enrichPanel(true));
      slot.append(canWrite ? classifyPanel() : readonlyStep("分类"));
      const tables = tablesBlock([2]);
      if (tables) slot.append(h("section", { class: "card" }, tables));
    } else if (detail.next === "analyze") {
      slot.append(canWrite ? analyzePanel() : readonlyStep("分析"));
    } else {
      slot.append(dossierPanel());
    }
  }

  function readonlyStep(name) {
    return h("section", { class: "card" }, h("h2", null, name), h("p", { class: "muted" }, "当前步骤需要编辑者执行。"));
  }

  function renderAside() {
    const { detail, els } = wb;
    const project = detail.project;
    const info = h("section", { class: "card", "aria-labelledby": "info-title" }, h("h2", { id: "info-title" }, "项目信息"),
      h("dl", { class: "kv" },
        h("dt", null, "产品"), h("dd", null, project.product),
        h("dt", null, "站点"), h("dd", null, project.marketplace),
        h("dt", null, "主关键词"), h("dd", null, project.primary_keyword || "-"),
        h("dt", null, "状态"), h("dd", null, project.status),
        h("dt", null, "我的角色"), h("dd", null, roleName(detail.role)),
        h("dt", null, "项目 ID"), h("dd", { class: "mono" }, project.id)));
    const artifacts = (detail.artifacts || []).length
      ? h("ul", null, detail.artifacts.map((name) => h("li", null, h("a", {
        href: "/api/projects/" + encodeURIComponent(wb.pid) + "/artifacts/" + encodeURIComponent(name),
        target: "_blank", rel: "noopener",
      }, name))))
      : h("p", { class: "muted" }, "暂无工件");
    els.aside.replaceChildren(info,
      h("section", { class: "card", "aria-labelledby": "art-title" }, h("h2", { id: "art-title" }, "已生成工件"), artifacts),
      membersCard());
  }

  function membersCard() {
    const isOwner = wb.detail.role === "owner";
    const pid = wb.pid;
    const msg = h("div");
    const list = h("div", null, h("p", { class: "muted" }, "加载成员名单…"));
    const loadMembers = async () => {
      const mine = wb;
      try {
        const data = await api("GET", "/api/projects/" + encodeURIComponent(pid) + "/members");
        if (wb !== mine) return;
        list.replaceChildren(h("ul", { class: "member-list" }, data.members.map((m) => h("li", null,
          h("strong", null, m.username), badge(roleName(m.role), m.role === "owner" ? "accent" : null),
          isOwner && m.role !== "owner"
            ? button("移除", { kind: "small danger", onclick: () => removeMember(m.username) })
            : null))));
      } catch (err) {
        if (wb === mine) list.replaceChildren(notice("danger", "读取成员名单失败", err.message));
      }
    };
    const removeMember = async (username) => {
      if (!window.confirm("确定移除成员 " + username + " 吗?他将无法再访问此项目。")) return;
      try {
        await api("DELETE", "/api/projects/" + encodeURIComponent(pid) + "/members/" + encodeURIComponent(username));
        setNotice(msg, "ok", "已移除 " + username);
        await loadMembers();
      } catch (err) {
        setNotice(msg, "danger", err.message);
      }
    };
    loadMembers();
    let form = null;
    if (isOwner) {
      const name = h("input", { type: "text", required: true, maxlength: "64", autocomplete: "off" });
      const role = h("select", null, h("option", { value: "editor" }, "编辑者(可执行)"), h("option", { value: "viewer" }, "只读"));
      const submit = button("添加成员", { type: "submit", kind: "primary" });
      form = h("form", {
        onsubmit: async (event) => {
          event.preventDefault();
          submit.disabled = true;
          try {
            const out = await api("POST", "/api/projects/" + encodeURIComponent(pid) + "/members",
              { json: { username: name.value.trim(), role: role.value } });
            setNotice(msg, "ok", "已将 " + out.username + " 设为" + roleName(out.role));
            name.value = "";
            await loadMembers();
          } catch (err) {
            setNotice(msg, "danger", err.message);
          } finally {
            submit.disabled = false;
          }
        },
      }, field("用户名", name, "须为已存在的账号"), field("角色", role), submit);
    }
    return h("section", { class: "card", "aria-labelledby": "member-title" },
      h("h2", { id: "member-title" }, "项目成员"), list, msg, form);
  }

  // ---------------------------------------------------------------- 作业

  function applyBusy() {
    if (!wb) return;
    for (const el of app.querySelectorAll("[data-write]")) el.disabled = wb.busy;
  }

  function setBusy(busy) {
    if (!wb) return;
    wb.busy = busy;
    applyBusy();
  }

  async function startJob(command, params) {
    const mine = wb;
    setBusy(true);
    setNotice(mine.els.msg, "", "");
    try {
      const out = await api("POST", "/api/projects/" + encodeURIComponent(mine.pid) + "/jobs", { json: { command, params } });
      if (wb === mine) trackJob(out.job_id);
    } catch (err) {
      if (wb !== mine) return;
      if (err.status === 409) {
        setNotice(mine.els.msg, "warn", "该项目已有进行中的作业,请等待其完成后再操作(已为你接入该作业的进度)。");
        await attachActiveJob();
      } else {
        setNotice(mine.els.msg, "danger", err.message);
        setBusy(false);
      }
    }
  }

  async function attachActiveJob() {
    const mine = wb;
    try {
      const jobs = await api("GET", "/api/projects/" + encodeURIComponent(mine.pid) + "/jobs");
      const active = jobs.jobs.find((j) => j.status === "queued" || j.status === "running");
      if (wb !== mine) return;
      if (active) trackJob(active.job_id);
      else setBusy(false);
    } catch (err) {
      if (wb === mine) setBusy(false);
    }
  }

  function jobTitle(job) {
    let title = COMMAND_NAMES[job.command] || job.command;
    if (job.command === "resolve") title += job.params && job.params.approve ? "(确认)" : "(驳回)";
    if ((job.command === "export-table" || job.command === "import-table") && job.params) title += "(表" + job.params.table + ")";
    return title;
  }

  function showIdle(last) {
    const text = last ? "上次作业:" + jobTitle(last) + " · 已成功" : "当前没有进行中的作业。";
    wb.els.jobbar.replaceChildren(h("div", { class: "jobbar", role: "status" }, badge("空闲"), h("span", { class: "muted" }, text)));
    setBusy(false);
  }

  function showJob(job) {
    if (job.status === "failed") {
      const retry = wb.canWrite ? button("重试", { kind: "primary", write: true, onclick: () => retryJob(job.job_id) }) : null;
      wb.els.jobbar.replaceChildren(h("div", { role: "alert" },
        h("div", { class: "jobbar" }, badge("失败", "danger"), h("strong", null, jobTitle(job)),
          h("span", { class: "muted" }, "第 " + job.attempts + " 次尝试"), retry),
        h("pre", { class: "mono small" }, job.error || "未知错误")));
      setBusy(false);
    } else if (job.status === "succeeded") {
      showIdle(job);
    } else {
      showIdle(null);
    }
  }

  async function retryJob(jobId) {
    const mine = wb;
    setBusy(true);
    try {
      const out = await api("POST", "/api/jobs/" + encodeURIComponent(jobId) + "/retry");
      if (wb === mine) trackJob(out.job_id);
    } catch (err) {
      if (wb !== mine) return;
      setNotice(mine.els.msg, err.status === 409 ? "warn" : "danger", err.message);
      if (err.status === 409) await attachActiveJob();
      else setBusy(false);
    }
  }

  function trackJob(jobId) {
    const mine = wb;
    if (mine.timer) clearTimeout(mine.timer);
    mine.jobId = jobId;
    setBusy(true);
    const poll = async () => {
      let job;
      try {
        job = await api("GET", "/api/jobs/" + encodeURIComponent(jobId));
      } catch (err) {
        if (wb !== mine || err.status === 401) return;
        setNotice(mine.els.msg, "danger", "读取作业状态失败:" + err.message + "(将继续重试)");
        mine.timer = setTimeout(poll, POLL_MS);
        return;
      }
      if (wb !== mine) return;
      if (job.status === "queued" || job.status === "running") {
        mine.els.jobbar.replaceChildren(h("div", { class: "jobbar", role: "status" },
          badge(job.status === "queued" ? "排队中" : "运行中", "accent"), h("strong", null, jobTitle(job)),
          h("progress", { "aria-label": "作业进行中" }),
          h("span", { class: "muted small" }, "每 2 秒刷新;期间不能提交新的操作")));
        mine.timer = setTimeout(poll, POLL_MS);
      } else if (job.status === "succeeded") {
        showIdle(job);
        await reloadProject(job.command === "import-table"
          ? "表" + job.params.table + " 已导入并通过校验。旧审批已失效,请在当前检查点重新人工确认;系统不会自动批准。"
          : "「" + jobTitle(job) + "」已完成。");
      } else {
        showJob(job);
        refreshJobs();
      }
    };
    poll();
  }

  async function refreshJobs() {
    const mine = wb;
    try {
      const data = await api("GET", "/api/projects/" + encodeURIComponent(mine.pid) + "/jobs");
      if (wb !== mine) return;
      mine.jobs = data.jobs;
      renderHistory();
    } catch (err) {
      // 作业历史只是辅助信息,读取失败不打断主流程
    }
  }

  function renderHistory() {
    const jobs = (wb.jobs || []).slice(0, 20);
    const body = jobs.length
      ? h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, ["时间", "作业", "状态", "尝试", "错误"].map((t) => h("th", null, t)))),
        h("tbody", null, jobs.map((job) => h("tr", null,
          h("td", { class: "mono-cell" }, formatTime(job.created_at)), h("td", null, jobTitle(job)),
          h("td", null, badge(STATUS_NAMES[job.status] || job.status,
            job.status === "failed" ? "danger" : job.status === "succeeded" ? "ok" : "accent")),
          h("td", { class: "num" }, job.attempts),
          h("td", { class: "small" }, job.error ? String(job.error).slice(0, 160) : "-"))))))
      : h("p", { class: "muted" }, "还没有作业记录。");
    wb.els.history.replaceChildren(h("section", { class: "card", "aria-labelledby": "history-title" },
      h("h2", { id: "history-title" }, "作业历史"), body));
  }

  function exportedTables() {
    const found = new Set();
    for (const job of wb.jobs || []) {
      if (job.command === "export-table" && job.status === "succeeded" && job.params) found.add(job.params.table);
    }
    return found;
  }

  // 导出/回传人工修改的表。回传成功后旧审批立即失效;这里不做任何批准动作。
  function tablesBlock(tables) {
    const exported = exportedTables();
    const rows = tables.map((n) => {
      const msg = h("div");
      const link = exported.has(n)
        ? h("a", { class: "btn btn-small", download: "",
          href: "/api/projects/" + encodeURIComponent(wb.pid) + "/exports/table" + n + ".csv" }, "下载已导出的 CSV")
        : null;
      if (!wb.canWrite) return link ? h("div", { class: "table-io-row" }, h("strong", null, TABLE_NAMES[n]), link) : null;
      const upload = fileInput(false);
      const send = button("上传修改后的表", { write: true, onclick: async () => {
        const mine = wb;
        if (!upload.files.length) { setNotice(msg, "danger", "请先选择修改后的表格文件"); return; }
        setNotice(msg, "", "");
        setBusy(true);
        try {
          const id = await uploadFile(upload.files[0]);
          if (wb !== mine) return;
          await startJob("import-table", { table: n, upload: id });
        } catch (err) {
          if (wb === mine) { setNotice(msg, "danger", err.message); setBusy(false); }
        }
      } });
      return h("div", { class: "table-io-row" }, h("strong", null, TABLE_NAMES[n]),
        h("div", { class: "row-actions" },
          button("导出此表(CSV)", { write: true, onclick: () => startJob("export-table", { table: n }) }), link),
        field("修改后的表(csv / xlsx)", upload),
        h("div", { class: "row-actions" }, send), msg);
    }).filter(Boolean);
    if (!rows.length) return null;
    return h("section", { class: "table-io" }, h("h3", null, "导出 / 回传人工修改的表(可选)"),
      notice("warn", "修改并上传后旧审批立即失效,需重新人工确认",
        "先导出 CSV,在表格软件中修改,再上传。系统会先校验,校验失败不改变任何状态并如实显示原因;上传成功也不会自动批准。"),
      rows);
  }

  // ---------------------------------------------------------------- 上传

  async function uploadFile(file) {
    const out = await api("PUT",
      "/api/projects/" + encodeURIComponent(wb.pid) + "/uploads?filename=" + encodeURIComponent(file.name), { body: file });
    return out.upload_id;
  }

  async function uploadAll(files) {
    const ids = [];
    for (const file of files) ids.push(await uploadFile(file));
    return ids;
  }

  function fileInput(multiple) {
    return h("input", { type: "file", accept: ".csv,.xlsx,.json", multiple: !!multiple });
  }

  function intOrNull(input, label, min) {
    const raw = input.value.trim();
    if (raw === "") return null;
    const n = Number(raw);
    if (!Number.isInteger(n) || n < min) throw new Error(label + "必须是不小于 " + min + " 的整数");
    return n;
  }

  // ---------------------------------------------------------------- 步骤 1:数据导入

  function ingestPanel() {
    const project = wb.detail.project;
    const msg = h("div");
    const files = fileInput(true);
    const month = h("input", { type: "text", placeholder: "YYYY-MM", maxlength: "7" });
    const topFile = h("input", { type: "number", min: "1", max: "1000", value: "100" });
    const fileForm = h("div", null,
      field("数据文件(csv / xlsx / json,可多选)", files, "每个文件对应一个月份;月份从文件名推断,如 2026-01.csv"),
      h("div", { class: "grid" },
        field("月份(可选)", month, "仅导入单个文件且文件名不含月份时填写"),
        field("每月保留 Top N", topFile)));

    const keyword = h("input", { type: "text", maxlength: "200", value: project.primary_keyword || "" });
    const monthsAuto = h("input", { type: "radio", name: "months-mode", value: "auto", checked: true });
    const monthsExplicit = h("input", { type: "radio", name: "months-mode", value: "explicit" });
    const monthsText = h("input", { type: "text", placeholder: "2025-01,2026-01", disabled: true });
    const syncMonths = () => { monthsText.disabled = !monthsExplicit.checked; };
    monthsAuto.addEventListener("change", syncMonths);
    monthsExplicit.addEventListener("change", syncMonths);
    const nodesText = h("textarea", { placeholder: "每行一个节点 ID,留空则按关键词默认范围" });
    const candidateN = h("input", { type: "number", min: "1", max: "2000", value: "200" });
    const topLive = h("input", { type: "number", min: "1", max: "1000", value: "100" });
    const nodeBox = h("div");
    const discover = button("发现节点", { write: true, onclick: () => {
      if (!keyword.value.trim()) { setNotice(msg, "danger", "请先填写关键词"); return; }
      setNotice(msg, "", "");
      startJob("discover-nodes", { keyword: keyword.value.trim() });
    } });
    const mcpForm = h("div", { hidden: true },
      field("关键词", keyword),
      h("fieldset", null, h("legend", null, "月份范围"),
        h("div", { class: "inline" },
          h("label", null, monthsAuto, "自动(最近完整月份窗口)"),
          h("label", null, monthsExplicit, "显式指定"), monthsText)),
      h("div", { class: "row-actions" }, discover,
        h("span", { class: "hint" }, "先发现节点,由你确认主节点;系统不会自动选定。")),
      nodeBox,
      field("节点(每行一个)", nodesText, "也可在上方勾选候选节点,二者合并"),
      h("div", { class: "grid" }, field("候选样本数", candidateN), field("每月保留 Top N", topLive)));
    if ((wb.detail.artifacts || []).includes("nodes.json")) loadNodeCandidates(nodeBox);

    const modeFile = h("input", { type: "radio", name: "ingest-mode", value: "file", checked: true });
    const modeMcp = h("input", { type: "radio", name: "ingest-mode", value: "mcp" });
    const syncMode = () => { fileForm.hidden = !modeFile.checked; mcpForm.hidden = !modeMcp.checked; };
    modeFile.addEventListener("change", syncMode);
    modeMcp.addEventListener("change", syncMode);

    const submit = button("开始导入", { kind: "primary", write: true, onclick: async () => {
      const mine = wb;
      setNotice(msg, "", "");
      try {
        if (modeFile.checked) {
          if (!files.files.length) throw new Error("请至少选择一个数据文件");
          const monthValue = month.value.trim();
          if (monthValue && !MONTH_RE.test(monthValue)) throw new Error("月份格式应为 YYYY-MM");
          if (monthValue && files.files.length > 1) throw new Error("指定月份时只能导入一个文件");
          const top = intOrNull(topFile, "Top N", 1) || 100;
          setBusy(true);
          const uploads = await uploadAll(Array.from(files.files));
          if (wb !== mine) return;
          const params = { uploads, top_n: top };
          if (monthValue) params.month = monthValue;
          await startJob("ingest", params);
        } else {
          const kw = keyword.value.trim();
          if (!kw) throw new Error("请填写关键词");
          const months = monthsExplicit.checked ? monthsText.value.replace(/\s+/g, "") : "auto";
          if (!MONTHS_RE.test(months) && months !== "auto") throw new Error("月份应形如 2025-01,2026-01");
          const nodes = new Set(nodesText.value.split("\n").map((s) => s.trim()).filter(Boolean));
          for (const box of nodeBox.querySelectorAll("input[type=checkbox]:checked")) nodes.add(box.value);
          const params = { live: true, keyword: kw, months, nodes: Array.from(nodes),
            candidate_n: intOrNull(candidateN, "候选样本数", 1) || 200, top_n: intOrNull(topLive, "Top N", 1) || 100 };
          await startJob("ingest", params);
        }
      } catch (err) {
        if (wb === mine) { setNotice(msg, "danger", err.message); setBusy(false); }
      }
    } });

    return h("section", { class: "card", "aria-labelledby": "ingest-title" },
      h("h2", { id: "ingest-title" }, "步骤 1 · 数据导入"),
      h("p", { class: "muted" }, "导入后会生成数据检查点,你确认之前不会进入分类。"),
      h("fieldset", null, h("legend", null, "数据来源"),
        h("div", { class: "inline" }, h("label", null, modeFile, "上传文件"), h("label", null, modeMcp, "MCP 直取"))),
      fileForm, mcpForm, msg, submit);
  }

  async function loadNodeCandidates(container) {
    const mine = wb;
    let data;
    try {
      data = await api("GET", "/api/projects/" + encodeURIComponent(mine.pid) + "/artifacts/nodes.json");
    } catch (err) {
      return;
    }
    if (wb !== mine || !data) return;
    const candidates = data.candidates || [];
    const suggestion = data.main_node_suggestion;
    const items = candidates.map((c) => h("li", null, h("label", null,
      h("input", { type: "checkbox", value: c.node_id, checked: !!(suggestion && suggestion.node_id === c.node_id) }),
      h("span", null, c.label + " "), h("code", null, c.node_id),
      h("span", { class: "muted small" }, " · " + c.asin_count + " 个 ASIN · 占比 " + (Number(c.share) * 100).toFixed(1) + "%"))));
    container.replaceChildren(h("fieldset", null,
      h("legend", null, "候选节点(关键词「" + (data.keyword || "") + "」)"),
      notice("warn", "主节点必须由你确认",
        "勾选仅是预填的参考" + (data.main_node_tied ? ";存在并列节点,请仔细核对" : "") + ",提交前请确认选择。"),
      h("ul", { class: "node-list" }, items)));
    applyBusy();
  }

  // ---------------------------------------------------------------- 步骤 3:分类

  function classifyPanel() {
    const msg = h("div");
    const rowsBox = h("div", { class: "label-rows" });
    const rows = [];
    const addRow = () => {
      const key = h("input", { type: "text", maxlength: "64", placeholder: "nugget" });
      const name = h("input", { type: "text", maxlength: "200", placeholder: "Nugget 冰" });
      const rule = h("input", { type: "text", maxlength: "400", placeholder: "nugget, pellet" });
      const row = { key, name, rule };
      const remove = button("删除", { kind: "small", onclick: () => {
        if (rows.length <= 1) return;
        rows.splice(rows.indexOf(row), 1);
        el.remove();
      } });
      const el = h("div", { class: "label-row" },
        field("标签键(英文/数字)", key), field("标签名称", name), field("规则关键词(逗号分隔,可选)", rule), remove);
      rows.push(row);
      rowsBox.append(el);
    };
    addRow();
    addRow();
    const dimension = h("input", { type: "text", value: "segment", maxlength: "64" });
    const providerRules = h("input", { type: "radio", name: "provider", value: "rules", checked: true });
    const providerLlm = h("input", { type: "radio", name: "provider", value: "llm" });
    const submit = button("开始分类", { kind: "primary", write: true, onclick: () => {
      try {
        const labels = Object.create(null); // 键可能是 __proto__ 等,用无原型对象避免污染
        const rules = Object.create(null);
        for (const row of rows) {
          const key = row.key.value.trim();
          const name = row.name.value.trim();
          const rule = row.rule.value.replace(/，/g, ",").trim();
          if (!key && !name && !rule) continue;
          if (!KEY_RE.test(key)) throw new Error("标签键只能含字母、数字和 _ . : -(最长 64):" + (key || "(空)"));
          if (!name) throw new Error("标签 " + key + " 缺少名称");
          if (key in labels) throw new Error("标签键重复:" + key);
          labels[key] = name;
          if (rule) rules[key] = rule;
        }
        if (!Object.keys(labels).length) throw new Error("至少定义一个标签");
        if (providerRules.checked && !Object.keys(rules).length) {
          throw new Error("规则分类至少需要为一个标签填写关键词;或改用 LLM 分类");
        }
        setNotice(msg, "", "");
        startJob("classify", { labels, rules, dimension: dimension.value.trim() || "segment",
          provider: providerLlm.checked ? "llm" : "rules" });
      } catch (err) {
        setNotice(msg, "danger", err.message);
      }
    } });
    return h("section", { class: "card", "aria-labelledby": "classify-title" },
      h("h2", { id: "classify-title" }, "步骤 3 · 分类"),
      h("p", { class: "muted" }, "定义分类维度的标签与规则。无法可靠归类的商品会被标记为待人工,在下一个检查点逐行指定。"),
      rowsBox, h("div", { class: "row-actions" }, button("添加标签", { onclick: addRow })),
      h("div", { class: "grid" }, field("分类维度", dimension)),
      h("fieldset", null, h("legend", null, "分类方式"),
        h("div", { class: "inline" }, h("label", null, providerRules, "规则关键词"), h("label", null, providerLlm, "LLM 辅助"))),
      h("p", { class: "muted" }, "卖家地区与卖点的补齐是独立的可选步骤:需要时先在上方运行「商品补齐」并确认补齐检查点,分类会采用已确认的补齐结果。"),
      msg, submit);
  }

  // ---------------------------------------------------------------- 商品补齐(可选)与补齐检查点

  function enrichPanel(optional) {
    const msg = h("div");
    return h("section", { class: "card", "aria-labelledby": "enrich-title" },
      h("h2", { id: "enrich-title" }, optional ? "商品补齐(可选)" : "商品补齐"),
      h("p", { class: "muted" }, "补齐缺失的卖家所属地与卖点(表2)。需要 MCP 会话;补齐结果存在冲突或失败时会在补齐检查点中醒目展示,不会静默覆盖,你确认之前不会进入分类。"),
      h("div", { class: "row-actions" }, button("运行商品补齐", { kind: optional ? null : "primary", write: true, onclick: () => startJob("enrich", {}) })),
      msg);
  }

  function enrichmentReviewPanel(cp) {
    const p = cp.payload || {};
    const note = p.human_table2_edits
      ? notice("info", "包含人工修改的表2",
        "人工校正的地区与卖点优先于机器补齐" + (p.table_edit ? "(本次修改 " + p.table_edit.changed_cells + " 个单元格)" : "") + ";请复核后再确认。")
      : null;
    const machine = p.summary
      ? enrichmentBlock({ summary: p.summary, conflicts: p.conflicts, failures: p.failures })
      : h("p", { class: "muted" }, "没有机器补齐结果,只有人工修改的表2。");
    return h("section", { class: "card", "aria-labelledby": "er-title" },
      checkpointHeader(cp, "补齐检查点(表2)", "er-title"),
      note, machine, tablesBlock([2]), resolveButtons(cp));
  }

  // ---------------------------------------------------------------- 步骤 5:分析

  function analyzePanel() {
    const msg = h("div");
    const newProducts = fileInput(false);
    const sales = h("input", { type: "number", min: "0", placeholder: "可选;仅作回退" });
    const consecutive = h("input", { type: "number", min: "1", max: "24", value: "3" });
    const early = h("input", { type: "number", min: "1", max: "24", value: "3" });
    const survival = h("input", { type: "number", min: "0" });
    const promotion = h("input", { type: "number", min: "0" });
    const retention = h("input", { type: "number", min: "0" });
    const study = h("input", { type: "text", placeholder: "YYYY-MM", maxlength: "7" });
    const pairs = h("textarea", { placeholder: "2025-01:2026-01" });
    const periods = h("textarea", { placeholder: "2025-01:2025-03:2026-01:2026-03" });
    const submit = button("开始分析", { kind: "primary", write: true, onclick: async () => {
      const mine = wb;
      try {
        const params = {
          consecutive_months: intOrNull(consecutive, "连续月数", 1) ?? 3,
          early_window: intOrNull(early, "早期窗口", 1) ?? 3,
        };
        for (const [key, input, label] of [["sales_threshold", sales, "销量阈值"], ["survival_threshold", survival, "存活阈值"],
          ["promotion_threshold", promotion, "推起阈值"], ["retention_threshold", retention, "留存阈值"]]) {
          const value = intOrNull(input, label, 0);
          if (value !== null) params[key] = value;
        }
        const studyMonth = study.value.trim();
        if (studyMonth && !MONTH_RE.test(studyMonth)) throw new Error("研究月格式应为 YYYY-MM");
        if (studyMonth) params.study_month = studyMonth;
        const lines = (box) => box.value.split("\n").map((l) => l.trim()).filter(Boolean);
        const pairList = lines(pairs);
        const periodList = lines(periods);
        if (pairList.length > 20 || periodList.length > 20) throw new Error("份额分解对和跨期比较各最多 20 条");
        const badPair = pairList.find((x) => !PAIR_RE.test(x));
        if (badPair) throw new Error("份额分解对格式应为 YYYY-MM:YYYY-MM,不合法:" + badPair);
        const badPeriod = periodList.find((x) => !PERIOD_RE.test(x));
        if (badPeriod) throw new Error("跨期比较格式应为 a起点:a终点:b起点:b终点(均为 YYYY-MM),不合法:" + badPeriod);
        if (pairList.length) params.pairs = pairList;
        if (periodList.length) params.periods = periodList;
        setNotice(msg, "", "");
        setBusy(true);
        if (newProducts.files.length) {
          params.new_products_upload = await uploadFile(newProducts.files[0]);
          if (wb !== mine) return;
        }
        await startJob("analyze", params);
      } catch (err) {
        if (wb === mine) { setNotice(msg, "danger", err.message); setBusy(false); }
      }
    } });
    return h("section", { class: "card", "aria-labelledby": "analyze-title" },
      h("h2", { id: "analyze-title" }, "步骤 5 · 分析"),
      h("p", { class: "muted" }, "所有指标由确定性程序计算;缺月按「未知」处理,不会当作零销量。完成后进入路线检查点。"),
      field("新品表(可选,csv / xlsx / json)", newProducts, "提供后才会做新品分析;须同时填写下方「推起阈值」和「留存阈值」(无内置默认值,未填则不计算新品指标并在资料包里说明)"),
      h("div", { class: "grid" },
        field("旧版统一阈值(可选,仅作回退)", sales), field("连续达标月数", consecutive), field("早期窗口(月)", early),
        field("存活阈值(可选)", survival), field("推起阈值(人工腰部销量)", promotion), field("留存阈值(月销量)", retention),
        field("研究月(可选)", study, "默认取数据中最新月份")),
      h("div", { class: "grid" },
        field("份额分解对(可选,每行一对)", pairs, "格式 YYYY-MM:YYYY-MM,基期:现期"),
        field("跨期比较(可选,每行一组)", periods, "格式 a起点:a终点:b起点:b终点,均为 YYYY-MM")),
      msg, submit);
  }

  // ---------------------------------------------------------------- 检查点通用

  function checkpointHeader(cp, title, id) {
    return [h("div", { class: "card-header" }, h("h2", { id }, title), badge("人工检查点", "warn")),
      cp.prompt ? h("p", null, cp.prompt) : null,
      notice("info", "需要你明确裁决", "系统不会自动批准;只有点击下方「确认」或「驳回」才会提交。")];
  }

  function resolveButtons(cp, getExtra, beforeApprove) {
    if (!wb.canWrite) return h("p", { class: "muted" }, "只读成员不能裁决检查点。");
    const approve = button("确认并继续", { kind: "primary", write: true, onclick: () => {
      if (beforeApprove && !beforeApprove()) return;
      startJob("resolve", Object.assign({ checkpoint_id: cp.checkpoint_id, approve: true }, getExtra ? getExtra() : {}));
    } });
    const reject = button("驳回", { kind: "danger", write: true, onclick: () => {
      if (!window.confirm("驳回后需要回到上一步重新执行,确定驳回吗?")) return;
      startJob("resolve", { checkpoint_id: cp.checkpoint_id, approve: false });
    } });
    return h("div", { class: "row-actions" }, approve, reject);
  }

  function unknownCheckpointPanel(cp) {
    const body = h("pre", { class: "mono small" }, JSON.stringify(cp.payload, null, 2));
    return h("section", { class: "card" }, checkpointHeader(cp, "检查点:" + cp.step_key), body, resolveButtons(cp));
  }

  // ---------------------------------------------------------------- 数据检查点

  function dataReviewPanel(cp) {
    const p = cp.payload || {};
    const months = Object.keys(p.months || {}).sort();
    const missing = p.missing_months || [];
    const msg = h("div");
    const ack = h("input", { type: "checkbox", disabled: !wb.canWrite });
    const warnCell = (n) => h("td", { class: "num" + (n > 0 ? " cell-warn" : "") }, n);
    const monthTable = h("div", { class: "table-wrap" }, h("table", null,
      h("caption", null, "按月清洗结果(数据来源:" + (p.source_mode || "未知") + ")"),
      h("thead", null, h("tr", null, ["月份", "输入行", "输出行", "父级去重", "标题去重", "警告数"].map(
        (t, i) => h("th", { class: i ? "num" : null }, t)))),
      h("tbody", null, months.map((m) => {
        const row = p.months[m];
        return h("tr", null, h("td", { class: "mono-cell" }, m), h("td", { class: "num" }, row.input_rows),
          h("td", { class: "num" }, row.output_rows), h("td", { class: "num" }, row.duplicate_parent_rows_removed),
          h("td", { class: "num" }, row.duplicate_title_rows_removed), warnCell(row.warnings));
      }))));
    const sources = h("div", { class: "table-wrap" }, h("table", null,
      h("caption", null, "数据来源与哈希(可与源文件逐一核对)"),
      h("thead", null, h("tr", null, ["来源", "类型", "SHA-256"].map((t) => h("th", null, t)))),
      h("tbody", null, (p.sources || []).map((s) => h("tr", null, h("td", null, s.name || s.artifact_id),
        h("td", null, s.kind), h("td", { class: "mono-cell" }, s.sha256))))));
    const missingBlock = missing.length
      ? notice("danger", "缺少 " + missing.length + " 个月份的数据",
        h("div", null, h("p", null, "缺失月份:" + missing.join("、")),
          h("p", null, "缺月不会被当作零销量,相关分析会标为「未知」。请决定是否补数据后再确认。"),
          h("label", { class: "inline" }, ack, "我已知悉缺月,仍然确认当前数据")))
      : notice("ok", "月份完整", "没有检测到缺失月份。");
    const warnTotal = months.reduce((n, m) => n + (p.months[m].warnings || 0), 0);
    const warnBlock = warnTotal > 0 ? notice("warn", "数据质量警告共 " + warnTotal + " 条", "请在上表标黄的月份逐项核对。") : null;
    return h("section", { class: "card", "aria-labelledby": "dr-title" },
      checkpointHeader(cp, "步骤 2 · 数据检查点", "dr-title"),
      missingBlock, warnBlock, monthTable, sources, tablesBlock([1]), msg,
      resolveButtons(cp, null, () => {
        if (missing.length && !ack.checked) {
          setNotice(msg, "danger", "存在缺月:请先勾选「我已知悉缺月」后再确认,或驳回并补充数据。");
          return false;
        }
        setNotice(msg, "", "");
        return true;
      }));
  }

  // ---------------------------------------------------------------- 分类检查点

  async function loadTitles() {
    const titles = new Map();
    try {
      const data = await api("GET", "/api/projects/" + encodeURIComponent(wb.pid) + "/artifacts/records.json");
      for (const records of Object.values(data || {})) {
        for (const rec of records) if (rec.title && !titles.has(rec.asin)) titles.set(rec.asin, rec.title);
      }
    } catch (err) {
      // 标题只是辅助信息,读取失败不影响审阅
    }
    return titles;
  }

  function classificationPanel(cp) {
    const p = cp.payload || {};
    const labels = (p.schema && p.schema.labels) || [];
    const rowsMap = (p.classification && p.classification.rows) || {};
    const needs = new Set(p.needs_review || []);
    // 规则分类对未命中的商品只标 unknown 而不置待人工;同样醒目置顶,避免被当作已分类放过
    const unknownKey = p.schema && p.schema.unknown_label;
    const unclassified = new Set(Object.keys(rowsMap).filter((a) => !needs.has(a) && rowsMap[a].label === unknownKey));
    const rank = (a) => (needs.has(a) ? 2 : unclassified.has(a) ? 1 : 0);
    const asins = Object.keys(rowsMap).sort((a, b) => (rank(b) - rank(a)) || (a < b ? -1 : a > b ? 1 : 0));
    const edits = new Map();
    const titles = new Map();
    const view = { query: "", onlyNeeds: false, page: 0 };
    const unresolved = h("div", { role: "status" });
    const nearConfirm = h("span", { class: "hint", role: "status" }); // 紧贴「确认」按钮的提示
    const tbody = h("tbody");
    const pager = h("div", { class: "pager" });
    const readonly = !wb.canWrite;

    const unresolvedCount = () => asins.filter((a) => needs.has(a) && !edits.has(a)).length;
    const refreshUnresolved = () => {
      const n = unresolvedCount();
      nearConfirm.textContent = n > 0 ? "仍有 " + n + " 行待人工,必须逐行指定标签后才能确认。" : "";
      const open = asins.filter((a) => unclassified.has(a) && !edits.has(a)).length;
      unresolved.replaceChildren(...[
        n > 0
          ? notice("warn", "仍有 " + n + " 行待人工分类", "必须逐行指定标签后才能确认;服务端也会拦截未指定的行。")
          : notice("ok", needs.size ? "待人工行均已指定" : "没有待人工行", null),
        open > 0 ? notice("warn", open + " 行未归类(标签为 " + unknownKey + ")",
          "规则未命中这些商品。可在下拉中指定标签;不指定则按 " + unknownKey + " 保留进入分析。") : null,
      ].filter(Boolean));
    };
    const filtered = () => asins.filter((a) => {
      if (view.onlyNeeds && rank(a) === 0) return false;
      if (!view.query) return true;
      return a.toLowerCase().includes(view.query) || (titles.get(a) || "").toLowerCase().includes(view.query);
    });

    const renderRows = () => {
      const list = filtered();
      const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
      view.page = Math.min(view.page, pages - 1);
      const slice = list.slice(view.page * PAGE_SIZE, (view.page + 1) * PAGE_SIZE);
      tbody.replaceChildren(...slice.map((asin) => classRow(asin)));
      pager.replaceChildren(
        button("上一页", { kind: "small", disabled: view.page === 0, onclick: () => { view.page -= 1; renderRows(); } }),
        h("span", { class: "muted" }, "第 " + (view.page + 1) + " / " + pages + " 页,共 " + list.length + " 行"),
        button("下一页", { kind: "small", disabled: view.page >= pages - 1, onclick: () => { view.page += 1; renderRows(); } }));
      applyBusy();
    };

    const classRow = (asin) => {
      const row = rowsMap[asin];
      const isNeeds = needs.has(asin);
      const isOpen = isNeeds || unclassified.has(asin);
      const select = h("select", { "aria-label": "ASIN " + asin + " 的标签", disabled: readonly });
      if (isOpen) select.append(h("option", { value: "" }, isNeeds ? "— 请选择 —" : "— 未归类 —"));
      for (const label of labels) select.append(h("option", { value: label.key }, label.name + "(" + label.key + ")"));
      select.value = edits.has(asin) ? edits.get(asin) : (isOpen ? "" : row.label);
      const tr = h("tr", { class: (isOpen ? "needs-review" : "") + (edits.has(asin) ? " edited" : "") });
      select.addEventListener("change", () => {
        if (select.value === "" || (!isOpen && select.value === row.label)) edits.delete(asin);
        else edits.set(asin, select.value);
        tr.classList.toggle("edited", edits.has(asin));
        refreshUnresolved();
      });
      const evidence = [row.evidence, row.rationale, isNeeds ? row.review_reason : ""].filter(Boolean);
      tr.append(h("td", { class: "mono-cell" }, asin, isOpen ? h("div", null, badge(isNeeds ? "待人工" : "未归类", "warn")) : null),
        h("td", null, titles.get(asin) || "-"), h("td", null, select),
        h("td", null, row.confidence || "-"),
        h("td", null, evidence.length ? evidence.map((t) => h("div", null, t)) : "-"),
        h("td", null, row.source || "-"));
      return tr;
    };

    const search = h("input", { type: "search", placeholder: "ASIN 或标题" });
    search.addEventListener("input", () => { view.query = search.value.trim().toLowerCase(); view.page = 0; renderRows(); });
    const onlyNeeds = h("input", { type: "checkbox" });
    onlyNeeds.addEventListener("change", () => { view.onlyNeeds = onlyNeeds.checked; view.page = 0; renderRows(); });
    const table = h("div", { class: "table-wrap" }, h("table", null,
      h("caption", null, "分类结果(待人工行置顶并高亮;修改下拉即登记人工修改)"),
      h("thead", null, h("tr", null, ["ASIN", "标题", "标签", "置信度", "依据", "来源"].map((t) => h("th", null, t)))),
      tbody));

    const actions = resolveButtons(cp, () => ({ edits: Object.fromEntries(edits) }));
    actions.append(nearConfirm);
    const panel = h("section", { class: "card", "aria-labelledby": "cr-title" },
      checkpointHeader(cp, "步骤 4 · 分类检查点", "cr-title"),
      enrichmentBlock(p.enrichment), unresolved,
      h("div", { class: "table-tools" }, field("筛选", search),
        h("label", { class: "inline" }, onlyNeeds, "只看待人工/未归类(" + (needs.size + unclassified.size) + ")")),
      table, pager, tablesBlock([3, 2]), actions);
    refreshUnresolved();
    renderRows();
    const mine = wb;
    loadTitles().then((map) => {
      if (wb !== mine) return;
      for (const [k, v] of map) titles.set(k, v);
      renderRows();
    });
    return panel;
  }

  const SUMMARY_NAMES = {
    unknown_region_before: "补齐前地区未知", unknown_region_after: "补齐后地区未知",
    regions_filled: "已补齐地区", bullets_fetched_asins: "已获取卖点的 ASIN", bullets_missing_asins: "缺少卖点的 ASIN",
  };

  function enrichmentBlock(enrichment) {
    if (!enrichment) return h("p", { class: "muted" }, "本次分类未执行 ASIN 补齐。");
    const conflicts = enrichment.conflicts || [];
    const failures = enrichment.failures || [];
    const summary = Object.entries(enrichment.summary || {});
    const parts = [];
    if (conflicts.length || failures.length) {
      parts.push(notice("danger", "ASIN 补齐存在 " + conflicts.length + " 处冲突、" + failures.length + " 处失败",
        "系统没有静默覆盖;请核对下方明细后再确认。冲突处保留的是首选数据源的值,仅供参考。"));
    }
    if (summary.length) {
      parts.push(h("dl", { class: "kv" }, summary.map(([k, v]) => [
        h("dt", null, SUMMARY_NAMES[k] || k), h("dd", null, Array.isArray(v) ? v.length + " 个" : String(v))])));
    }
    if (conflicts.length) {
      parts.push(h("div", { class: "table-wrap" }, h("table", null, h("caption", null, "补齐冲突(同一字段各来源取值不同)"),
        h("thead", null, h("tr", null, ["ASIN", "字段", "月份", "各来源取值", "当前保留"].map((t) => h("th", null, t)))),
        h("tbody", null, conflicts.map((c) => h("tr", null, h("td", { class: "mono-cell" }, c.asin), h("td", null, c.field),
          h("td", null, c.month || "-"),
          h("td", null, Object.entries(c.values || {}).map(([src, val]) => h("div", null, src + " = " + val))),
          h("td", { class: "cell-warn" }, c.chosen)))))));
    }
    if (failures.length) {
      parts.push(h("div", { class: "table-wrap" }, h("table", null, h("caption", null, "补齐失败"),
        h("thead", null, h("tr", null, ["ASIN", "来源", "错误"].map((t) => h("th", null, t)))),
        h("tbody", null, failures.map((f) => h("tr", null, h("td", { class: "mono-cell" }, f.asin),
          h("td", null, f.source), h("td", { class: "cell-danger" }, f.error)))))));
    }
    return h("div", null, h("h3", null, "ASIN 补齐结果"), parts);
  }

  // ---------------------------------------------------------------- 路线检查点

  function routePanel(cp) {
    const suggestions = (cp.payload && cp.payload.suggestions) || [];
    const readonly = !wb.canWrite;
    const boxes = suggestions.map((s) => ({
      s, input: h("input", { type: "checkbox", value: s.route_id, disabled: readonly, "aria-describedby": "route-" + s.route_id }),
    }));
    const PRIORITY = { high: "高优先级", normal: "普通", low: "低优先级" };
    const list = suggestions.length
      ? h("ul", { class: "route-list" }, boxes.map(({ s, input }) => h("li", null, h("label", null, input,
        h("span", { id: "route-" + s.route_id },
          h("span", { class: "route-title" }, s.title + " "), badge(PRIORITY[s.priority] || s.priority || "普通", s.priority === "high" ? "warn" : null),
          h("div", null, s.reason),
          (s.evidence_ids || []).length ? h("div", { class: "hint mono" }, "证据:" + s.evidence_ids.join("、")) : null,
          (s.next_modules || []).length ? h("div", { class: "hint" }, "后续模块:" + s.next_modules.join("、")) : null)))))
      : h("p", { class: "muted" }, "本次没有路线建议,可直接确认。");
    return h("section", { class: "card", "aria-labelledby": "rr-title" },
      checkpointHeader(cp, "步骤 6 · 路线检查点", "rr-title"),
      h("p", { class: "muted" }, "勾选你决定登记的研究路线(可不选)。确认只登记路线,不会自动执行任何支线。"),
      list,
      resolveButtons(cp, () => ({ select: boxes.filter((b) => b.input.checked).map((b) => b.s.route_id) })));
  }

  // ---------------------------------------------------------------- 资料包

  function dossierPanel() {
    const done = wb.detail.next === "done";
    const listBox = h("div", null, h("p", { class: "muted" }, "加载资料包版本…"));
    const mine = wb;
    api("GET", "/api/projects/" + encodeURIComponent(mine.pid) + "/dossiers").then((data) => {
      if (wb !== mine) return;
      const versions = (data.dossiers || []).slice().reverse();
      listBox.replaceChildren(versions.length
        ? h("ul", { class: "version-list" }, versions.map((d) => h("li", null, h("strong", { class: "mono" }, d.version),
          h("span", { class: "muted" }, d.files + " 个文件"),
          h("a", { class: "btn btn-small", download: "",
            href: "/api/projects/" + encodeURIComponent(mine.pid) + "/dossiers/" + encodeURIComponent(d.version) + "/download.zip",
          }, "下载 zip"))))
        : h("p", { class: "muted" }, "还没有生成过资料包。"));
    }).catch((err) => {
      if (wb === mine) listBox.replaceChildren(notice("danger", "读取资料包列表失败", err.message));
    });
    const generate = wb.canWrite
      ? button(done ? "生成新版本资料包" : "生成资料包", { kind: "primary", write: true, onclick: () => startJob("dossier", {}) })
      : null;
    return h("section", { class: "card", "aria-labelledby": "dossier-title" },
      h("h2", { id: "dossier-title" }, "步骤 7 · 资料包"),
      done ? notice("ok", "各检查点均已由你确认", "可下载资料包,也可生成新版本。") : null,
      h("p", { class: "disclaimer" }, "最终商业决定由人工在报告第 8 节填写,系统不代填。"),
      h("h3", null, "已生成的版本"), listBox, generate);
  }

  // ---------------------------------------------------------------- 路由

  function route() {
    if (!session.me) return;
    const match = location.hash.match(/^#\/p\/(.+)$/);
    if (match) {
      stopView();
      renderWorkbench(decodeURIComponent(match[1]));
    } else {
      stopView();
      renderProjects();
    }
  }

  window.addEventListener("hashchange", route);

  async function boot() {
    try {
      const me = await api("GET", "/api/me");
      session.me = { username: me.username, role: me.role };
      session.csrf = me.csrf_token;
      route();
    } catch (err) {
      if (err.status === 401) renderLogin("");
      else mount(h("main", { class: "page" }, notice("danger", "无法连接服务器", err.message)));
    }
  }

  boot();
})();
