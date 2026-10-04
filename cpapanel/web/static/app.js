/*!
 * CPA 面板 · 单页前端
 * 约束：零依赖、零构建、无任何外链资源；hash 路由；图表为手写 SVG。
 * 所有写请求（POST/PUT/PATCH/DELETE）都带 `X-CPA-Panel: 1`（服务端 CSRF 校验）。
 */
(function () {
  "use strict";

  var CSRF_HEADER = "X-CPA-Panel";
  var WRITE_METHODS = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };
  var EXTERNAL = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;   // 用于拒绝外链（安全闸）
  var LS_THEME = "cpa.ui.theme";
  var LS_NODE = "cpa.ui.node";
  var SVG_NS = "http://www.w3.org/2000/svg";

  var STATE_TEXT = {
    healthy: "健康",
    cooling: "冷却中",
    quota_exhausted: "额度耗尽",
    unauthorized: "需重登",
    disabled: "已禁用",
    unknown: "未知"
  };
  var STATE_TONE = {
    healthy: "ok",
    cooling: "warn",
    quota_exhausted: "warn",
    unauthorized: "bad",
    disabled: "muted",
    unknown: "muted"
  };
  var TONES = ["#0a84ff", "#34c759", "#ff9f0a", "#ff375f", "#af52de", "#30b0c7", "#8e8e93", "#ffd60a"];
  var OAUTH_PROVIDERS = ["claude", "codex", "antigravity", "kimi", "kimi-ai", "xai", "devin", "meta"];

  /* ================================================================ DOM 工具 */

  function byId(id) { return document.getElementById(id); }
  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function txt(value) { return document.createTextNode(value === null || value === undefined ? "" : String(value)); }

  function apply(node, attrs) {
    if (!attrs) return node;
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") { node.className = String(value); return; }
      if (key === "text") { node.textContent = value === null ? "" : String(value); return; }
      if (key === "style" && typeof value === "object") {
        Object.keys(value).forEach(function (prop) { node.style.setProperty(prop, String(value[prop])); });
        return;
      }
      if (key === "data" && typeof value === "object") {
        Object.keys(value).forEach(function (prop) {
          if (value[prop] !== null && value[prop] !== undefined) node.setAttribute("data-" + prop, String(value[prop]));
        });
        return;
      }
      if (key.slice(0, 2) === "on" && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
        return;
      }
      if (value === true) { node.setAttribute(key, ""); return; }
      node.setAttribute(key, String(value));
    });
    return node;
  }

  function append(parent, child) {
    if (child === null || child === undefined || child === false) return parent;
    if (Array.isArray(child)) {
      for (var i = 0; i < child.length; i++) append(parent, child[i]);
      return parent;
    }
    if (child instanceof Node) { parent.appendChild(child); return parent; }
    parent.appendChild(document.createTextNode(String(child)));
    return parent;
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    apply(node, attrs);
    if (children !== undefined) append(node, children);
    return node;
  }

  function svgEl(tag, attrs, children) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        node.setAttribute(key, String(value));
      });
    }
    if (children !== undefined) append(node, children);
    return node;
  }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); return node; }
  function mount(host, child) { clear(host); append(host, child); return host; }

  /** 只接受站内相对路径，任何绝对/协议相对地址一律降级为 "#"（无外链保证）。 */
  function safeHref(url) {
    var value = String(url || "");
    if (EXTERNAL.test(value)) return "#";
    return value;
  }

  /* ================================================================ 格式化 */

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }
  function fmtInt(value) {
    return String(Math.round(num(value))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function fmtCompact(value) {
    var n = num(value);
    var abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (abs >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return fmtInt(n);
  }
  function fmtCost(value) {
    var n = num(value);
    if (n > 0 && n < 0.01) return "$" + n.toFixed(5);
    return "$" + n.toFixed(2);
  }
  function fmtPct(value) { return (Math.round(num(value) * 1000) / 10) + "%"; }
  function fmtBytes(value) {
    var n = num(value);
    if (n <= 0) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return (n / Math.pow(1024, index)).toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function fmtTs(ts, withDate) {
    if (!ts) return "—";
    var date = new Date(num(ts) * 1000);
    if (isNaN(date.getTime())) return "—";
    var out = pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) + " " + pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    return withDate ? date.getFullYear() + "-" + out : out;
  }
  function dur(seconds) {
    var s = Math.max(0, Math.floor(num(seconds)));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60 ? (s % 60) + "s" : "");
    if (s < 86400) return Math.floor(s / 3600) + "h" + (Math.floor((s % 3600) / 60) + "m");
    return Math.floor(s / 86400) + "d" + Math.floor((s % 86400) / 3600) + "h";
  }
  function relTime(ts) {
    if (!ts) return "—";
    var diff = Math.floor(Date.now() / 1000) - num(ts);
    if (diff < 0) return "还有 " + dur(-diff);
    return dur(diff) + "前";
  }
  function shortDay(day) { return String(day || "").slice(5) || "—"; }
  function truncate(value, limit) {
    var s = value === null || value === undefined ? "" : String(value);
    return s.length > limit ? s.slice(0, limit - 1) + "…" : s;
  }
  function isMasked(value) { return /[…*]/.test(String(value || "")); }
  function show(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") { try { return JSON.stringify(value); } catch (e) { return String(value); } }
    return String(value);
  }

  /* ================================================================ HTTP */

  function buildQuery(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === "" || value === false) return;
      parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(value)));
    });
    return parts.join("&");
  }

  function api(method, path, options) {
    options = options || {};
    var init = { method: method, credentials: "same-origin", cache: "no-store", headers: {} };
    if (WRITE_METHODS[method]) init.headers[CSRF_HEADER] = "1";
    if (typeof FormData !== "undefined" && options.body instanceof FormData) {
      init.body = options.body;                       // 交给浏览器补 boundary
    } else if (options.body !== undefined && options.body !== null) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (options.headers) {
      Object.keys(options.headers).forEach(function (key) { init.headers[key] = options.headers[key]; });
    }
    var query = options.query ? buildQuery(options.query) : "";
    return fetch(path + (query ? "?" + query : ""), init).then(function (response) {
      return response.text().then(function (raw) {
        var data = null;
        if (raw) { try { data = JSON.parse(raw); } catch (e) { data = { raw: raw }; } }
        if (!response.ok) {
          var message = (data && (data.error || data.message)) || ("HTTP " + response.status + " " + response.statusText);
          var error = new Error(message);
          error.status = response.status;
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  }

  function get(path, query) { return api("GET", path, { query: query }); }
  function send(method, path, body, query) { return api(method, path, { body: body === undefined ? {} : body, query: query }); }
  function plainText(data) {
    if (data === null || data === undefined) return "";
    if (typeof data === "string") return data;
    if (data.raw !== undefined) return String(data.raw);
    try { return JSON.stringify(data, null, 2); } catch (e) { return String(data); }
  }

  /* ================================================================ 应用状态 */

  var app = {
    session: null,
    nodes: [],
    nodeId: null,
    page: "overview",
    params: [],
    timers: [],
    bound: false,
    pageSize: 50
  };

  function nodeQuery() { return app.nodeId ? { node_id: app.nodeId } : {}; }
  function withNode(extra) {
    var out = extra || {};
    if (app.nodeId) out.node_id = app.nodeId;
    return out;
  }
  function addTimer(id) { app.timers.push(id); return id; }
  function clearTimers() {
    app.timers.forEach(function (id) { clearInterval(id); clearTimeout(id); });
    app.timers = [];
  }

  function toast(message, tone) {
    var host = byId("toasts");
    if (!host) return;
    var item = el("div", { class: "toast " + (tone || "info"), role: "status" }, [txt(message)]);
    host.appendChild(item);
    setTimeout(function () { if (item.parentNode) item.parentNode.removeChild(item); }, tone === "bad" ? 8000 : 4200);
  }

  function reportError(error) {
    if (error && error.status === 401) {
      toast("会话已失效，请重新登录", "bad");
      showLogin();
      return;
    }
    toast((error && error.message) || String(error), "bad");
  }

  /* ================================================================ 模态框 */

  function openModal(options) {
    options = options || {};
    var root = byId("modalRoot");
    var body = el("div", { class: "modal-body" });
    append(body, options.body);
    var actions = el("div", { class: "modal-actions" });
    (options.actions || []).forEach(function (action) {
      actions.appendChild(el("button", {
        class: "btn " + (action.tone || ""),
        type: "button",
        text: action.label,
        onclick: function () {
          if (typeof action.onClick === "function") action.onClick(closeModal, body);
          else closeModal();
        }
      }));
    });
    var dialog = el("div", { class: "modal " + (options.size || ""), role: "dialog", "aria-modal": "true" }, [
      el("div", { class: "modal-head" }, [
        el("h2", { text: options.title || "" }),
        el("button", { class: "icon-btn", type: "button", "aria-label": "关闭", text: "✕", onclick: closeModal })
      ]),
      body,
      (options.actions || []).length ? actions : null
    ]);
    var backdrop = el("div", {
      class: "modal-backdrop",
      onclick: function (event) { if (event.target === backdrop) closeModal(); }
    }, [dialog]);
    mount(root, backdrop);
    root.hidden = false;
    document.body.classList.add("modal-open");
    return body;
  }

  function closeModal() {
    var root = byId("modalRoot");
    if (!root) return;
    clear(root);
    root.hidden = true;
    document.body.classList.remove("modal-open");
  }

  /** 带说明文字的二次确认（替代 window.confirm，便于把风险写清楚）。 */
  function confirmDialog(title, message, onConfirm, confirmLabel) {
    openModal({
      title: title,
      body: [el("p", { text: message })],
      actions: [
        { label: "取消", onClick: function (close) { close(); } },
        {
          label: confirmLabel || "确认执行",
          tone: "danger",
          onClick: function (close) { close(); onConfirm(); }
        }
      ]
    });
  }

  /* ================================================================ 主题 */

  function theme() { return localStorage.getItem(LS_THEME) || "auto"; }
  function applyTheme(value) { document.documentElement.setAttribute("data-theme", value || "auto"); }
  function cycleTheme() {
    var order = ["auto", "light", "dark"];
    var current = theme();
    var next = order[(order.indexOf(current) + 1) % order.length];
    localStorage.setItem(LS_THEME, next);
    applyTheme(next);
    if (app.session && app.session.authenticated) {
      send("PUT", "/api/settings", { "ui.theme": next }).catch(function () { /* 主题是本地偏好，失败不打扰 */ });
    }
    toast("主题：" + ({ auto: "跟随系统", light: "浅色", dark: "深色" })[next], "info");
  }

  /* ================================================================ 通用组件 */

  function button(label, onClick, tone) {
    return el("button", { class: "btn " + (tone || ""), type: "button", text: label, onclick: onClick });
  }
  function badge(text, tone) { return el("span", { class: "badge " + (tone || "muted"), text: text }); }
  function stateBadge(state) { return badge(STATE_TEXT[state] || state || "未知", STATE_TONE[state] || "muted"); }
  function hint(text) { return el("p", { class: "muted small", text: text }); }
  function emptyRow(message) { return el("div", { class: "empty muted", text: message || "暂无数据" }); }
  function loading(text) { return el("div", { class: "loading" }, [el("span", { class: "spinner" }), txt(text || "加载中…")]); }
  function errorCard(error) {
    return el("div", { class: "card error-card" }, [
      el("div", { class: "card-body stack" }, [
        el("p", { text: "加载失败：" + ((error && error.message) || String(error)) }),
        button("重试", function () { refresh(); })
      ])
    ]);
  }
  function jsonBlock(value) {
    var text;
    if (typeof value === "string") text = value;
    else { try { text = JSON.stringify(value, null, 2); } catch (e) { text = String(value); } }
    return el("pre", { class: "code", text: text === undefined ? "" : text });
  }
  function card(title, body, options) {
    options = options || {};
    var head = null;
    if (title || options.tools) {
      head = el("header", { class: "card-head" }, [
        title ? el("h2", { text: title }) : null,
        options.hint ? el("span", { class: "muted small", text: options.hint }) : null,
        options.tools && options.tools.length ? el("div", { class: "card-tools" }, options.tools) : null
      ]);
    }
    return el("section", { class: "card " + (options.class || "") }, [head, el("div", { class: "card-body" }, body)]);
  }
  function pageHead(title, subtitle, actions) {
    return el("header", { class: "page-head" }, [
      el("div", {}, [el("h1", { text: title }), subtitle ? el("p", { class: "muted", text: subtitle }) : null]),
      actions && actions.length ? el("div", { class: "page-actions" }, actions) : null
    ]);
  }
  function tile(label, value, sub, tone) {
    return el("div", { class: "tile " + (tone || "") }, [
      el("div", { class: "tile-label", text: label }),
      el("div", { class: "tile-value", text: value === null || value === undefined ? "—" : String(value) }),
      sub ? el("div", { class: "tile-sub muted small", text: sub }) : null
    ]);
  }
  function tiles(list) { return el("div", { class: "tiles" }, list); }
  function keyValue(pairs) {
    return el("dl", { class: "kv" }, pairs.map(function (pair) {
      return el("div", { class: "kv-row" }, [el("dt", { text: pair[0] }), el("dd", {}, pair[1])]);
    }));
  }
  function field(label, control, extraClass) {
    var node = el("div", { class: "field " + (extraClass || "") });
    node.appendChild(el("span", { text: label === undefined || label === null ? "" : String(label) }));
    if (label && control && typeof control.tagName === "string" &&
        /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName) && !control.getAttribute("aria-label")) {
      control.setAttribute("aria-label", String(label));
    }
    append(node, control);
    return node;
  }
  function input(attrs) { return el("input", attrs); }
  function select(options, value, onChange) {
    var node = el("select", { onchange: onChange });
    options.forEach(function (option) {
      node.appendChild(el("option", { value: option.value, text: option.label }));
    });
    if (value !== undefined && value !== null) node.value = String(value);
    return node;
  }

  function dataTable(columns, rows, options) {
    options = options || {};
    if (!rows || !rows.length) {
      return el("div", { class: "table-wrap" }, [emptyRow(options.emptyText)]);
    }
    var head = el("thead", {}, [el("tr", {}, columns.map(function (column) {
      return el("th", { class: column.class || "", text: column.title });
    }))]);
    var body = el("tbody", {}, rows.map(function (row, index) {
      return el("tr", { class: options.rowClass ? options.rowClass(row, index) : "" }, columns.map(function (column) {
        var cell = el("td", { class: column.class || "" });
        append(cell, column.render ? column.render(row, index) : show(row[column.key]));
        return cell;
      }));
    }));
    return el("div", { class: "table-wrap" }, [el("table", { class: "table" }, [head, body])]);
  }

  /* ================================================================ 手写 SVG 图表 */

  var CHART_W = 720;

  /** 面积 + 折线图：items = [{label, value}] */
  function svgAreaChart(items, options) {
    options = options || {};
    var height = options.height || 220;
    var format = options.format || fmtCompact;
    var color = options.color || TONES[0];
    var pad = { l: 62, r: 18, t: 14, b: 30 };
    var values = items.map(function (item) { return num(item.value); });
    var max = values.length ? Math.max.apply(null, values) : 0;
    if (max <= 0) max = 1;
    var innerW = CHART_W - pad.l - pad.r;
    var innerH = height - pad.t - pad.b;
    var count = values.length;

    function px(index) { return count <= 1 ? pad.l + innerW / 2 : pad.l + (innerW * index) / (count - 1); }
    function py(value) { return pad.t + innerH - (innerH * num(value)) / max; }

    var root = svgEl("svg", {
      class: "chart", viewBox: "0 0 " + CHART_W + " " + height,
      role: "img", "aria-label": options.label || "趋势图"
    });

    for (var line = 0; line <= 4; line++) {
      var level = (max * line) / 4;
      var y = py(level);
      root.appendChild(svgEl("line", { class: "grid", x1: pad.l, y1: y.toFixed(1), x2: CHART_W - pad.r, y2: y.toFixed(1) }));
      root.appendChild(svgEl("text", { class: "axis", x: pad.l - 8, y: (y + 4).toFixed(1), "text-anchor": "end" }, [txt(format(level))]));
    }

    if (count) {
      var path = "";
      for (var i = 0; i < count; i++) {
        path += (i ? " L" : "M") + px(i).toFixed(1) + "," + py(values[i]).toFixed(1);
      }
      var base = (pad.t + innerH).toFixed(1);
      root.appendChild(svgEl("path", { class: "area", fill: color, d: path + " L" + px(count - 1).toFixed(1) + "," + base + " L" + px(0).toFixed(1) + "," + base + " Z" }));
      root.appendChild(svgEl("path", { class: "line", stroke: color, d: path }));

      var step = Math.max(1, Math.ceil(count / 7));
      for (var j = 0; j < count; j++) {
        var dot = svgEl("circle", { class: "dot", cx: px(j).toFixed(1), cy: py(values[j]).toFixed(1), r: 3, stroke: color });
        dot.appendChild(svgEl("title", {}, [txt(items[j].label + "：" + format(values[j]))]));
        root.appendChild(dot);
        if (j % step === 0 || j === count - 1) {
          root.appendChild(svgEl("text", { class: "axis", x: px(j).toFixed(1), y: height - 8, "text-anchor": "middle" }, [txt(items[j].label)]));
        }
      }
    } else {
      root.appendChild(svgEl("text", { class: "axis", x: CHART_W / 2, y: height / 2, "text-anchor": "middle" }, [txt("暂无数据")]));
    }
    return root;
  }

  /** 横向条形排行：items = [{label, value}] */
  function svgBarChart(items, options) {
    options = options || {};
    var format = options.format || fmtCompact;
    var color = options.color || TONES[0];
    var labelWidth = 210;
    var data = items.slice(0, options.limit || 12);
    var rowHeight = 30;
    var height = Math.max(48, data.length * rowHeight + 12);
    var barWidth = CHART_W - labelWidth - 96;
    var max = 1;
    data.forEach(function (item) { max = Math.max(max, num(item.value)); });
    var root = svgEl("svg", {
      class: "chart bars", viewBox: "0 0 " + CHART_W + " " + height,
      role: "img", "aria-label": options.label || "排行图"
    });
    data.forEach(function (item, index) {
      var top = index * rowHeight + 8;
      var value = num(item.value);
      var width = Math.max(2, (barWidth * value) / max);
      root.appendChild(svgEl("text", { class: "axis", x: 0, y: top + 15 }, [txt(truncate(item.label || "(未知)", 30))]));
      root.appendChild(svgEl("rect", { class: "bar-bg", x: labelWidth, y: top + 4, width: barWidth, height: 16, rx: 8 }));
      var rect = svgEl("rect", { class: "bar", x: labelWidth, y: top + 4, width: width.toFixed(1), height: 16, rx: 8, fill: item.color || color });
      rect.appendChild(svgEl("title", {}, [txt((item.label || "") + "：" + format(value))]));
      root.appendChild(rect);
      root.appendChild(svgEl("text", { class: "axis value", x: CHART_W, y: top + 17, "text-anchor": "end" }, [txt(format(value))]));
    });
    return root;
  }

  /** 环形图：items = [{label, value, color}] */
  function svgDonut(items, options) {
    options = options || {};
    var size = 200;
    var radius = 74;
    var stroke = 22;
    var center = size / 2;
    var circumference = 2 * Math.PI * radius;
    var total = 0;
    items.forEach(function (item) { total += Math.max(0, num(item.value)); });
    var root = svgEl("svg", {
      class: "chart donut", viewBox: "0 0 " + size + " " + size,
      role: "img", "aria-label": options.label || "分布图"
    });
    if (total <= 0) {
      root.appendChild(svgEl("circle", { class: "bar-bg", cx: center, cy: center, r: radius, fill: "none", "stroke-width": stroke }));
    } else {
      var offset = 0;
      items.forEach(function (item, index) {
        var value = Math.max(0, num(item.value));
        if (value <= 0) return;
        var length = (circumference * value) / total;
        var arc = svgEl("circle", {
          cx: center, cy: center, r: radius, fill: "none", "stroke-width": stroke,
          stroke: item.color || TONES[index % TONES.length],
          "stroke-dasharray": length.toFixed(2) + " " + (circumference - length).toFixed(2),
          "stroke-dashoffset": (-offset).toFixed(2),
          transform: "rotate(-90 " + center + " " + center + ")"
        });
        arc.appendChild(svgEl("title", {}, [txt((item.label || "") + "：" + fmtInt(value))]));
        root.appendChild(arc);
        offset += length;
      });
    }
    root.appendChild(svgEl("text", { class: "donut-total", x: center, y: center + 1, "text-anchor": "middle" }, [txt(fmtCompact(total))]));
    root.appendChild(svgEl("text", { class: "axis", x: center, y: center + 20, "text-anchor": "middle" }, [txt(options.center || "合计")]));
    return root;
  }

  function legend(items) {
    var total = 0;
    items.forEach(function (item) { total += Math.max(0, num(item.value)); });
    return el("ul", { class: "legend" }, items.map(function (item, index) {
      var value = Math.max(0, num(item.value));
      return el("li", {}, [
        el("span", { class: "dot", style: { background: item.color || TONES[index % TONES.length] } }),
        el("span", { class: "legend-label", text: item.label || "(未知)" }),
        el("span", { class: "legend-value", text: fmtInt(value) + (total ? " · " + Math.round((value / total) * 100) + "%" : "") })
      ]);
    }));
  }

  /* ================================================================ 导航与路由（hash） */

  var NAV = [
    { key: "overview", title: "概览", group: "总览" },
    { key: "credentials", title: "账号凭证", group: "资源" },
    { key: "usage", title: "用量统计", group: "资源" },
    { key: "keys", title: "下游 Key", group: "资源" },
    { key: "nodes", title: "CPA 节点", group: "资源" },
    { key: "inspect", title: "巡检与动作", group: "自动化" },
    { key: "upstream", title: "上游配置", group: "上游" },
    { key: "logs", title: "日志", group: "上游" },
    { key: "settings", title: "设置", group: "系统" }
  ];

  var RENDER = {};   // key -> function (host, params)

  function parseHash() {
    var raw = String(location.hash || "").replace(/^#\/?/, "");
    var parts = raw.split("/").filter(function (part) { return part !== ""; }).map(function (part) {
      try { return decodeURIComponent(part); } catch (e) { return part; }
    });
    return { page: parts[0] || "overview", params: parts.slice(1) };
  }

  function navigate(page, param) {
    location.hash = "#/" + page + (param ? "/" + encodeURIComponent(param) : "");
  }

  function buildNav() {
    var list = byId("navList");
    clear(list);
    var group = null;
    NAV.forEach(function (item) {
      if (item.group !== group) {
        group = item.group;
        list.appendChild(el("li", { class: "nav-group", text: group }));
      }
      list.appendChild(el("li", {}, [
        el("a", { class: "nav-item", href: "#/" + item.key, "data-nav": item.key }, [
          el("span", { class: "nav-title", text: item.title, "data-nav": item.key })
        ])
      ]));
    });
  }

  function refresh() { if (app.session && app.session.authenticated) onRoute(); }

  function onRoute() {
    clearTimers();
    closeModal();
    var rawHash = String(location.hash || "");
    if (rawHash && rawHash.slice(0, 2) !== "#/") return;   // 页内锚点（如跳过导航）不参与路由
    var route = parseHash();
    if (!app.session || !app.session.authenticated) {
      if (route.page !== "login") { location.replace("#/login"); return; }
      showLogin();
      return;
    }
    if (route.page === "login") { location.replace("#/overview"); return; }
    var known = NAV.filter(function (item) { return item.key === route.page; })[0];
    if (!known) {
      toast("未知页面：" + route.page, "warn");
      location.replace("#/overview");
      return;
    }
    app.page = route.page;
    app.params = route.params;
    $$("[data-nav]").forEach(function (link) {
      link.classList.toggle("active", link.getAttribute("data-nav") === route.page);
    });
    document.title = known.title + " · CPA 面板";
    var sidebar = byId("sidebar");
    if (sidebar) sidebar.classList.remove("open");
    var view = byId("view");
    clear(view);
    window.scrollTo(0, 0);
    try {
      RENDER[route.page](view, route.params);
    } catch (error) {
      reportError(error);
      view.appendChild(errorCard(error));
    }
  }

  /* ================================================================ 登录闸门 */

  function showLogin() {
    clearTimers();
    closeModal();
    byId("app").hidden = true;
    byId("booting").hidden = true;
    var gate = byId("gate");
    gate.hidden = false;
    var version = (app.session && app.session.version) || "";
    var form = el("form", {
      class: "login-form",
      onsubmit: function (event) { event.preventDefault(); submitLogin(form); }
    }, [
      field("用户名", input({ name: "username", type: "text", autocomplete: "username", required: true })),
      field("口令", input({ name: "password", type: "password", autocomplete: "current-password", required: true })),
      el("button", { class: "btn primary", type: "submit", text: "登录" })
    ]);
    mount(gate, el("div", { class: "gate-card" }, [
      el("div", { class: "gate-brand" }, [el("span", { class: "brand-dot" }), el("h1", { text: "CPA 面板" })]),
      hint("CLIProxyAPI 账号 · 用量 · 巡检控制台" + (version ? "（v" + version + "）" : "")),
      form
    ]));
    var userInput = form.querySelector("input[name=username]");
    if (userInput) userInput.focus();
  }

  function submitLogin(form) {
    var data = new FormData(form);
    var submit = form.querySelector("button[type=submit]");
    if (submit) { submit.disabled = true; submit.textContent = "登录中…"; }
    send("POST", "/api/login", { username: data.get("username"), password: data.get("password") })
      .then(function () { return get("/api/session"); })
      .then(function (session) {
        app.session = session;
        toast("登录成功", "ok");
        enterApp();
      })
      .catch(function (error) {
        reportError(error);
        if (submit) { submit.disabled = false; submit.textContent = "登录"; }
      });
  }

  function enterApp() {
    byId("gate").hidden = true;
    byId("booting").hidden = true;
    byId("app").hidden = false;
    var version = app.session && app.session.version;
    byId("brandVer").textContent = version ? "v" + version : "";
    if (!app.bound) {
      app.bound = true;
      buildNav();
      window.addEventListener("hashchange", onRoute);
      byId("navToggle").addEventListener("click", function () {
        var sidebar = byId("sidebar");
        var open = sidebar.classList.toggle("open");
        byId("navToggle").setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
    loadNodes().then(function () {
      if (!/^#\//.test(String(location.hash || ""))) {
        location.replace("#/overview");
        return;
      }
      onRoute();
    });
  }

  function loadNodes() {
    return get("/api/nodes").then(function (data) {
      app.nodes = (data && data.nodes) || [];
      var stored = localStorage.getItem(LS_NODE);
      var ids = app.nodes.map(function (node) { return String(node.id); });
      app.nodeId = stored && ids.indexOf(String(stored)) >= 0 ? Number(stored) : null;
      var picker = byId("nodeSelect");
      clear(picker);
      picker.appendChild(el("option", { value: "", text: app.nodes.length ? "全部 / 默认节点" : "（未配置节点）" }));
      app.nodes.forEach(function (node) {
        picker.appendChild(el("option", {
          value: String(node.id),
          text: node.name + " · " + node.base_url + (node.enabled ? "" : "（已停用）")
        }));
      });
      picker.value = app.nodeId ? String(app.nodeId) : "";
      byId("navFoot").textContent = app.nodes.length ? app.nodes.length + " 个节点" : "尚未配置节点";
    }).catch(function (error) {
      reportError(error);
    });
  }

  function bindChrome() {
    byId("btnRefresh").addEventListener("click", refresh);
    byId("btnTheme").addEventListener("click", cycleTheme);
    byId("btnLogout").addEventListener("click", function () {
      send("POST", "/api/logout").catch(function () { /* 无论成败都回到登录页 */ }).then(function () {
        app.session = { authenticated: false };
        location.replace("#/login");
        showLogin();
      });
    });
    byId("nodeSelect").addEventListener("change", function (event) {
      var value = event.target.value;
      app.nodeId = value ? Number(value) : null;
      if (value) localStorage.setItem(LS_NODE, value);
      else localStorage.removeItem(LS_NODE);
      toast(value ? "已切换到节点 #" + value : "已切换到全部 / 默认节点", "info");
      refresh();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        var root = byId("modalRoot");
        if (root && !root.hidden) closeModal();
      }
    });
  }

  /* ================================================================ 页面：概览 */

  function openResultModal(title, result) {
    openModal({
      title: title,
      size: "wide",
      body: jsonBlock(result),
      actions: [{ label: "关闭", onClick: function (close) { close(); } }]
    });
  }

  function collectorLine(snapshot) {
    var keys = Object.keys(snapshot || {});
    if (!keys.length) return "采集器暂无状态（可能未启用或尚未跑过一轮）";
    return keys.map(function (key) {
      var state = snapshot[key] || {};
      var parts = ["节点 " + key + "："];
      parts.push(state.last_success_ts ? relTime(state.last_success_ts) + "成功" : "未成功");
      if (state.consecutive_errors) parts.push("连续失败 " + state.consecutive_errors + " 次");
      if (state.records) parts.push("累计 " + fmtInt(state.records) + " 条");
      if (state.duplicates) parts.push("去重 " + fmtInt(state.duplicates) + " 条");
      if (state.last_error) parts.push("错误：" + truncate(state.last_error, 80));
      return parts.join(" ");
    }).join("；");
  }

  RENDER.overview = function (host) {
    var stack = el("div", { class: "stack" });
    var body = el("div", { class: "stack" });
    stack.appendChild(pageHead("概览", "账号健康、用量与告警一览", [
      button("同步账号", function () {
        send("POST", "/api/credentials/sync", nodeQuery()).then(function (data) {
          toast("已同步 " + fmtInt(data.total) + " 条：新增 " + fmtInt(data.added) + " / 变更 " + fmtInt(data.changed) + " / 移除 " + fmtInt(data.removed), "ok");
          refresh();
        }).catch(reportError);
      }),
      button("手动采集", function () {
        send("POST", "/api/usage/collect", nodeQuery()).then(function () {
          toast("采集完成", "ok");
          refresh();
        }).catch(reportError);
      }),
      button("立即巡检", function () {
        send("POST", "/api/inspections/run", nodeQuery()).then(function (data) {
          toast("巡检完成", "ok");
          openResultModal("巡检结果", data.result);
        }).catch(reportError);
      })
    ]));
    stack.appendChild(body);
    host.appendChild(stack);
    body.appendChild(loading());

    get("/api/overview", nodeQuery()).then(function (data) {
      clear(body);
      var counts = (data.credentials && data.credentials.counts) || {};
      var providers = (data.credentials && data.credentials.providers) || [];
      var usage = data.usage || {};
      var today = usage.today || {};
      var panel = data.panel || {};
      var database = panel.database || {};
      var inspector = data.inspector || {};

      var alerts = data.alerts || [];
      if (alerts.length) {
        body.appendChild(card("告警", alerts.map(function (alert) {
          return el("div", { class: "alert " + (alert.level || "info") }, [
            el("span", { class: "alert-dot" }),
            el("div", {}, [
              el("div", { text: alert.text || "" }),
              alert.action ? el("div", { class: "muted small", text: "建议：" + alert.action }) : null
            ])
          ]);
        })));
      }

      body.appendChild(card("账号状态", [tiles([
        tile("凭证总数", fmtInt(counts.total)),
        tile("可用", fmtInt(counts.active), null, "ok"),
        tile("冷却中", fmtInt(counts.cooling), null, "warn"),
        tile("已禁用", fmtInt(counts.disabled), null, "muted"),
        tile("错误", fmtInt(counts.erroring), null, "bad"),
        tile("备用池", fmtInt(counts.standby))
      ])]));

      body.appendChild(card("今日用量", [tiles([
        tile("请求", fmtInt(today.requests)),
        tile("错误率", fmtPct(today.error_rate), fmtInt(today.errors) + " 次错误", today.errors ? "bad" : "ok"),
        tile("Tokens", fmtCompact(today.total_tokens)),
        tile("费用", fmtCost(today.cost_usd))
      ])]));

      var series = usage.series || [];
      body.appendChild(card("近 14 天请求量", series.length
        ? svgAreaChart(series.map(function (row) {
            return { label: shortDay(row.day), value: row.requests || 0 };
          }), { label: "近 14 天请求量" })
        : emptyRow("暂无用量数据（队列只有 60 秒生命期，采集器必须持续运行）")));

      var donutItems = providers.map(function (item, index) {
        return { label: item.provider || "(未知)", value: item.total || 0, color: TONES[index % TONES.length] };
      });
      var topModels = usage.top_models || [];
      var topCredentials = usage.top_credentials || [];

      body.appendChild(el("div", { class: "grid-2" }, [
        card("提供方分布", donutItems.length
          ? el("div", { class: "donut-wrap" }, [svgDonut(donutItems, { center: "凭证" }), legend(donutItems)])
          : emptyRow("暂无凭证")),
        card("模型调用 Top 8", topModels.length
          ? svgBarChart(topModels.map(function (item) {
              return { label: item.model || "(未知)", value: item.requests || 0 };
            }), { label: "模型请求量排行" })
          : emptyRow("暂无用量"), { hint: "按请求数" })
      ]));

      body.appendChild(card("账号用量 Top", dataTable([
        { title: "凭证", render: function (row) { return truncate(row.name || row.credential_index || "—", 40); } },
        { title: "提供方", render: function (row) { return row.provider || "—"; } },
        { title: "请求", class: "num", render: function (row) { return fmtInt(row.requests); } },
        { title: "Tokens", class: "num", render: function (row) { return fmtCompact(row.tokens); } },
        { title: "费用", class: "num", render: function (row) { return fmtCost(row.cost_usd); } }
      ], topCredentials, { emptyText: "暂无账号用量" })));

      body.appendChild(card("采集器", [
        el("p", { text: collectorLine(data.collector) }),
        hint("上游用量队列是消费型读取且只保留 60 秒，采集间隔必须远小于该窗口。")
      ]));

      var last = inspector.last || {};
      body.appendChild(el("div", { class: "grid-2" }, [
        card("最近一次巡检", Object.keys(last).length ? jsonBlock(last) : emptyRow("尚未运行巡检"), {
          tools: [button("查看全部", function () { navigate("inspect"); }, "tiny")]
        }),
        card("最近动作", dataTable([
          { title: "时间", render: function (row) { return fmtTs(row.ts); } },
          { title: "凭证", render: function (row) { return truncate(row.credential_name || "—", 26); } },
          { title: "动作", render: function (row) { return row.action || "—"; } },
          { title: "结果", render: function (row) { return badge(row.result || "—", row.result === "ok" ? "ok" : "warn"); } }
        ], inspector.actions || [], { emptyText: "暂无动作" }))
      ]));

      body.appendChild(card("面板", [keyValue([
        ["版本", show(panel.version)],
        ["运行时长", dur(panel.uptime_seconds)],
        ["节点数", fmtInt((data.nodes || []).length)],
        ["凭证数量", fmtInt(database.credentials)],
        ["用量事件", fmtInt(database.usage_events)],
        ["数据库", fmtBytes(database.database_bytes)],
        ["当前用户", show((app.session && app.session.username) + " / " + (app.session && app.session.role))]
      ])]));
    }).catch(function (error) {
      clear(body);
      body.appendChild(errorCard(error));
    });
  };

  /* ================================================================ 页面：账号凭证 */

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait || 300);
    };
  }

  /** OAuth 授权链接是用户主动点击的功能性外链（不带任何资源），单独放行。 */
  function externalLink(url, label) {
    return el("a", { class: "link", href: String(url || "#"), target: "_blank", rel: "noreferrer noopener", text: label || url });
  }

  RENDER.credentials = function (host, params) {
    var filters = { provider: "", state: "", q: "", standby: "", include_removed: false };
    var stack = el("div", { class: "stack" });
    var summary = el("div", { class: "stack" });
    var listBox = el("div", { class: "stack" });
    var eventsBox = el("div", { class: "stack" });

    var providerSelect = select([{ value: "", label: "全部提供方" }], "", function () {
      filters.provider = providerSelect.value; load();
    });
    var stateSelect = select([
      { value: "", label: "全部状态" },
      { value: "healthy", label: "健康" },
      { value: "cooling", label: "冷却中" },
      { value: "quota_exhausted", label: "额度耗尽" },
      { value: "unauthorized", label: "需重登" },
      { value: "disabled", label: "已禁用" },
      { value: "unknown", label: "未知" }
    ], "", function () { filters.state = stateSelect.value; load(); });
    var standbySelect = select([
      { value: "", label: "含备用池" },
      { value: "1", label: "仅备用池" },
      { value: "0", label: "排除备用池" }
    ], "", function () { filters.standby = standbySelect.value; load(); });
    var keywordInput = input({ type: "search", placeholder: "名称 / 邮箱 / 索引", oninput: debounce(function () { filters.q = keywordInput.value.trim(); load(); }, 350) });
    var removedInput = input({ type: "checkbox", onchange: function () { filters.include_removed = removedInput.checked; load(); } });

    stack.appendChild(pageHead("账号凭证", "上游 auth-files 的本地镜像；禁用 / 优先级会同步推给上游", [
      button("同步", doSync),
      button("导入 JSON", openImportDialog),
      button("新增登录", openOauthDialog),
      button("刷新", load)
    ]));
    stack.appendChild(card(null, el("div", { class: "filters" }, [
      field("提供方", providerSelect),
      field("状态", stateSelect),
      field("备用池", standbySelect),
      field("关键词", keywordInput),
      field("选项", el("label", { class: "field inline" }, [removedInput, el("span", { text: "含已移除" })]))
    ])));
    stack.appendChild(summary);
    stack.appendChild(listBox);
    stack.appendChild(eventsBox);
    host.appendChild(stack);

    summary.appendChild(loading());
    load();

    /* ---------------------------------------------------------- 数据 */

    function load() {
      clear(summary);
      summary.appendChild(loading());
      var query = withNode({
        provider: filters.provider, state: filters.state, q: filters.q,
        standby: filters.standby, include_removed: filters.include_removed
      });
      get("/api/credentials", query).then(function (data) {
        clear(summary);
        var counts = data.counts || {};
        summary.appendChild(tiles([
          tile("筛选结果", fmtInt(data.total)),
          tile("总数", fmtInt(counts.total)),
          tile("可用", fmtInt(counts.active), null, "ok"),
          tile("冷却中", fmtInt(counts.cooling), null, "warn"),
          tile("禁用", fmtInt(counts.disabled), null, "muted"),
          tile("错误", fmtInt(counts.erroring), null, "bad")
        ]));
        fillProviders(data.providers || []);
        clear(listBox);
        listBox.appendChild(card("凭证", dataTable(columns(), data.credentials || [], {
          emptyText: "暂无凭证，先「同步」或「导入 JSON」"
        }), { hint: "点击名称查看详情" }));
      }).catch(function (error) {
        clear(summary);
        summary.appendChild(errorCard(error));
      });
      loadEvents();
    }

    function fillProviders(providers) {
      var current = filters.provider;
      clear(providerSelect);
      providerSelect.appendChild(el("option", { value: "", text: "全部提供方" }));
      providers.forEach(function (item) {
        providerSelect.appendChild(el("option", {
          value: item.provider || "",
          text: (item.provider || "(未知)") + "（总数 " + fmtInt(item.total) + " / 健康 " + fmtInt(item.healthy) + "）"
        }));
      });
      providerSelect.value = current;
    }

    function columns() {
      return [
        {
          title: "名称",
          render: function (row) {
            return el("button", {
              class: "btn link", type: "button", text: truncate(row.name || "—", 30),
              onclick: function () { openDetail(row.id); }
            });
          }
        },
        { title: "提供方", render: function (row) { return row.provider || "—"; } },
        { title: "账号", render: function (row) { return truncate(row.email || row.account || "—", 28); } },
        {
          title: "状态",
          render: function (row) {
            var wrap = el("div", { class: "row" }, [stateBadge(row.state)]);
            if (row.state_reason) wrap.appendChild(el("span", { class: "muted small", title: row.state_reason, text: truncate(row.state_reason, 24) }));
            if (row.standby) wrap.appendChild(badge("备用", "info"));
            return wrap;
          }
        },
        { title: "成功/失败", class: "num", render: function (row) { return fmtInt(row.success) + " / " + fmtInt(row.failed); } },
        { title: "优先级", class: "num", render: function (row) { return row.priority === null || row.priority === undefined ? "—" : fmtInt(row.priority); } },
        {
          title: "冷却至",
          render: function (row) {
            if (!row.next_retry_after) return "—";
            return row.next_retry_after * 1000 > Date.now()
              ? badge(relTime(row.next_retry_after), "warn")
              : el("span", { class: "muted small", text: fmtTs(row.next_retry_after) });
          }
        },
        {
          title: "操作", class: "actions",
          render: function (row) {
            var group = el("div", { class: "row" }, [
              button("详情", function () { openDetail(row.id); }, "tiny"),
              button(row.disabled ? "启用" : "禁用", function () {
                patch(row, { disabled: !row.disabled }, row.disabled ? "已启用" : "已禁用");
              }, "tiny"),
              button("刷新", function () { act(row, "refresh", "刷新"); }, "tiny"),
              button(row.standby ? "恢复" : "备用", function () {
                act(row, row.standby ? "promote" : "standby", row.standby ? "恢复" : "转备用");
              }, "tiny"),
              button("删除", function () {
                confirmDialog("删除凭证", "将从上游删除 «" + row.name + "»，此操作不可撤销。", function () {
                  act(row, "delete", "删除");
                }, "删除");
              }, "tiny danger")
            ]);
            return group;
          }
        }
      ];
    }

    function patch(row, body, label) {
      send("PATCH", "/api/credentials/" + row.id, body).then(function () {
        toast(label + "：" + row.name, "ok");
        load();
      }).catch(reportError);
    }

    function act(row, action, label) {
      send("POST", "/api/credentials/" + row.id + "/action", { action: action }).then(function (result) {
        if (result && result.ok === false) {
          toast(label + "失败：" + (result.detail || result.result || "未知原因"), "bad");
        } else {
          toast(label + "成功：" + row.name, "ok");
        }
        load();
      }).catch(reportError);
    }

    function doSync() {
      send("POST", "/api/credentials/sync", nodeQuery()).then(function (data) {
        toast("已同步 " + fmtInt(data.total) + " 条：+ " + fmtInt(data.added) + " / ~ " + fmtInt(data.changed) + " / - " + fmtInt(data.removed), "ok");
        load();
      }).catch(reportError);
    }

    function loadEvents() {
      get("/api/credentials/events", withNode({ limit: 40 })).then(function (data) {
        clear(eventsBox);
        eventsBox.appendChild(card("凭证变更事件", dataTable([
          { title: "时间", render: function (row) { return fmtTs(row.ts, true); } },
          { title: "凭证", render: function (row) { return truncate(row.credential_name || "—", 30); } },
          { title: "类型", render: function (row) { return badge(row.kind || "—", row.kind === "removed" ? "bad" : "info"); } },
          { title: "说明", render: function (row) { return truncate(row.detail || "—", 60); } }
        ], (data && data.events) || [], { emptyText: "暂无事件" })));
      }).catch(reportError);
    }

    /* ---------------------------------------------------------- 详情 / 模型 */

    function openDetail(id) {
      var body = openModal({
        title: "凭证详情", size: "wide", body: loading(),
        actions: [{ label: "关闭", onClick: function (close) { close(); } }]
      });
      get("/api/credentials/" + id).then(function (data) {
        var row = data.credential || {};
        clear(body);
        append(body, [
          keyValue([
            ["名称", show(row.name)],
            ["auth_index", show(row.auth_index)],
            ["提供方", show(row.provider)],
            ["账号", show(row.email || row.account)],
            ["状态", el("span", {}, [stateBadge(row.state), row.state_reason ? " " + row.state_reason : ""])],
            ["账号类型 / 计划", show([row.account_type, row.plan_type].filter(Boolean).join(" / "))],
            ["项目", show(row.project_id)],
            ["优先级 / 权重", show(row.priority) + " / " + show(row.weight)],
            ["备注", show(row.note)],
            ["成功 / 失败", fmtInt(row.success) + " / " + fmtInt(row.failed)],
            ["上次刷新", fmtTs(row.last_refresh, true)],
            ["冷却至", fmtTs(row.next_retry_after, true)],
            ["订阅至", fmtTs(row.subscription_until, true)],
            ["来源", show([row.source, row.runtime_only ? "仅内存" : ""].filter(Boolean).join(" / "))],
            ["物理路径", show(row.path)],
            ["首次 / 最近出现", fmtTs(row.first_seen_at, true) + " / " + fmtTs(row.last_seen_at, true)]
          ]),
          el("div", { class: "row" }, [
            button("查看可用模型", function () { showModels(row); }),
            el("a", { class: "btn", href: safeHref("/api/credentials/" + row.id + "/download"), text: "下载 JSON" })
          ]),
          row.quota ? card("配额信号（被动观测）", jsonBlock(row.quota)) : null,
          card("近期采样", dataTable([
            { title: "时间", render: function (item) { return fmtTs(item.ts, true); } },
            { title: "状态", render: function (item) { return item.status || "—"; } },
            { title: "禁用", render: function (item) { return item.disabled ? "是" : "否"; } },
            { title: "不可用", render: function (item) { return item.unavailable ? "是" : "否"; } },
            { title: "失败", class: "num", render: function (item) { return fmtInt(item.failed); } },
            { title: "原因", render: function (item) { return truncate(item.reason || "—", 40); } }
          ], (row.samples || []).slice(0, 60), { emptyText: "暂无采样（需采集/巡检跑过一轮）" }))
        ]);
      }).catch(function (error) {
        clear(body);
        append(body, el("p", { text: "加载失败：" + error.message }));
      });
    }

    function showModels(row) {
      var body = openModal({
        title: "可用模型 · " + row.name, size: "wide", body: loading(),
        actions: [{ label: "关闭", onClick: function (close) { close(); } }]
      });
      get("/api/credentials/" + row.id + "/models").then(function (data) {
        var models = (data && data.models) || [];
        clear(body);
        append(body, models.length ? dataTable([
          { title: "ID", render: function (item) { return item.id || "—"; } },
          { title: "名称", render: function (item) { return item.display_name || "—"; } },
          { title: "类型", render: function (item) { return item.type || "—"; } },
          { title: "属主", render: function (item) { return item.owned_by || "—"; } }
        ], models) : emptyRow("上游未返回模型列表"));
      }).catch(function (error) {
        clear(body);
        append(body, el("p", { text: "加载失败：" + error.message }));
      });
    }

    /* ---------------------------------------------------------- 导入 / OAuth */

    function openImportDialog() {
      var fileInput = input({ type: "file", accept: ".json,application/json", multiple: true });
      var nodeSelect = select(app.nodes.map(function (node) {
        return { value: String(node.id), label: node.name + " · " + node.base_url };
      }), app.nodeId ? String(app.nodeId) : (app.nodes[0] ? String(app.nodes[0].id) : ""), null);
      var resultBox = el("div");
      openModal({
        title: "导入凭证 JSON",
        body: [
          hint("上游只接受 .json 文件，文件名不得包含路径分隔符。"),
          field("目标节点", nodeSelect),
          field("文件", fileInput),
          resultBox
        ],
        actions: [
          { label: "取消", onClick: function (close) { close(); } },
          {
            label: "上传", tone: "primary",
            onClick: function () {
              var files = fileInput.files;
              if (!files || !files.length) { toast("请先选择文件", "warn"); return; }
              var form = new FormData();
              for (var i = 0; i < files.length; i++) form.append("file", files[i], files[i].name);
              if (nodeSelect.value) form.append("node_id", nodeSelect.value);
              clear(resultBox);
              resultBox.appendChild(loading("上传中…"));
              send("POST", "/api/credentials/import", form).then(function (data) {
                clear(resultBox);
                resultBox.appendChild(dataTable([
                  { title: "文件", render: function (row) { return row.filename; } },
                  { title: "结果", render: function (row) { return row.ok ? badge("成功", "ok") : badge("失败", "bad"); } },
                  { title: "说明", render: function (row) { return row.error || "—"; } }
                ], data.results || []));
                toast(data.ok ? "导入完成" : "部分文件导入失败", data.ok ? "ok" : "warn");
                load();
              }).catch(function (error) {
                clear(resultBox);
                append(resultBox, el("p", { text: "上传失败：" + error.message }));
              });
            }
          }
        ]
      });
    }

    function openOauthDialog() {
      var providerSelect = select(OAUTH_PROVIDERS.map(function (name) { return { value: name, label: name }; }), "codex", null);
      var statusBox = el("div", { class: "stack" });
      var pollId = null;
      function stopPoll() { if (pollId) { clearInterval(pollId); pollId = null; } }
      openModal({
        title: "新增登录（OAuth）",
        body: [
          hint("由上游生成授权链接；在浏览器里完成登录后，本面板会自动轮询状态。"),
          field("提供方", providerSelect),
          statusBox
        ],
        actions: [
          { label: "取消", onClick: function (close) { stopPoll(); close(); } },
          {
            label: "生成链接", tone: "primary",
            onClick: function () {
              stopPoll();
              clear(statusBox);
              statusBox.appendChild(loading("请求中…"));
              send("POST", "/api/oauth/start", withNode({ provider: providerSelect.value })).then(function (data) {
                var state = data.state || "";
                var url = data.url || data.auth_url || "";
                clear(statusBox);
                var statusLine = el("p", { class: "muted small", text: "状态：等待授权…" });
                append(statusBox, [
                  url ? el("p", {}, [externalLink(url, "打开授权页面")]) : emptyRow("上游未返回授权链接"),
                  url ? jsonBlock(url) : null,
                  statusLine
                ]);
                pollId = addTimer(setInterval(function () {
                  get("/api/oauth/status", { state: state }).then(function (result) {
                    var status = String((result && result.status) || "wait");
                    statusLine.textContent = "状态：" + status;
                    if (["ok", "success", "done"].indexOf(status.toLowerCase()) >= 0) {
                      stopPoll();
                      toast("授权成功", "ok");
                      load();
                    } else if (status.toLowerCase() === "error") {
                      stopPoll();
                      toast("授权失败：" + ((result && result.error) || "未知原因"), "bad");
                    }
                  }).catch(function (error) { statusLine.textContent = "状态：" + error.message; });
                }, 3000));
              }).catch(function (error) {
                clear(statusBox);
                append(statusBox, el("p", { text: "请求失败：" + error.message }));
              });
            }
          }
        ]
      });
    }

    if (params && params[0] && /^\d+$/.test(params[0])) {
      openDetail(Number(params[0]));
    }
  };

  /* ================================================================ 页面：用量统计 */

  var METRICS = {
    requests: { label: "请求数", pick: function (row) { return row.requests || 0; }, format: fmtCompact },
    tokens: { label: "Tokens", pick: function (row) { return row.tokens || 0; }, format: fmtCompact },
    cost: { label: "费用", pick: function (row) { return row.cost_usd || 0; }, format: fmtCost },
    errors: { label: "错误数", pick: function (row) { return row.errors || 0; }, format: fmtCompact }
  };

  function openEventDetail(id) {
    var body = openModal({
      title: "用量记录 #" + id, size: "wide", body: loading(),
      actions: [{ label: "关闭", onClick: function (close) { close(); } }]
    });
    get("/api/usage/events/" + id).then(function (data) {
      var row = data.event || {};
      clear(body);
      append(body, [
        keyValue([
          ["时间", fmtTs(row.ts, true)],
          ["模型", show(row.model)],
          ["提供方", show(row.provider)],
          ["凭证索引", show(row.credential_index)],
          ["凭证名", show(row.credential_label)],
          ["下游 Key", show(row.api_key_masked)],
          ["端点", show(row.endpoint)],
          ["状态", show(row.status) + (row.is_error ? "（错误）" : "")],
          ["HTTP", show(row.http_status)],
          ["延迟", row.latency_ms === null || row.latency_ms === undefined ? "—" : fmtInt(row.latency_ms) + " ms"],
          ["输入/输出 Tokens", fmtInt(row.input_tokens) + " / " + fmtInt(row.output_tokens)],
          ["推理/缓存 Tokens", fmtInt(row.reasoning_tokens) + " / " + fmtInt(row.cached_tokens)],
          ["费用", fmtCost(row.cost_usd)],
          ["写入时间", fmtTs(row.collected_at, true)]
        ]),
        row.error ? card("错误", el("p", { class: "mono", text: row.error })) : null,
        card("原始记录（未经归一化的上游 JSON）", jsonBlock(data.raw))
      ]);
    }).catch(function (error) {
      clear(body);
      append(body, el("p", { text: "加载失败：" + error.message }));
    });
  }

  RENDER.usage = function (host, params) {
    var state = { days: 7, metric: "requests", errorsOnly: false, model: "", offset: 0, limit: app.pageSize || 50 };
    var stack = el("div", { class: "stack" });
    var tilesBox = el("div", { class: "stack" });
    var chartBox = el("div", { class: "stack" });
    var modelsBox = el("div", { class: "stack" });
    var credsBox = el("div", { class: "stack" });
    var keysBox = el("div", { class: "stack" });
    var eventsBox = el("div", { class: "stack" });

    var rangeSelect = select([
      { value: "1", label: "今日" },
      { value: "7", label: "近 7 天" },
      { value: "14", label: "近 14 天" },
      { value: "30", label: "近 30 天" }
    ], "7", function () { state.days = Number(rangeSelect.value) || 7; reload(); });
    var metricSelect = select(Object.keys(METRICS).map(function (key) {
      return { value: key, label: METRICS[key].label };
    }), state.metric, function () { state.metric = metricSelect.value; reload(); });
    var errorsInput = input({ type: "checkbox", onchange: function () { state.errorsOnly = errorsInput.checked; state.offset = 0; loadEvents(); } });
    var modelInput = input({ type: "search", placeholder: "精确模型名", oninput: debounce(function () { state.model = modelInput.value.trim(); state.offset = 0; loadEvents(); }, 350) });

    stack.appendChild(pageHead("用量统计", "数据来自上游消费型队列，由采集器持久化到本地库", [
      button("手动采集", function () {
        send("POST", "/api/usage/collect", nodeQuery()).then(function () { toast("采集完成", "ok"); reload(); }).catch(reportError);
      }),
      button("刷新", reload)
    ]));
    stack.appendChild(card(null, el("div", { class: "filters" }, [
      field("区间", rangeSelect),
      field("趋势指标", metricSelect),
      field("事件筛选", el("label", { class: "field inline" }, [errorsInput, el("span", { text: "仅错误" })])),
      field("模型", modelInput)
    ])));
    stack.appendChild(tilesBox);
    stack.appendChild(chartBox);
    stack.appendChild(el("div", { class: "grid-2" }, [modelsBox, credsBox]));
    stack.appendChild(keysBox);
    stack.appendChild(eventsBox);
    host.appendChild(stack);

    reload();

    function reload() {
      loadSummary();
      loadSeries();
      loadModels();
      loadCredentials();
      loadKeys();
      loadEvents();
    }

    function block(target, promise, builder) {
      mount(target, loading());
      promise.then(function (data) {
        mount(target, builder(data) || emptyRow());
      }).catch(function (error) {
        mount(target, errorCard(error));
      });
    }

    function loadSummary() {
      block(tilesBox, get("/api/usage/summary", withNode({ days: state.days })), function (data) {
        var summary = data.summary || {};
        var today = data.today || {};
        return el("div", { class: "stack" }, [
          card("区间汇总（" + state.days + " 天）", [tiles([
            tile("请求", fmtInt(summary.requests)),
            tile("错误", fmtInt(summary.errors), "错误率 " + fmtPct(summary.error_rate), summary.errors ? "bad" : "ok"),
            tile("Tokens", fmtCompact(summary.total_tokens), "输入 " + fmtCompact(summary.input_tokens) + " / 输出 " + fmtCompact(summary.output_tokens)),
            tile("费用", fmtCost(summary.cost_usd)),
            tile("今日请求", fmtInt(today.requests)),
            tile("今日费用", fmtCost(today.cost_usd))
          ])]),
          card("采集器", [
            el("p", { text: collectorLine(data.collector) }),
            keyValue([
              ["已持久化事件", fmtInt(((data.totals || {}).events) || 0)],
              ["最早", fmtTs((data.totals || {}).first_ts, true)],
              ["最新", fmtTs((data.totals || {}).last_ts, true)]
            ])
          ])
        ]);
      });
    }

    function loadSeries() {
      block(chartBox, get("/api/usage/series", withNode({ days: state.days })), function (data) {
        var series = (data && data.series) || [];
        var metric = METRICS[state.metric] || METRICS.requests;
        return card("趋势 · " + metric.label + "（" + state.days + " 天）", series.length
          ? svgAreaChart(series.map(function (row) {
              return { label: shortDay(row.day), value: metric.pick(row) };
            }), { label: metric.label + "趋势", format: metric.format })
          : emptyRow("暂无数据"));
      });
    }

    function loadModels() {
      block(modelsBox, get("/api/usage/models", withNode({ days: state.days, limit: 30 })), function (data) {
        var models = (data && data.models) || [];
        return card("模型排行", models.length ? el("div", { class: "stack" }, [
          svgBarChart(models.map(function (row) {
            return { label: row.model || "(未知)", value: row.requests || 0 };
          }), { label: "模型请求量排行" }),
          dataTable([
            { title: "模型", render: function (row) { return row.model || "—"; } },
            { title: "请求", class: "num", render: function (row) { return fmtInt(row.requests); } },
            { title: "错误", class: "num", render: function (row) { return fmtInt(row.errors); } },
            { title: "Tokens", class: "num", render: function (row) { return fmtCompact(num(row.input_tokens) + num(row.output_tokens) + num(row.reasoning_tokens)); } },
            { title: "费用", class: "num", render: function (row) { return fmtCost(row.cost_usd); } }
          ], models.slice(0, 12))
        ]) : emptyRow("暂无模型用量"));
      });
    }

    function loadCredentials() {
      block(credsBox, get("/api/usage/credentials", withNode({ days: state.days, limit: 50 })), function (data) {
        var rows = (data && data.credentials) || [];
        return card("账号排行", dataTable([
          { title: "凭证", render: function (row) { return truncate(row.name || row.credential_index || "—", 30); } },
          { title: "提供方", render: function (row) { return row.provider || "—"; } },
          { title: "请求", class: "num", render: function (row) { return fmtInt(row.requests); } },
          { title: "错误", class: "num", render: function (row) { return fmtInt(row.errors); } },
          { title: "费用", class: "num", render: function (row) { return fmtCost(row.cost_usd); } }
        ], rows.slice(0, 10), { emptyText: "暂无账号用量" }));
      });
    }

    function loadKeys() {
      block(keysBox, get("/api/usage/keys", withNode({ days: state.days })), function (data) {
        var rows = (data && data.keys) || [];
        return card("下游 Key 用量", dataTable([
          { title: "Key", render: function (row) { return el("span", { class: "mono", text: row.api_key_masked || maskKey(row.api_key) }); } },
          { title: "请求", class: "num", render: function (row) { return fmtInt(row.requests); } },
          { title: "错误", class: "num", render: function (row) { return fmtInt(row.errors); } },
          { title: "Tokens", class: "num", render: function (row) { return fmtCompact(row.tokens); } },
          { title: "费用", class: "num", render: function (row) { return fmtCost(row.cost_usd); } }
        ], rows, { emptyText: "暂无 Key 用量" }));
      });
    }

    function loadEvents() {
      var query = withNode({
        limit: state.limit, offset: state.offset, errors_only: state.errorsOnly, model: state.model
      });
      block(eventsBox, get("/api/usage/events", query), function (data) {
        var rows = (data && data.events) || [];
        var prev = button("上一页", function () { state.offset = Math.max(0, state.offset - state.limit); loadEvents(); }, "tiny");
        var next = button("下一页", function () { state.offset += state.limit; loadEvents(); }, "tiny");
        prev.disabled = state.offset <= 0;
        next.disabled = rows.length < state.limit;
        return card("用量事件（第 " + (Math.floor(state.offset / state.limit) + 1) + " 页）", dataTable([
          { title: "时间", render: function (row) { return fmtTs(row.ts); } },
          { title: "模型", render: function (row) { return truncate(row.model || "—", 26); } },
          { title: "凭证", render: function (row) { return truncate(row.credential_label || row.credential_index || "—", 20); } },
          { title: "Key", render: function (row) { return row.api_key_masked || "—"; } },
          { title: "状态", render: function (row) { return row.is_error ? badge("错误", "bad") : badge(row.http_status ? String(row.http_status) : "ok", "ok"); } },
          { title: "Tokens", class: "num", render: function (row) { return fmtCompact(row.total_tokens); } },
          { title: "延迟", class: "num", render: function (row) { return row.latency_ms === null || row.latency_ms === undefined ? "—" : fmtInt(row.latency_ms) + "ms"; } },
          {
            title: "详情", class: "actions",
            render: function (row) { return button("查看", function () { openEventDetail(row.id); }, "tiny"); }
          }
        ], rows, { emptyText: "暂无记录" }), { tools: [prev, next] });
      });
    }

    if (params && params[0] === "events" && params[1] && /^\d+$/.test(params[1])) {
      openEventDetail(Number(params[1]));
    }
  };

  /* ================================================================ 页面：下游 Key */

  RENDER.keys = function (host) {
    var stack = el("div", { class: "stack" });
    var body = el("div", { class: "stack" });
    var keyInput = input({ type: "text", placeholder: "粘贴完整 Key", autocomplete: "off" });

    stack.appendChild(pageHead("下游 Key", "上游 api-keys 是整表替换语义，面板做读-改-写", [
      button("同步", function () {
        send("POST", "/api/keys/sync", nodeQuery()).then(function (data) {
          toast("已同步：新增 " + fmtInt(data.added) + " / 移除 " + fmtInt(data.removed) + " / 共 " + fmtInt(data.total), "ok");
          load();
        }).catch(reportError);
      }),
      button("刷新", load)
    ]));
    stack.appendChild(card("添加 Key", el("div", { class: "filters" }, [
      field("完整 Key", keyInput),
      button("添加", add, "primary")
    ]), { hint: "面板不回显明文，只保存掩码与哈希" }));
    stack.appendChild(body);
    host.appendChild(stack);

    load();

    function load() {
      mount(body, loading());
      get("/api/keys", nodeQuery()).then(function (data) {
        var rows = (data && data.keys) || [];
        var requests = 0, tokens = 0, cost = 0;
        rows.forEach(function (row) {
          var usage = row.usage || {};
          requests += num(usage.requests);
          tokens += num(usage.input_tokens) + num(usage.output_tokens);
          cost += num(usage.cost_usd);
        });
        mount(body, el("div", { class: "stack" }, [
          tiles([
            tile("Key 数量", fmtInt(rows.length)),
            tile("累计请求", fmtInt(requests)),
            tile("累计 Tokens", fmtCompact(tokens)),
            tile("累计费用", fmtCost(cost))
          ]),
          card("Key 列表", dataTable([
            { title: "Key（掩码）", render: function (row) { return el("span", { class: "mono", text: row.key_masked || "—" }); } },
            { title: "状态", render: function (row) { return row.present ? badge("在线", "ok") : badge("已消失", "muted"); } },
            { title: "首次出现", render: function (row) { return fmtTs(row.first_seen_at, true); } },
            { title: "最近出现", render: function (row) { return fmtTs(row.last_seen_at, true); } },
            { title: "请求", class: "num", render: function (row) { return fmtInt((row.usage || {}).requests); } },
            { title: "Tokens", class: "num", render: function (row) { return fmtCompact(num((row.usage || {}).input_tokens) + num((row.usage || {}).output_tokens)); } },
            { title: "费用", class: "num", render: function (row) { return fmtCost((row.usage || {}).cost_usd); } },
            {
              title: "操作", class: "actions",
              render: function (row) { return button("移除", function () { removeKey(row); }, "tiny danger"); }
            }
          ], rows, { emptyText: "暂无 Key，先「同步」或直接添加" }))
        ]));
      }).catch(function (error) { mount(body, errorCard(error)); });
    }

    function add() {
      var key = keyInput.value.trim();
      if (!key) { toast("请输入完整 Key", "warn"); return; }
      send("POST", "/api/keys", withNode({ key: key })).then(function (data) {
        toast(data.already_exists ? "该 Key 已存在" : "已添加，共 " + fmtInt(data.count) + " 个", "ok");
        keyInput.value = "";
        load();
      }).catch(reportError);
    }

    function removeKey(row) {
      if (!isMasked(row.key_masked)) { doRemove(row.key_masked); return; }
      var paste = input({ type: "text", placeholder: "粘贴要移除的完整 Key", autocomplete: "off" });
      openModal({
        title: "移除下游 Key",
        body: [
          hint("上游要求提交完整 Key；面板只存掩码，无法反推明文，所以需要你贴一次。"),
          keyValue([["面板记录", el("span", { class: "mono", text: row.key_masked || "—" })]]),
          field("完整 Key", paste)
        ],
        actions: [
          { label: "取消", onClick: function (close) { close(); } },
          {
            label: "移除", tone: "danger",
            onClick: function (close) {
              var value = paste.value.trim();
              if (!value) { toast("请输入完整 Key", "warn"); return; }
              close();
              doRemove(value);
            }
          }
        ]
      });
    }

    function doRemove(key) {
      send("DELETE", "/api/keys", withNode({ key: key })).then(function (data) {
        toast("已移除，剩 " + fmtInt(data.count) + " 个", "ok");
        load();
      }).catch(reportError);
    }
  };

  /* ================================================================ 页面：CPA 节点 */

  RENDER.nodes = function (host) {
    var stack = el("div", { class: "stack" });
    var body = el("div", { class: "stack" });

    stack.appendChild(pageHead("CPA 节点", "面板对接的 CLIProxyAPI 实例（使用管理密钥访问）", [
      button("新增节点", function () { openNodeDialog(null); }, "primary"),
      button("刷新", load)
    ]));
    stack.appendChild(body);
    host.appendChild(stack);

    load();

    function load() {
      mount(body, loading());
      get("/api/nodes").then(function (data) {
        var rows = (data && data.nodes) || [];
        var enabled = rows.filter(function (row) { return row.enabled; }).length;
        var reachable = rows.filter(function (row) { return row.last_ok_at; }).length;
        mount(body, el("div", { class: "stack" }, [
          tiles([
            tile("节点数", fmtInt(rows.length)),
            tile("已启用", fmtInt(enabled), null, "ok"),
            tile("曾探测成功", fmtInt(reachable))
          ]),
          card("节点列表", dataTable([
            { title: "名称", render: function (row) { return row.name || "—"; } },
            { title: "地址", render: function (row) { return el("span", { class: "mono", text: row.base_url || "—" }); } },
            {
              title: "API 前缀",
              render: function (row) {
                var text = row.api_prefix || "auto";
                if (row.detected_prefix) text += " → " + row.detected_prefix;
                return badge(text, row.detected_prefix ? "ok" : "muted");
              }
            },
            { title: "密钥", render: function (row) { return row.has_key ? badge("已设置", "ok") : badge("未设置", "bad"); } },
            { title: "启用", render: function (row) { return row.enabled ? "是" : "否"; } },
            { title: "版本", render: function (row) { return row.version || "—"; } },
            { title: "上次成功", render: function (row) { return row.last_ok_at ? relTime(row.last_ok_at) : "—"; } },
            { title: "凭证", class: "num", render: function (row) { var c = row.counts || {}; return fmtInt(c.total) + " / 可用 " + fmtInt(c.active); } },
            { title: "上次错误", render: function (row) { return row.last_error ? el("span", { class: "muted small", text: truncate(row.last_error, 42) }) : "—"; } },
            {
              title: "操作", class: "actions",
              render: function (row) {
                return el("div", { class: "row" }, [
                  button("测试", function () { testNode(row); }, "tiny"),
                  button("编辑", function () { openNodeDialog(row); }, "tiny"),
                  button("删除", function () {
                    confirmDialog("删除节点", "将从面板移除 «" + row.name + "»（不会影响上游）。", function () {
                      send("DELETE", "/api/nodes/" + row.id).then(function () {
                        toast("已删除", "ok");
                        loadNodes();
                        load();
                      }).catch(reportError);
                    }, "删除");
                  }, "tiny danger")
                ]);
              }
            }
          ], rows, { emptyText: "尚未配置节点；无法访问任何上游数据" }))
        ]));
      }).catch(function (error) { mount(body, errorCard(error)); });
    }

    function testNode(row) {
      toast("正在探测 " + row.name + "…", "info");
      send("POST", "/api/nodes/" + row.id + "/test").then(function (result) {
        if (result && result.ok) {
          toast("探测成功：前缀 " + (result.prefix || "?") + (result.version ? "，版本 " + result.version : ""), "ok");
        } else {
          toast("探测失败：" + ((result && result.error) || "未知原因"), "bad");
        }
        loadNodes();
        load();
      }).catch(reportError);
    }

    function openNodeDialog(row) {
      var editing = !!row;
      var nameInput = input({ type: "text", value: editing ? row.name : "default" });
      var urlInput = input({ type: "text", placeholder: "http://127.0.0.1:8317", value: editing ? row.base_url : "" });
      var keyInput = input({
        type: "password", autocomplete: "off",
        placeholder: editing && row.has_key ? "已设置（留空保持不变）" : "上游管理密钥"
      });
      var prefixSelect = select([
        { value: "auto", label: "auto（自动探测）" },
        { value: "v0", label: "v0（旧前缀）" },
        { value: "v8", label: "v8（新前缀）" }
      ], editing ? row.api_prefix : "auto", null);
      var enabledInput = input({ type: "checkbox", checked: editing ? !!row.enabled : true });

      openModal({
        title: editing ? "编辑节点 · " + row.name : "新增节点",
        body: [
          hint("base_url 要指向 CPA 根地址（不含 /v0 或 /v8）。管理密钥只用于面板→上游，不会写进日志。"),
          el("div", { class: "form-grid" }, [
            field("名称", nameInput),
            field("base_url", urlInput),
            field("管理密钥", keyInput),
            field("API 前缀", prefixSelect),
            field("状态", el("label", { class: "field inline" }, [enabledInput, el("span", { text: "启用" })]))
          ])
        ],
        actions: [
          { label: "取消", onClick: function (close) { close(); } },
          {
            label: editing ? "保存" : "创建", tone: "primary",
            onClick: function (close) {
              var payload = {
                name: nameInput.value.trim() || "default",
                base_url: urlInput.value.trim().replace(/\/+$/, ""),
                api_prefix: prefixSelect.value,
                enabled: enabledInput.checked
              };
              if (!/^https?:\/\//.test(payload.base_url)) { toast("base_url 必须以 http:// 或 https:// 开头", "warn"); return; }
              var key = keyInput.value.trim();
              if (key) payload.management_key = key;
              var request = editing
                ? send("PATCH", "/api/nodes/" + row.id, payload)
                : send("POST", "/api/nodes", payload);
              request.then(function () {
                close();
                toast(editing ? "已保存" : "已创建", "ok");
                loadNodes();
                load();
              }).catch(reportError);
            }
          }
        ]
      });
    }
  };

  /* ================================================================ 页面：巡检与动作 */

  RENDER.inspect = function (host) {
    var stack = el("div", { class: "stack" });
    var snapshotBox = el("div", { class: "stack" });
    var historyBox = el("div", { class: "stack" });
    var actionsBox = el("div", { class: "stack" });
    var modeSelect = select([
      { value: "keep", label: "按配置（不动 dry_run）" },
      { value: "true", label: "仅计划（dry_run = true）" },
      { value: "false", label: "实际执行（dry_run = false）" }
    ], "keep", null);

    stack.appendChild(pageHead("巡检与动作", "按状态处置账号：只计划默认不落动作（dry_run）", [
      button("运行巡检", run, "primary"),
      button("刷新", load)
    ]));
    stack.appendChild(card("运行设置", el("div", { class: "filters" }, [
      field("模式", modeSelect),
      button("立即运行", run)
    ]), { hint: "严重动作（删除凭证）受配置开关与 max_deletes_per_run 限制" }));
    stack.appendChild(snapshotBox);
    stack.appendChild(historyBox);
    stack.appendChild(actionsBox);
    host.appendChild(stack);

    load();

    function run() {
      var payload = withNode({});
      if (modeSelect.value === "true") payload.dry_run = true;
      if (modeSelect.value === "false") payload.dry_run = false;
      send("POST", "/api/inspections/run", payload).then(function (data) {
        toast("巡检完成", "ok");
        openResultModal("巡检结果", data.result);
        load();
      }).catch(reportError);
    }

    function load() {
      mount(snapshotBox, loading());
      get("/api/inspections", withNode({ limit: 30 })).then(function (data) {
        var rows = data.inspections || [];
        var last = data.last || {};
        var latest = rows[0] || {};
        var parts = [];
        if (rows.length) {
          parts.push(tiles([
            tile("扫描", fmtInt(latest.scanned)),
            tile("活跃", fmtInt(latest.active), null, "ok"),
            tile("需重登", fmtInt(latest.unauthorized), null, latest.unauthorized ? "bad" : ""),
            tile("额度耗尽", fmtInt(latest.quota_exhausted), null, "warn"),
            tile("冷却", fmtInt(latest.cooling), null, "warn"),
            tile("计划 / 执行", fmtInt(latest.planned) + " / " + fmtInt(latest.executed)),
            tile("失败", fmtInt(latest.failures), null, latest.failures ? "bad" : "")
          ]));
        }
        parts.push(card("最近一次内存快照", Object.keys(last).length ? jsonBlock(last) : emptyRow("尚未运行巡检")));
        mount(snapshotBox, el("div", { class: "stack" }, parts));
        mount(historyBox, card("巡检历史", dataTable([
          { title: "#", class: "num", render: function (row) { return fmtInt(row.id); } },
          { title: "模式", render: function (row) { return row.mode || "—"; } },
          { title: "开始", render: function (row) { return fmtTs(row.started_at, true); } },
          { title: "结束", render: function (row) { return row.finished_at ? fmtTs(row.finished_at, true) : badge("进行中", "info"); } },
          { title: "扫描/活跃", class: "num", render: function (row) { return fmtInt(row.scanned) + " / " + fmtInt(row.active); } },
          { title: "计划/执行/失败", class: "num", render: function (row) { return fmtInt(row.planned) + " / " + fmtInt(row.executed) + " / " + fmtInt(row.failures); } },
          { title: "错误", render: function (row) { return row.error ? el("span", { class: "muted small", text: truncate(row.error, 40) }) : "—"; } },
          {
            title: "摘要", class: "actions",
            render: function (row) { return button("查看", function () { openResultModal("巡检 #" + row.id + " 摘要", row.summary || {}); }, "tiny"); }
          }
        ], rows, { emptyText: "暂无巡检记录" })));
      }).catch(function (error) { mount(snapshotBox, errorCard(error)); });

      mount(actionsBox, loading());
      get("/api/actions", withNode({ limit: 100 })).then(function (data) {
        var rows = (data && data.actions) || [];
        mount(actionsBox, card("动作记录（最近 " + fmtInt(rows.length) + " 条）", dataTable([
          { title: "时间", render: function (row) { return fmtTs(row.ts, true); } },
          { title: "凭证", render: function (row) { return truncate(row.credential_name || "—", 28); } },
          { title: "动作", render: function (row) { return badge(row.action || "—", "info"); } },
          { title: "原因", render: function (row) { return truncate(row.reason || "—", 34); } },
          { title: "结果", render: function (row) { return row.result === "ok" ? badge("成功", "ok") : badge(row.result || "—", "warn"); } },
          { title: "明细", render: function (row) { return row.detail ? el("span", { class: "muted small", text: truncate(row.detail, 48) }) : "—"; } }
        ], rows, { emptyText: "暂无动作" })));
      }).catch(function (error) { mount(actionsBox, errorCard(error)); });
    }
  };

  /* ================================================================ 页面：上游配置 */

  RENDER.upstream = function (host) {
    var stack = el("div", { class: "stack" });
    var quotaBox = el("div", { class: "stack" });
    var jsonBox = el("div", { class: "stack" });
    var yamlArea = el("textarea", { spellcheck: "false", placeholder: "加载中…" });

    stack.appendChild(pageHead("上游配置", "直接读写 CPA 的配置与配额端点（只读优先）", [button("全部刷新", function () { loadQuotas(); loadJson(); loadYaml(); })]));
    stack.appendChild(quotaBox);
    stack.appendChild(jsonBox);
    stack.appendChild(card("config.yaml（PUT 为全量替换）", [
      yamlArea,
      el("div", { class: "row" }, [
        button("重新加载", loadYaml),
        button("保存到上游", saveYaml, "danger")
      ]),
      hint("上游 PUT 是全量替换语义：保存后文件即为文本框内容。")
    ]));
    host.appendChild(stack);

    loadQuotas();
    loadJson();
    loadYaml();

    function loadQuotas() {
      mount(quotaBox, loading());
      get("/api/quotas", nodeQuery()).then(function (data) {
        var rows = (data && data.credentials) || [];
        mount(quotaBox, el("div", { class: "stack" }, [
          card("配额总览", [
            el("p", { class: "muted small", text: data.note || "" }),
            jsonBlock(data.providers)
          ], { hint: "上游 quota/providers" }),
          card("凭证配额", dataTable([
            { title: "凭证", render: function (row) { return truncate(row.name || "—", 28); } },
            { title: "提供方", render: function (row) { return row.provider || "—"; } },
            { title: "状态", render: function (row) { return stateBadge(row.state); } },
            { title: "支持主动查询", render: function (row) { return row.supports_quota ? badge("是", "ok") : badge("否", "muted"); } },
            { title: "信号", render: function (row) { return row.quota ? el("span", { class: "muted small", text: truncate(JSON.stringify(row.quota), 60) }) : "—"; } },
            { title: "订阅至", render: function (row) { return fmtTs(row.subscription_until, true); } },
            {
              title: "操作", class: "actions",
              render: function (row) {
                return button("主动查询", function () {
                  send("POST", "/api/quotas/fetch", withNode({ name: row.name })).then(function (result) {
                    toast("配额查询完成", "ok");
                    openResultModal("配额查询 · " + row.name, result.result);
                  }).catch(reportError);
                }, "tiny");
              }
            }
          ], rows, { emptyText: "暂无可用配额信息" }))
        ]));
      }).catch(function (error) { mount(quotaBox, errorCard(error)); });
    }

    function loadJson() {
      mount(jsonBox, loading());
      get("/api/upstream/config", nodeQuery()).then(function (data) {
        mount(jsonBox, card("上游配置 JSON", jsonBlock(data.config)));
      }).catch(function (error) { mount(jsonBox, errorCard(error)); });
    }

    function loadYaml() {
      yamlArea.value = "加载中…";
      get("/api/upstream/config-yaml", nodeQuery()).then(function (data) {
        yamlArea.value = (data && data.yaml) || "";
      }).catch(function (error) {
        yamlArea.value = "";
        toast("加载 config.yaml 失败：" + error.message, "bad");
      });
    }

    function saveYaml() {
      var text = yamlArea.value;
      if (!text.trim()) { toast("yaml 内容为空", "warn"); return; }
      confirmDialog("保存 config.yaml", "将用当前文本框内容全量替换上游配置文件，确定继续？", function () {
        send("PUT", "/api/upstream/config-yaml", withNode({ yaml: text })).then(function (data) {
          toast("已保存（" + fmtInt(data.bytes) + " 字节）", "ok");
          loadYaml();
        }).catch(reportError);
      }, "全量替换");
    }
  };

  /* ================================================================ 页面：日志 */

  RENDER.logs = function (host) {
    var stack = el("div", { class: "stack" });
    var upstreamBox = el("div", { class: "stack" });
    var panelBox = el("div", { class: "stack" });
    var errorsBox = el("div", { class: "stack" });
    var auto = { on: false, id: null, button: null };
    var levelSelect = select([
      { value: "", label: "全部级别" },
      { value: "DEBUG", label: "DEBUG" },
      { value: "INFO", label: "INFO" },
      { value: "WARNING", label: "WARNING" },
      { value: "ERROR", label: "ERROR" }
    ], "", function () { loadPanel(); });

    function toggleAuto() {
      auto.on = !auto.on;
      if (auto.id) { clearInterval(auto.id); auto.id = null; }
      if (auto.on) {
        auto.id = addTimer(setInterval(function () { loadUpstream(); loadPanel(); }, 5000));
      }
      if (auto.button) auto.button.textContent = auto.on ? "自动刷新：开" : "自动刷新：关";
    }

    auto.button = button("自动刷新：关", toggleAuto);
    stack.appendChild(pageHead("日志", "上游服务日志、面板日志与错误日志文件", [
      auto.button,
      button("刷新", function () { loadUpstream(); loadPanel(); loadErrors(); })
    ]));
    stack.appendChild(upstreamBox);
    stack.appendChild(panelBox);
    stack.appendChild(errorsBox);
    host.appendChild(stack);

    loadUpstream();
    loadPanel();
    loadErrors();

    function loadUpstream() {
      mount(upstreamBox, loading());
      get("/api/logs", withNode({ limit: 500 })).then(function (data) {
        var lines = (data && data.lines) || [];
        mount(upstreamBox, card("上游日志（共 " + fmtInt((data && data.total) || lines.length) + " 行，显示后 " + fmtInt(lines.length) + " 行）", [
          el("pre", { class: "code log", text: lines.length ? lines.join("\n") : "（空）" })
        ], {
          tools: [button("清空上游日志", function () {
            confirmDialog("清空上游日志", "将调用上游 DELETE /logs，已写入的日志不可恢复。", function () {
              send("DELETE", "/api/logs", nodeQuery()).then(function () {
                toast("已清空", "ok");
                loadUpstream();
              }).catch(reportError);
            }, "清空");
          }, "tiny danger")]
        }));
      }).catch(function (error) { mount(upstreamBox, errorCard(error)); });
    }

    function loadPanel() {
      mount(panelBox, loading());
      get("/api/logs/panel", { limit: 300, level: levelSelect.value }).then(function (data) {
        var lines = (data && data.lines) || [];
        mount(panelBox, card("面板日志", [
          el("div", { class: "filters" }, [field("级别", levelSelect)]),
          el("pre", { class: "code log", text: lines.length ? lines.join("\n") : "（空）" })
        ]));
      }).catch(function (error) { mount(panelBox, errorCard(error)); });
    }

    function loadErrors() {
      mount(errorsBox, loading());
      get("/api/logs/errors", nodeQuery()).then(function (data) {
        var files = (data && data.files) || [];
        mount(errorsBox, card("错误日志文件（" + fmtInt(files.length) + "）", dataTable([
          {
            title: "文件",
            render: function (row) {
              var name = typeof row === "string" ? row : (row.name || row.filename || String(row));
              return el("button", { class: "btn link", type: "button", text: name, onclick: function () { viewError(name); } });
            }
          },
          {
            title: "大小", class: "num",
            render: function (row) { return typeof row === "string" ? "—" : fmtBytes(row.size); }
          },
          {
            title: "时间",
            render: function (row) { return typeof row === "string" ? "—" : fmtTs(row.mtime || row.modtime || row.updated_at, true); }
          }
        ], files, { emptyText: "上游未返回错误日志文件" })));
      }).catch(function (error) { mount(errorsBox, errorCard(error)); });
    }

    function viewError(name) {
      var body = openModal({
        title: "错误日志 · " + name, size: "wide", body: loading(),
        actions: [{ label: "关闭", onClick: function (close) { close(); } }]
      });
      get("/api/logs/errors/" + encodeURIComponent(name), nodeQuery()).then(function (data) {
        var text = plainText(data);
        clear(body);
        append(body, el("pre", { class: "code log", text: text || "（空）" }));
      }).catch(function (error) {
        clear(body);
        append(body, el("p", { text: "加载失败：" + error.message }));
      });
    }
  };

  /* ================================================================ 页面：设置 */

  var CONFIG_FIELDS = [
    { key: "log_level", label: "日志级别", type: "select", options: ["DEBUG", "INFO", "WARNING", "ERROR"] },
    { key: "public_url", label: "公开地址（反代 / OAuth 回调）", type: "text" },
    { key: "trust_proxy_headers", label: "信任代理头", type: "bool" },
    { key: "pricing_file", label: "价格文件", type: "text" },
    { key: "collector.enabled", label: "启用采集器", type: "bool" },
    { key: "collector.interval_seconds", label: "采集间隔（秒）", type: "number" },
    { key: "collector.batch_size", label: "每轮批量条数", type: "number" },
    { key: "collector.queue_retention_seconds", label: "队列保留窗口（秒）", type: "number" },
    { key: "collector.gap_warn_ratio", label: "采集间隙告警倍数", type: "number" },
    { key: "inspector.enabled", label: "启用巡检", type: "bool" },
    { key: "inspector.interval_seconds", label: "巡检间隔（秒）", type: "number" },
    { key: "inspector.dry_run", label: "仅计划（dry_run）", type: "bool" },
    { key: "inspector.disable_unauthorized", label: "自动禁用失效账号", type: "bool" },
    { key: "inspector.disable_quota_exhausted", label: "自动禁用额度耗尽账号", type: "bool" },
    { key: "inspector.delete_unauthorized", label: "自动删除失效账号（危险）", type: "bool" },
    { key: "inspector.delete_quota_exhausted", label: "自动删除额度耗尽账号（危险）", type: "bool" },
    { key: "inspector.max_deletes_per_run", label: "单轮最大删除数", type: "number" },
    { key: "inspector.standby_pool", label: "启用备用池", type: "bool" },
    { key: "inspector.target_active", label: "目标活跃数（0 = 不启用）", type: "number" },
    { key: "inspector.promote_standby_when_low", label: "低水位自动恢复备用", type: "bool" },
    { key: "notify.webhook_url", label: "Webhook URL", type: "secret" },
    { key: "notify.telegram_bot_token", label: "Telegram Bot Token", type: "secret" },
    { key: "notify.telegram_chat_id", label: "Telegram Chat ID", type: "text" },
    { key: "notify.min_interval_seconds", label: "通知去抖（秒）", type: "number" }
  ];

  function dig(object, path) {
    var current = object;
    var parts = String(path).split(".");
    for (var i = 0; i < parts.length; i++) {
      if (current === null || current === undefined || typeof current !== "object") return undefined;
      current = current[parts[i]];
    }
    return current;
  }

  RENDER.settings = function (host) {
    var stack = el("div", { class: "stack" });
    var accountBox = el("div", { class: "stack" });
    var prefsBox = el("div", { class: "stack" });
    var configBox = el("div", { class: "stack" });
    var tokenBox = el("div", { class: "stack" });
    var auditBox = el("div", { class: "stack" });
    var opsBox = el("div", { class: "stack" });

    stack.appendChild(pageHead("设置", "账户、面板配置、令牌与维护", [
      button("刷新", function () { refresh(); }),
      button("退出登录", function () { byId("btnLogout").click(); })
    ]));

    /* ---------------------------------------------------------- 账户与口令 */
    var oldInput = input({ type: "password", autocomplete: "current-password" });
    var newInput = input({ type: "password", autocomplete: "new-password" });
    var confirmInput = input({ type: "password", autocomplete: "new-password" });
    var session = app.session || {};
    accountBox.appendChild(card("账户", [
      keyValue([
        ["用户名", show(session.username)],
        ["角色", show(session.role)],
        ["认证方式", show(session.auth_via || "会话 Cookie")],
        ["面板版本", show(session.version)]
      ]),
      el("div", { class: "form-grid" }, [
        field("原口令", oldInput),
        field("新口令（至少 8 位）", newInput),
        field("重复新口令", confirmInput)
      ]),
      el("div", { class: "row" }, [button("修改口令", changePassword, "primary")])
    ]));
    stack.appendChild(accountBox);

    function changePassword() {
      if (newInput.value.length < 8) { toast("新口令至少 8 位", "warn"); return; }
      if (newInput.value !== confirmInput.value) { toast("两次输入的新口令不一致", "warn"); return; }
      send("POST", "/api/password", { old_password: oldInput.value, new_password: newInput.value })
        .then(function () {
          toast("口令已更新", "ok");
          oldInput.value = ""; newInput.value = ""; confirmInput.value = "";
        }).catch(reportError);
    }

    /* ---------------------------------------------------------- 界面偏好 */
    var themeSelect = select([
      { value: "auto", label: "跟随系统" },
      { value: "light", label: "浅色" },
      { value: "dark", label: "深色" }
    ], theme(), null);
    var autoInspect = input({ type: "checkbox" });
    var pageSizeInput = input({ type: "number", min: "10", max: "500" });
    prefsBox.appendChild(card("界面与自动化", [
      el("div", { class: "form-grid" }, [
        field("主题", themeSelect),
        field("自动巡检（inspector.auto）", el("label", { class: "field inline" }, [autoInspect, el("span", { text: "启用" })])),
        field("列表每页条数（ui.page_size）", pageSizeInput)
      ]),
      el("div", { class: "row" }, [button("保存偏好", savePrefs, "primary")])
    ]));
    stack.appendChild(prefsBox);

    get("/api/settings").then(function (data) {
      var settings = (data && data.settings) || {};
      if (settings["ui.theme"]) themeSelect.value = settings["ui.theme"];
      autoInspect.checked = (data && data.auto_inspect) !== false;
      pageSizeInput.value = settings["ui.page_size"] || 50;
    }).catch(function () { /* 偏好读取失败不阻断整页 */ });

    function savePrefs() {
      var themeValue = themeSelect.value;
      var size = parseInt(pageSizeInput.value, 10);
      localStorage.setItem(LS_THEME, themeValue);
      applyTheme(themeValue);
      send("PUT", "/api/settings", {
        "ui.theme": themeValue,
        "ui.page_size": isFinite(size) ? size : 50,
        "inspector.auto": autoInspect.checked
      }).then(function () {
        toast("偏好已保存", "ok");
      }).catch(reportError);
    }

    /* ---------------------------------------------------------- 面板配置 */
    stack.appendChild(configBox);
    loadConfig();

    function loadConfig() {
      mount(configBox, loading());
      get("/api/config").then(function (data) {
        var config = (data && data.config) || {};
        var fields = CONFIG_FIELDS.map(function (spec) { return buildConfigControl(spec, config); });
        mount(configBox, card("面板配置", [
          el("div", { class: "form-grid" }, fields.map(function (item) { return item.node; })),
          el("div", { class: "row" }, [
            button("保存修改", saveConfig, "primary"),
            hint("只提交被修改过的项；敏感值已脱敏，留空表示保持不变")
          ])
        ], { hint: "配置文件：" + show(data && data.path) }));

        function saveConfig() {
          var payload = {};
          fields.forEach(function (item) {
            var value = item.read();
            if (value === undefined) return;
            if (item.type === "secret" && (value === "" || value === null)) return;
            if (item.type !== "secret" && String(value) === String(item.original === null || item.original === undefined ? "" : item.original)) return;
            payload[item.key] = value;
          });
          if (!Object.keys(payload).length) { toast("没有检测到修改", "info"); return; }
          send("PUT", "/api/config", payload).then(function (result) {
            toast("已应用 " + Object.keys((result && result.applied) || payload).length + " 项配置", "ok");
            loadConfig();
          }).catch(reportError);
        }
      }).catch(function (error) { mount(configBox, errorCard(error)); });
    }

    function buildConfigControl(spec, config) {
      var original = dig(config, spec.key);
      var node;
      if (spec.type === "bool") {
        var box = input({ type: "checkbox", checked: !!original });
        node = field(spec.label, el("label", { class: "field inline" }, [box, el("span", { text: "启用" })]));
        return { key: spec.key, type: spec.type, original: original, node: node, read: function () { return box.checked; } };
      }
      if (spec.type === "number") {
        var number = input({ type: "number", value: original === null || original === undefined ? "" : String(original) });
        return {
          key: spec.key, type: spec.type, original: original, node: field(spec.label, number),
          read: function () {
            if (number.value === "") return undefined;
            var parsed = Number(number.value);
            return isFinite(parsed) ? parsed : undefined;
          }
        };
      }
      if (spec.type === "select") {
        var picker = select(spec.options.map(function (option) { return { value: option, label: option }; }), original, null);
        return { key: spec.key, type: spec.type, original: original, node: field(spec.label, picker), read: function () { return picker.value; } };
      }
      if (spec.type === "secret") {
        var secret = input({
          type: "password", autocomplete: "off",
          placeholder: original ? "已设置（" + original + "），留空保持不变" : "未设置"
        });
        return { key: spec.key, type: spec.type, original: original, node: field(spec.label, secret), read: function () { return secret.value.trim(); } };
      }
      var text = input({ type: "text", value: original === null || original === undefined ? "" : String(original) });
      return { key: spec.key, type: "text", original: original, node: field(spec.label, text), read: function () { return text.value.trim(); } };
    }

    /* ---------------------------------------------------------- 令牌 */
    stack.appendChild(tokenBox);
    loadTokens();

    function loadTokens() {
      mount(tokenBox, loading());
      get("/api/tokens").then(function (data) {
        var rows = (data && data.tokens) || [];
        var nameInput = input({ type: "text", placeholder: "用途备注，如 ci" });
        var roleSelect = select([
          { value: "viewer", label: "viewer（只读）" },
          { value: "admin", label: "admin（可写）" }
        ], "viewer", null);
        mount(tokenBox, card("快捷令牌", [
          el("div", { class: "filters" }, [
            field("名称", nameInput),
            field("角色", roleSelect),
            button("创建", create, "primary")
          ]),
          hint("明文只在创建时显示一次；调用时用 Authorization: Bearer <token>，不需要 CSRF 头。"),
          dataTable([
            { title: "#", class: "num", render: function (row) { return fmtInt(row.id); } },
            { title: "名称", render: function (row) { return row.name || "—"; } },
            { title: "角色", render: function (row) { return badge(row.role || "—", row.role === "admin" ? "info" : "muted"); } },
            { title: "创建", render: function (row) { return fmtTs(row.created_at, true); } },
            { title: "最近使用", render: function (row) { return row.last_used_at ? relTime(row.last_used_at) : "—"; } },
            {
              title: "操作", class: "actions",
              render: function (row) {
                return button("注销", function () {
                  confirmDialog("注销令牌", "注销后使用该令牌的脚本将立即失效。", function () {
                    send("DELETE", "/api/tokens/" + row.id).then(function () {
                      toast("已注销", "ok");
                      loadTokens();
                    }).catch(reportError);
                  }, "注销");
                }, "tiny danger");
              }
            }
          ], rows, { emptyText: "暂无令牌" })
        ]));

        function create() {
          send("POST", "/api/tokens", {
            name: nameInput.value.trim() || "token",
            role: roleSelect.value
          }).then(function (result) {
            openModal({
              title: "令牌已创建",
              body: [
                hint("明文只显示这一次，请立即保存。"),
                jsonBlock(result.token),
                el("p", { class: "muted small", text: result.note || "" })
              ],
              actions: [{ label: "我已保存", onClick: function (close) { close(); } }]
            });
            loadTokens();
          }).catch(reportError);
        }
      }).catch(function (error) { mount(tokenBox, errorCard(error)); });
    }

    /* ---------------------------------------------------------- 通知与维护 */
    var usageDays = input({ type: "number", value: "180", min: "1" });
    var sampleDays = input({ type: "number", value: "30", min: "1" });
    var auditDays = input({ type: "number", value: "90", min: "1" });
    opsBox.appendChild(card("通知与维护", [
      el("div", { class: "filters" }, [
        field("用量明细保留（天）", usageDays),
        field("采样保留（天）", sampleDays),
        field("审计保留（天）", auditDays),
        button("执行清理", prune, "danger"),
        button("测试通知", function () {
          send("POST", "/api/notify/test").then(function (result) {
            toast("通知已发送", "ok");
            openResultModal("通知测试结果", result);
          }).catch(reportError);
        })
      ]),
      hint("清理会删除过期用量明细 / 采样 / 审计并回收数据库空间，不可恢复。")
    ]));
    stack.appendChild(opsBox);

    function prune() {
      confirmDialog("执行数据清理", "将按上述保留天数删除历史数据并 VACUUM，确定继续？", function () {
        send("POST", "/api/maintenance/prune", {
          usage_days: Number(usageDays.value) || 180,
          sample_days: Number(sampleDays.value) || 30,
          audit_days: Number(auditDays.value) || 90
        }).then(function (result) {
          toast("清理完成", "ok");
          openResultModal("清理结果", result.deleted);
        }).catch(reportError);
      }, "执行清理");
    }

    /* ---------------------------------------------------------- 审计 */
    var auditFilter = input({ type: "search", placeholder: "动作关键字，如 credential.", oninput: debounce(loadAudit, 350) });
    stack.appendChild(auditBox);
    loadAudit();

    function loadAudit() {
      mount(auditBox, loading());
      get("/api/audit", { limit: 200, action: auditFilter.value.trim() }).then(function (data) {
        var rows = (data && data.audit) || [];
        mount(auditBox, card("审计日志（最近 " + fmtInt(rows.length) + " 条）", [
          el("div", { class: "filters" }, [field("筛选", auditFilter)]),
          dataTable([
            { title: "时间", render: function (row) { return fmtTs(row.ts, true); } },
            { title: "操作者", render: function (row) { return row.actor || "—"; } },
            { title: "动作", render: function (row) { return el("span", { class: "mono", text: row.action || "—" }); } },
            { title: "对象", render: function (row) { return truncate(row.target || "—", 30); } },
            { title: "明细", render: function (row) { return truncate(row.detail || "—", 46); } },
            { title: "来源 IP", render: function (row) { return row.ip || "—"; } }
          ], rows, { emptyText: "暂无审计记录" })
        ]));
      }).catch(function (error) { mount(auditBox, errorCard(error)); });
    }

    /* ---------------------------------------------------------- 健康 */
    var healthBox = el("div", { class: "stack" });
    stack.appendChild(healthBox);
    mount(healthBox, loading());
    get("/api/health").then(function (data) {
      var database = (data && data.database) || {};
      mount(healthBox, card("健康检查", keyValue([
        ["状态", data && data.ok ? badge("正常", "ok") : badge("异常", "bad")],
        ["应用 / 版本", show((data && data.app) + " " + (data && data.version))],
        ["运行时长", dur(data && data.uptime_seconds)],
        ["凭证", fmtInt(database.credentials)],
        ["用量事件", fmtInt(database.usage_events)],
        ["数据库大小", fmtBytes(database.size_bytes)],
        ["/metrics", el("span", { class: "mono small", text: location.origin + "/metrics" })]
      ])));
    }).catch(function (error) { mount(healthBox, errorCard(error)); });

    host.appendChild(stack);
  };

  /* ================================================================ 启动 */

  function boot() {
    applyTheme(theme());
    bindChrome();
    get("/api/session").then(function (session) {
      app.session = session || { authenticated: false };
      if (!app.session.authenticated) {
        showLogin();
        return null;
      }
      return get("/api/settings").then(function (data) {
        var settings = (data && data.settings) || {};
        if (settings["ui.page_size"]) app.pageSize = Number(settings["ui.page_size"]) || 50;
      }).catch(function () { /* 偏好不影响启动 */ }).then(enterApp);
    }).catch(function (error) {
      mount(byId("booting"), el("div", { class: "gate-card" }, [
        el("h1", { text: "CPA 面板" }),
        el("p", { text: "无法连接面板接口：" + error.message }),
        hint("确认服务已启动，并通过同一个地址访问面板（会话 Cookie 需同源）。"),
        button("重试", function () { location.reload(); })
      ]));
    });
  }

  boot();
})();
