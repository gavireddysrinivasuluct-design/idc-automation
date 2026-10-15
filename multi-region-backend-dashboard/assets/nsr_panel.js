/* nsr live metrics, inside every fabric page, in one colour.
 *
 * Same visual language as the ICE2 / LON14 dashboards (panels, stat strip, issue list),
 * but everything that comes from nsr (Grafana: UFM telemetry, gNMI, SNMP, ping, UFM
 * servers; Loki: UFM event log, syslog; PagerDuty) is drawn in teal and tagged "nsr", so it
 * is never confused with the NetBox / UFM / switch data of the page around it.
 *
 *   <div id="nsr-live" data-site="ice2"></div>
 *   <script src="/assets/nsr_panel.js"></script>
 *
 * On the ICE2 / LON14 engine pages the single service injects both lines; the container is
 * placed right under the page's own stat strip. URLs are absolute so the engine pages'
 * fetch() prefix shim leaves them alone.
 */
(function () {
  "use strict";
  var NSR = "#2dd4bf", ORDER = { critical: 0, major: 1, minor: 2, info: 3 };
  var css = "" +
    ".nsrp{--nsr:" + NSR + ";font:14px/1.5 'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',sans-serif;color:#e7eef7;margin:0 0 20px}" +
    ".nsrp .hd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 8px;font:500 11px/1 'JetBrains Mono',ui-monospace,monospace;letter-spacing:.18em;text-transform:uppercase;color:var(--nsr)}" +
    ".nsrp .tag{font:700 10px 'JetBrains Mono',monospace;letter-spacing:.08em;color:#04211d;background:var(--nsr);border-radius:4px;padding:2px 6px}" +
    ".nsrp .hd .sp{flex:1}.nsrp .hd a,.nsrp .hd button{font:500 12px 'IBM Plex Sans',system-ui;letter-spacing:0;text-transform:none;color:var(--nsr);background:none;border:1px solid color-mix(in srgb,var(--nsr) 45%,transparent);border-radius:6px;padding:4px 10px;cursor:pointer;text-decoration:none}" +
    ".nsrp .strip{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:0;border:1px solid color-mix(in srgb,var(--nsr) 45%,#223550);border-radius:10px;overflow:hidden}" +
    ".nsrp .strip>div{box-shadow:inset -1px -1px 0 color-mix(in srgb,var(--nsr) 35%,#223550)}" +
    ".nsrp .strip div{background:linear-gradient(180deg,#0f2a2b,#0d1f24);padding:10px 14px}" +
    ".nsrp .strip b{display:block;font:600 24px/1.1 'Chakra Petch','Arial Narrow',system-ui;color:var(--nsr);font-variant-numeric:tabular-nums}" +
    ".nsrp .strip b.bad{color:#ff5e78}.nsrp .strip b.warn{color:#f6a93b}.nsrp .strip b.na{color:#5b6d86}" +
    ".nsrp .strip span{font-size:12px;color:#8fb8b3}" +
    ".nsrp .strip .lnk a{display:block;color:inherit;text-decoration:none}.nsrp .strip .lnk a:hover span{text-decoration:underline}" +
    ".nsrp .strip em.pdl{display:flex;flex-wrap:wrap;gap:4px 8px;margin-top:6px;font:500 11.5px 'JetBrains Mono',ui-monospace,monospace;font-style:normal}" +
    ".nsrp .strip em.pdl a.pdn{color:var(--nsr);text-decoration:none}.nsrp .strip em.pdl a.pdn:hover{text-decoration:underline}" +
    ".nsrp details.box{margin-top:10px;background:#0c1d22;border:1px solid color-mix(in srgb,var(--nsr) 35%,#223550);border-radius:10px}" +
    ".nsrp details.box>summary{cursor:pointer;padding:10px 14px;font:600 15px 'Chakra Petch',system-ui;color:var(--nsr);list-style:none}" +
    ".nsrp details.box>summary::-webkit-details-marker{display:none}.nsrp .in{padding:0 14px 12px}" +
    ".nsrp .iss{border:1px solid #1d3b40;border-radius:8px;margin:6px 0;background:#0a171c}" +
    ".nsrp .iss summary{cursor:pointer;display:flex;gap:10px;align-items:center;padding:7px 10px;list-style:none}" +
    ".nsrp .iss summary::-webkit-details-marker{display:none}.nsrp .iss .n{margin-left:auto;font-weight:700}" +
    ".nsrp .sev{font:800 10px 'JetBrains Mono',monospace;padding:2px 6px;border-radius:4px;text-transform:uppercase;letter-spacing:.06em}" +
    ".nsrp .sev.critical{background:#3a1616;color:#ff5e78}.nsrp .sev.major{background:#33260b;color:#f6a93b}.nsrp .sev.minor,.nsrp .sev.info{background:#0f2e2c;color:var(--nsr)}" +
    ".nsrp .act{color:#8fb8b3;font-size:12.5px;margin:2px 10px 6px}" +
    ".nsrp table{width:100%;border-collapse:collapse;font-size:12px;margin:0 0 8px}.nsrp th,.nsrp td{text-align:left;padding:3px 10px;border-bottom:1px solid #15302f}" +
    ".nsrp th{color:#8fb8b3;font-weight:500}.nsrp td{font-family:'JetBrains Mono',ui-monospace,monospace}.nsrp td.r{text-align:right}" +
    ".nsrp .log{font:11.5px/1.5 'JetBrains Mono',monospace;max-height:220px;overflow:auto;background:#081316;border:1px solid #15302f;border-radius:8px;padding:6px 10px;white-space:pre-wrap;word-break:break-word}" +
    ".nsrp .log time{color:#5b8f89;margin-right:8px}.nsrp .muted{color:#8fb8b3}.nsrp .ok{color:var(--nsr)}" +
    ".nsrp .err{color:#fecaca;font-size:12.5px}";

  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function fmt(v) { if (v == null) return "—"; v = Number(v); return Math.abs(v) >= 1000 ? Math.round(v).toLocaleString() : (Number.isInteger(v) ? String(v) : String(+v.toFixed(2))); }
  function url(p) { return new URL(p, location.origin).href; }
  function get(p) { return fetch(url(p), { cache: "no-store" }).then(function (r) { return r.json(); }); }

  function panel(root) {
    var site = root.getAttribute("data-site"), data = {};
    if (!document.getElementById("nsrp-css")) { var st = document.createElement("style"); st.id = "nsrp-css"; st.textContent = css; document.head.appendChild(st); }
    root.className = "nsrp";
    root.innerHTML = '<div class="hd"><span class="tag">nsr</span>live metrics · Grafana · Loki · PagerDuty<span class="sp"></span><span id="nsrp-at" class="muted" style="letter-spacing:0;text-transform:none"></span><button type="button" id="nsrp-ref">⟳ Refresh</button><a href="' + url("/r/" + site + "#issues") + '" target="_top">All checks ↗</a></div><div class="strip" id="nsrp-strip"><div><b class="na">…</b><span>querying nsr</span></div></div><div id="nsrp-body"></div>';
    function load(force) {
      ["fabric", "switches", "reach", "events", "incidents"].forEach(function (s) {
        get("/api/region/" + site + "/" + s + (force ? "?refresh=1" : "")).then(function (d) { data[s] = d; draw(); }).catch(function (e) { data[s] = { error: String(e) }; draw(); });
      });
    }
    root.querySelector("#nsrp-ref").onclick = function () { load(true); };
    root.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest("a[data-jump]");
      if (!a) return;
      e.preventDefault();
      var box = root.querySelector("#nsrp-box"), list = root.querySelector("#nsrp-inc");
      if (box) box.open = true;
      if (list) { list.open = true; list.scrollIntoView({ behavior: "smooth", block: "center" }); }
    });
    load(false);
    setInterval(function () { if (!document.hidden) load(false); }, 60000);

    function checks() { return ["fabric", "switches", "reach"].reduce(function (a, s) { return a.concat((data[s] && data[s].checks) || []); }, []); }
    function c(id) { return checks().filter(function (x) { return x.id === id; })[0]; }
    function v(id) { var x = c(id); return x && x.kind === "value" ? x.value : null; }
    function rows(id) { var x = c(id); return x && x.state === "ok" ? x.rows : null; }
    // UFM boxes on the page's fabric drawing (ICE2 / LON14): the corner label shows the server's
    // HA role from the command exporter (MASTER / STANDBY), matched by the box's hostname line.
    function markUfmRoles(srv) {
      if (!srv) return;
      var byName = {};
      srv.forEach(function (r) { byName[r.labels.name || r.labels.instance] = r; });
      document.querySelectorAll("g.tile.node").forEach(function (g) {
        var host = g.querySelector("text.nh"), tag = g.querySelector("text.nv");
        var r = host && tag && byName[host.textContent.trim()];
        if (!r) return;
        var role = r.labels.role || "", master = role.indexOf("master") === 0, standby = role.indexOf("standby") === 0;
        var colour = master ? "#38d9a0" : standby ? "#4ea8ff" : r.value === 1 ? "#8b9db6" : "#ff5e78";
        tag.textContent = master ? "\u25CF MASTER" : standby ? "STANDBY" : r.value === 1 ? "UP" : "NO HA ROLE";
        tag.setAttribute("fill", colour);
        tag.style.fontWeight = "800"; tag.style.fontSize = "11.5px"; tag.style.letterSpacing = ".1em";
        // a pill behind the label so the role stands out on the box
        var box = tag.getBBox(), pill = g.querySelector("rect.ha-pill");
        if (!pill) { pill = document.createElementNS("http://www.w3.org/2000/svg", "rect"); pill.setAttribute("class", "ha-pill"); g.insertBefore(pill, tag); }
        pill.setAttribute("x", box.x - 6); pill.setAttribute("y", box.y - 3); pill.setAttribute("width", box.width + 12); pill.setAttribute("height", box.height + 6);
        pill.setAttribute("rx", 4); pill.setAttribute("fill", colour); pill.setAttribute("fill-opacity", ".16"); pill.setAttribute("stroke", colour); pill.setAttribute("stroke-opacity", ".6");
        var title = tag.querySelector("title") || tag.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "title"));
        title.textContent = "UFM HA role (command exporter): " + (role || (r.value === 1 ? "up" : "no HA role"));
      });
    }
    function sum(r) { return r ? r.reduce(function (a, x) { return a + (x.value || 0); }, 0) : null; }

    function tile(val, label, cls, href, extra, local) {
      var inner = '<b class="' + (cls || "") + '">' + val + "</b><span>" + esc(label) + (href ? (local ? " \u2193" : " \u2197") : "") + "</span>";
      var a = local ? '<a href="#nsrp-inc" data-jump="1" title="Show this site\'s incidents below">'
                    : '<a href="' + esc(href) + '" target="_blank" rel="noopener" title="Open in PagerDuty">';
      return "<div" + (href ? ' class="lnk"' : "") + ">" + (href ? a + inner + "</a>" : inner) + (extra || "") + "</div>";
    }
    // PagerDuty tile: one incident opens it in PagerDuty; several jump to this site's incident list
    // below (PagerDuty's own list can only filter by service, not by site). Each incident number
    // underneath links to that incident.
    function pdTile(inc) {
      var label = "open PagerDuty incidents";
      if (inc == null) return tile("\u2014", label, "na");
      if (!inc.length) return tile("0", label, "");
      var one = inc.length === 1 && inc[0].url, href = one || "#nsrp-inc";
      var nums = inc.length > 1 ? '<em class="pdl">' + inc.slice(0, 8).map(function (i) {
        return i.url ? '<a class="pdn" href="' + esc(i.url) + '" target="_blank" rel="noopener" title="' + esc(i.title) + '">#' + esc(i.number) + "</a>" : "#" + esc(i.number);
      }).join("") + "</em>" : "";
      return tile(fmt(inc.length), label, "bad", href, nums, !one);
    }
    function draw() {
      var strip = [], pu = v("ping_up"), pt = v("ping_total"), na = rows("ufm_nonactive"), ld = rows("ufm_linkdown"), er = rows("ufm_errors"),
        srv = rows("ufm_servers"), gr = v("gnmi_ready"), gt = v("gnmi_targets"), hw = (rows("gnmi_psu_fan") || []).length + (rows("gnmi_psu_input") || []).length,
        su = v("snmp_up"), stt = v("snmp_total"), inc = data.incidents && data.incidents.incidents;
      var ev = data.events && (data.events.streams || []).filter(function (s) { return s.id === "ufm_events"; })[0];
      strip.push(tile(pt == null ? "—" : fmt(pu) + " / " + fmt(pt), "switches answering ping", pt == null ? "na" : pu < pt ? "bad" : ""));
      strip.push(tile(v("ufm_switches") == null ? "—" : fmt(v("ufm_switches")), "switches in UFM telemetry", v("ufm_switches") == null ? "na" : ""));
      strip.push(tile(na ? fmt(sum(na)) : "—", "ports not Active (UFM)", na == null ? "na" : sum(na) ? "warn" : ""));
      strip.push(tile(ld ? fmt(ld.length) : "—", "ports with link-down · 15 min", ld == null ? "na" : ld.length ? "warn" : ""));
      strip.push(tile(er ? fmt(er.length) : "—", "ports with errors · 15 min", er == null ? "na" : er.length ? "warn" : ""));
      strip.push(tile(srv ? srv.filter(function (x) { return x.value === 1; }).length + " / " + srv.length : "—", "UFM servers up", srv == null ? "na" : srv.some(function (x) { return x.value === 0; }) ? "bad" : ""));
      if (gt != null || c("gnmi_targets")) strip.push(tile(gt == null ? "—" : fmt(gr) + " / " + fmt(gt), "gNMI sessions READY", gt == null ? "na" : gr < gt ? "warn" : ""));
      if (c("gnmi_psu_fan")) strip.push(tile(fmt(hw), "PSU / fan problems (gNMI)", hw ? "bad" : ""));
      if (c("snmp_total")) strip.push(tile(stt == null ? "—" : fmt(su) + " / " + fmt(stt), "SNMP targets up", stt == null ? "na" : su < stt ? "warn" : ""));
      strip.push(pdTile(inc));
      root.querySelector("#nsrp-strip").innerHTML = strip.join("");
      markUfmRoles(rows("ufm_servers"));
      var t = Math.max.apply(null, ["fabric", "switches", "reach", "events"].map(function (s) { return (data[s] && data[s].collected_at) || 0; }));
      if (t) root.querySelector("#nsrp-at").textContent = "updated " + new Date(t * 1000).toLocaleTimeString();

      var issues = checks().filter(function (x) { return x.kind === "rows" && x.issues > 0 && x.severity !== "info"; })
        .sort(function (a, b) { return ORDER[a.severity] - ORDER[b.severity] || b.issues - a.issues; });
      var html = issues.map(function (x) {
        var cols = x.columns || ["name"], list = x.bad_when ? x.rows.filter(function (r) { return r.value === 0; }) : x.rows;
        return '<details class="iss"' + (x.severity === "critical" ? " open" : "") + '><summary><span class="sev ' + x.severity + '">' + x.severity + "</span>" + esc(x.title) + '<span class="n">' + x.issues + "</span></summary>" +
          (x.action ? '<div class="act">' + esc(x.action) + "</div>" : "") +
          "<table><tr>" + cols.map(function (k) { return k.indexOf("value:") === 0 ? '<th style="text-align:right">' + esc(k.slice(6) || "value") + "</th>" : "<th>" + esc(k.replace("_", " ")) + "</th>"; }).join("") + "</tr>" +
          list.slice(0, 100).map(function (r) { return "<tr>" + cols.map(function (k) { return k.indexOf("value:") === 0 ? '<td class="r">' + fmt(r.value) + "</td>" : "<td>" + esc(r.labels[k] != null ? r.labels[k] : r.labels.name) + "</td>"; }).join("") + "</tr>"; }).join("") + "</table></details>";
      }).join("");
      if (ev && ev.lines && ev.lines.length) html += '<details class="iss"><summary><span class="sev major">major</span>UFM event log: critical / error / down events, last hour<span class="n">' + ev.lines.length + '</span></summary><div class="in"><div class="log">' +
        ev.lines.map(function (l) { return "<div><time>" + esc(new Date(l.ts * 1000).toISOString().slice(5, 19).replace("T", " ")) + "</time>" + esc(l.line) + "</div>"; }).join("") + "</div></div></details>";
      if (inc && inc.length) html += '<details class="iss" id="nsrp-inc" open><summary><span class="sev critical">pagerduty</span>Open BackEnd Network incidents<span class="n">' + inc.length + "</span></summary><table><tr><th>#</th><th>Incident</th><th>Status</th></tr>" +
        inc.map(function (i) { return "<tr><td>" + (i.url ? '<a style="color:var(--nsr)" target="_blank" rel="noopener" href="' + esc(i.url) + '">' + esc(i.number) + "</a>" : esc(i.number)) + "</td><td>" + esc(i.title) + "</td><td>" + esc(i.status) + "</td></tr>"; }).join("") + "</table></details>";
      var errs = checks().filter(function (x) { return x.state === "error"; });
      var loading = ["fabric", "switches", "reach", "events", "incidents"].some(function (s) { return !data[s]; });
      var head = loading ? "Live issues (nsr) · querying…" : "Live issues (nsr) · " + (issues.length + (ev && ev.lines && ev.lines.length ? 1 : 0) + (inc && inc.length ? 1 : 0) || "none");
      var body = html || (loading ? '<p class="muted">Querying Grafana, Loki and PagerDuty through nsr…</p>' : '<p class="ok">No live issues in the sources nsr has for this site.</p>');
      if (errs.length) body += '<p class="err">' + errs.length + " nsr check(s) failed: " + esc(errs[0].error) + " · " + esc(errs[0].hint || "") + "</p>";
      var open = root.querySelector("#nsrp-box") ? root.querySelector("#nsrp-box").open : true;
      root.querySelector("#nsrp-body").innerHTML = '<details class="box" id="nsrp-box"' + (open ? " open" : "") + "><summary>" + esc(head) + '</summary><div class="in">' + body + "</div></details>";
    }
  }

  function boot() {
    var root = document.getElementById("nsr-live");
    if (!root) return;
    // Engine pages: move the container directly under the page's own stat strip.
    var anchor = root.getAttribute("data-after") && document.querySelector(root.getAttribute("data-after"));
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(root, anchor.nextSibling);
    panel(root);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  window.nsrPanel = panel;
})();
