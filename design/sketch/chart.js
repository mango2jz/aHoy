/* aHoy design sketch: the session chart and the steering wheel.
   Synthetic data (fake repo "pocketwiki"). Not shipped; the app draws this with Cytoscape + a custom SVG wheel.
   Usage: AHOY.chart(svgElement, { current, wheel:{node, hover}, pan:{x,y}, interactive }) */
(function () {
  const W = 960, H = 690, R_OUT = 172, R_IN = 48, HUB = 46;
  const SLOTS = ["NW", "N", "NE", "E", "SE", "S", "SW", "W"];
  const ANG = { NW: -135, N: -90, NE: -45, E: 0, SE: 45, S: 90, SW: 135, W: 180 };
  const FIXED = {
    E: { kind: "verb", verb: "Deeper", lines: ["Deeper"] },
    SE: { kind: "verb", verb: "Show code", lines: ["Show code"] },
    S: { kind: "verb", verb: "Simpler", lines: ["Simpler"] },
    SW: { kind: "custom", verb: "Custom", lines: ["Custom…"] },
    W: { kind: "verb", verb: "Zoom out", lines: ["Zoom out"] },
  };

  // id, parent, position, radius, title, label anchor, tool calls on the incoming branch
  const NODES = [
    { id: 0, p: null, x: 250, y: 330, r: 15, t: "Overview", lx: 250, ly: 362, a: "middle", root: true,
      moves: [["Start at", "`app.py`"], ["Map the", "folders"], ["How pages", "are stored"]] },
    { id: 1, p: 0, x: 415, y: 262, r: 10, t: "How a request reaches a page", lx: 415, ly: 243, a: "middle", dots: 4 },
    { id: 4, p: 1, x: 500, y: 352, r: 9, t: "The router table in app.py", lx: 484, ly: 356, a: "end", dots: 3 },
    { id: 10, p: 4, x: 722, y: 432, r: 9, t: "Following render_page", lx: 722, ly: 458, a: "middle", dots: 6,
      moves: [["Follow `url_for`", "back into", "the router"], ["Trace where", "backlinks", "come from"], ["Compare", "with the 404", "page"]] },
    { id: 9, p: 4, x: 470, y: 470, r: 7, t: "What a decorator does", lx: 470, ly: 494, a: "middle", dots: 2 },
    { id: 5, p: 1, x: 520, y: 150, r: 8, t: "Middleware: auth and sessions", lx: 535, ly: 154, a: "start", dots: 3 },
    { id: 11, p: 5, x: 676, y: 76, r: 9, t: "Step failed", lx: 691, ly: 80, a: "start", dots: 5, err: true,
      sub: "Answer invalid twice · still steerable", moves: [] },
    { id: 2, p: 0, x: 175, y: 480, r: 10, t: "Where pages are stored", lx: 158, ly: 484, a: "end", dots: 3 },
    { id: 6, p: 2, x: 300, y: 575, r: 9, t: "The Page model and SQLite", lx: 300, ly: 601, a: "middle", dots: 4, blockedAt: 2,
      blocked: "sqlite3 tests/fixtures/wiki.db .schema",
      moves: [["Show the", "migration", "history"], ["Compare with", "`Revision`"]] },
    { id: 12, p: 6, x: 432, y: 548, r: 6, t: "What is an ORM?", lx: 445, ly: 552, a: "start", dots: 1 },
    { id: 7, p: 2, x: 92, y: 598, r: 7, t: "Revision history", lx: 92, ly: 622, a: "middle", dots: 3 },
    { id: 3, p: 0, x: 150, y: 205, r: 10, t: "How markdown becomes HTML", lx: 150, ly: 186, a: "middle", dots: 2 },
    { id: 8, p: 3, x: 290, y: 118, r: 8, t: "The sanitiser, and why", lx: 305, ly: 122, a: "start", dots: 3 },
    { id: 13, p: 8, x: 372, y: 70, r: 6, t: "What is XSS?", lx: 385, ly: 74, a: "start", dots: 1 },
    { id: 14, p: 3, x: 62, y: 112, r: 6, t: "Wiki links", lx: 75, ly: 116, a: "start", dots: 2, unread: true, sub: "New, not opened" },
  ];
  const DEFAULT_MOVES = [["Trace who", "calls this"], ["Find the", "tests for it"]];

  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const f = (n) => (Math.round(n * 100) / 100).toString();
  const pol = (cx, cy, r, deg) => [cx + r * Math.cos((deg * Math.PI) / 180), cy + r * Math.sin((deg * Math.PI) / 180)];
  const plain = (lines) => lines.join(" ").replace(/`/g, "");
  // a line with `code` spans → tspans, code in the mono
  const rich = (line) => line.split(/(`[^`]+`)/).filter(Boolean)
    .map((s) => (s[0] === "`" ? `<tspan class="m">${esc(s.slice(1, -1))}</tspan>` : esc(s))).join("");

  function slotsFor(n) {
    const moves = (n.moves || DEFAULT_MOVES).slice();
    const s = {};
    for (const k of ["E", "SE", "S", "SW", "W"]) s[k] = FIXED[k];
    const upper = ["N", "NE", "NW"];
    if (n.blocked) {
      s.NW = { kind: "allow", verb: "Allow blocked", lines: ["Allow", "blocked…"], cmd: n.blocked };
      upper.pop();
    }
    upper.forEach((k, i) => { if (moves[i]) s[k] = { kind: "move", verb: "Crew proposal", lines: moves[i], title: plain(moves[i]) }; });
    return s; // missing keys are empty slots: drawn blank, never selectable (the wheel never forces filler)
  }

  function wedgePath(cx, cy, a0, a1) {
    const [x0, y0] = pol(cx, cy, R_OUT, a0), [x1, y1] = pol(cx, cy, R_OUT, a1);
    const [x2, y2] = pol(cx, cy, R_IN, a1), [x3, y3] = pol(cx, cy, R_IN, a0);
    return `M${f(x0)},${f(y0)} A${R_OUT},${R_OUT} 0 0 1 ${f(x1)},${f(y1)} L${f(x2)},${f(y2)} A${R_IN},${R_IN} 0 0 0 ${f(x3)},${f(y3)} Z`;
  }

  function wheelSVG(n, hover, opts) {
    const cx = n.x, cy = n.y, slots = slotsFor(n);
    let face = `<g filter="url(#lift)"><circle class="wheel-face" cx="${cx}" cy="${cy}" r="${R_OUT}"/></g>`;
    let wedges = "", seps = "", labels = "";
    SLOTS.forEach((k) => {
      const a = ANG[k], s = slots[k], hov = hover === k && s;
      const cls = !s ? "wedge empty" : `wedge can ${s.kind === "move" ? "model" : s.kind === "allow" ? "allow" : "fixed"}${hov ? " hov" : ""}`;
      wedges += `<path class="${cls}" data-slot="${k}" d="${wedgePath(cx, cy, a - 22.5, a + 22.5)}"/>`;
      const [sx0, sy0] = pol(cx, cy, R_IN, a - 22.5), [sx1, sy1] = pol(cx, cy, R_OUT, a - 22.5);
      seps += `<line class="sep" x1="${f(sx0)}" y1="${f(sy0)}" x2="${f(sx1)}" y2="${f(sy1)}"/>`;
      if (!s) return;
      const R = s.kind === "move" ? 106 : s.kind === "allow" ? 108 : 112;
      const [lx, ly] = pol(cx, cy, R, a);
      const nl = s.lines.length, top = ly - ((nl - 1) * 15) / 2 + 4 + (s.kind === "allow" ? 8 : 0);
      const fixed = s.kind !== "move";
      labels += `<text class="wl${fixed ? " fixed" : ""}${hov ? " hov" : ""}" data-slot="${k}" text-anchor="middle">` +
        s.lines.map((l, i) => `<tspan x="${f(lx)}" y="${f(top + i * 15)}">${rich(l)}</tspan>`).join("") + `</text>`;
      if (s.kind === "allow") {
        const gx = lx, gy = top - 20;
        labels += `<g class="allow-glyph${hov ? " hov" : ""}" data-slot="${k}"><circle cx="${f(gx)}" cy="${f(gy)}" r="5"/><line x1="${f(gx - 3.5)}" y1="${f(gy + 3.5)}" x2="${f(gx + 3.5)}" y2="${f(gy - 3.5)}"/></g>`;
      }
    });
    // crew band over the model-move arc
    const mk = ["NW", "N", "NE"].filter((k) => slots[k] && slots[k].kind === "move");
    let band = "";
    if (mk.length) {
      const a0 = ANG[mk[0]] - 22.5, a1 = ANG[mk[mk.length - 1]] + 22.5;
      const p = (r, a) => pol(cx, cy, r, a).map(f).join(",");
      band = `<path class="crew-band" d="M${p(178, a0)} A178,178 0 0 1 ${p(178, a1)} L${p(176, a1)} A176,176 0 0 0 ${p(176, a0)} Z"/>` +
        `<path id="crewarc-${n.id}" d="M${p(187, a0)} A187,187 0 0 1 ${p(187, a1)}" fill="none"/>` +
        `<text class="crew-cap"><textPath href="#crewarc-${n.id}" startOffset="50%" text-anchor="middle">${mk.length > 1 ? "Proposed by the crew for this waypoint" : "Crew proposal"}</textPath></text>`;
    }
    const mini = n.err
      ? `<g class="node-err"><circle cx="${cx}" cy="${cy - 15}" r="6.5"/><line x1="${cx}" y1="${cy - 18}" x2="${cx}" y2="${cy - 14.5}"/><circle class="pip" cx="${cx}" cy="${cy - 12}" r=".9"/></g>`
      : `<circle class="node cur" cx="${cx}" cy="${cy - 15}" r="6"/>`;
    const hub = `<circle class="hub" cx="${cx}" cy="${cy}" r="${HUB}"/>${mini}<text class="hub-t" text-anchor="middle"><tspan x="${cx}" y="${cy + 6}">Release here</tspan><tspan x="${cx}" y="${cy + 19}">to cancel</tspan></text>`;
    let cursor = "";
    if (opts.drawCursor && hover && slots[hover]) {
      const [kx, ky] = pol(cx, cy, 150, ANG[hover] + 12);
      cursor = `<g transform="translate(${f(kx)},${f(ky)})"><path class="cursor" d="M0,0 L0,15.5 L3.8,11.9 L6.6,18.2 L9.1,17.1 L6.4,10.9 L11.6,10.9 Z"/></g>`;
    }
    const hint = `<text class="hint-a" x="${cx}" y="${cy + 198}" text-anchor="middle">Steering ${n.id ? `<tspan class="num">${n.id}</tspan> · ` : ""}${esc(n.t)}</text>` +
      `<text class="hint-b" x="${cx}" y="${cy + 215}" text-anchor="middle">Drag to a direction and release. Esc, or releasing in the centre, cancels.</text>`;
    return `${face}<g class="wheel wheel-live">${wedges}${seps}${band}${labels}${hub}${cursor}${hint}</g>`;
  }

  function chart(svg, o) {
    o = Object.assign({ current: 10, pan: { x: 0, y: 0 }, wheel: null, interactive: false }, o || {});
    const nodes = NODES.map((n) => Object.assign({}, n));
    const byId = (id) => nodes.find((n) => n.id === id);
    let current = o.current, pan = Object.assign({}, o.pan), wheel = o.wheel ? Object.assign({}, o.wheel) : null;
    let nextId = 15, hold = null, live = null;

    const pathSet = () => { const s = new Set(); let n = byId(current); while (n) { s.add(n.id); n = n.p == null ? null : byId(n.p); } return s; };

    function render() {
      const route = pathSet();
      const g = [];
      // graticule follows the pan, modulo the 120px grid
      const gx = ((pan.x % 120) + 120) % 120, gy = (((pan.y + 30) % 120) + 120) % 120;
      g.push(`<defs><filter id="lift" x="-30%" y="-30%" width="160%" height="170%"><feDropShadow dx="0" dy="8" stdDeviation="14" flood-color="#1b2434" flood-opacity=".16"/><feDropShadow dx="0" dy="1" stdDeviation="1.2" flood-color="#1b2434" flood-opacity=".14"/></filter></defs><g class="grat">`);
      for (let x = gx - 120; x <= W + 120; x += 120) g.push(`<line x1="${x}" y1="0" x2="${x}" y2="${H}"/>`);
      for (let y = gy - 120; y <= H + 120; y += 120) g.push(`<line x1="0" y1="${y}" x2="${W}" y2="${y}"/>`);
      g.push(`</g><g class="pan" style="transform:translate(${pan.x}px,${pan.y}px)">`);
      // branches and tool-call dots
      const rest = [], onroute = [];
      nodes.forEach((n) => {
        if (n.p == null) return;
        const p = byId(n.p), dx = n.x - p.x, dy = n.y - p.y, L = Math.hypot(dx, dy), ux = dx / L, uy = dy / L;
        const x1 = p.x + ux * (p.r + 3), y1 = p.y + uy * (p.r + 3), x2 = n.x - ux * (n.r + 3), y2 = n.y - uy * (n.r + 3);
        const on = route.has(n.id);
        let s = `<line class="edge${on ? " route" : n.err ? " err" : ""}" x1="${f(x1)}" y1="${f(y1)}" x2="${f(x2)}" y2="${f(y2)}"/>`;
        const count = n.dots || 0, shown = n.shown == null ? count : n.shown;
        for (let i = 1; i <= shown; i++) {
          const t = i / (count + 1), cx = x1 + (x2 - x1) * t, cy = y1 + (y2 - y1) * t;
          if (n.blockedAt === i - 1) s += `<g class="dot-blocked"><circle cx="${f(cx)}" cy="${f(cy)}" r="4.6"/><line x1="${f(cx - 3.2)}" y1="${f(cy + 3.2)}" x2="${f(cx + 3.2)}" y2="${f(cy - 3.2)}"/></g>`;
          else s += `<circle class="dot${on ? " route" : ""}${n.fresh && i === shown ? " dot-new" : ""}" cx="${f(cx)}" cy="${f(cy)}" r="${on ? 3 : 2.4}"/>`;
        }
        (on ? onroute : rest).push(s);
      });
      g.push(rest.join(""), onroute.join(""));
      // discs
      nodes.forEach((n) => {
        const on = route.has(n.id), cur = n.id === current;
        if (n.running) g.push(`<circle class="running" cx="${n.x}" cy="${n.y}" r="9" fill="none" stroke="var(--route)" stroke-width="1.4"/>`);
        if (n.err) {
          g.push(`<g class="node-err"><circle cx="${n.x}" cy="${n.y}" r="${n.r}"/><line x1="${n.x}" y1="${n.y - 4}" x2="${n.x}" y2="${n.y + 1}"/><circle class="pip" cx="${n.x}" cy="${n.y + 4}" r="1.1"/></g>`);
          if (cur) g.push(`<circle class="root-ring" cx="${n.x}" cy="${n.y}" r="${n.r + 5}"/>`);
        } else {
          g.push(`<circle class="node${cur ? " cur" : on ? " on" : ""}" cx="${n.x}" cy="${n.y}" r="${n.r}"/>`);
        }
        if (n.root) g.push(`<circle class="root-ring" cx="${n.x}" cy="${n.y}" r="${n.r + 5}"/>`);
        if (n.unread && !cur) g.push(`<circle class="unread" cx="${n.x}" cy="${n.y}" r="${n.r + 4.5}"/>`);
      });
      // labels
      nodes.forEach((n) => {
        const on = route.has(n.id), cur = n.id === current;
        const cls = n.err ? `lbl errl${cur ? " cur" : ""}` : cur ? "lbl cur" : on ? "lbl on" : "lbl";
        const num = n.root ? "" : `<tspan class="num">${n.id}</tspan><tspan dx="5">`;
        g.push(`<text class="${cls}" x="${n.lx}" y="${n.ly}" text-anchor="${n.a}">${num}${esc(n.t)}${n.root ? "" : "</tspan>"}</text>`);
        if (n.sub && !(n.unread && cur)) g.push(`<text class="lbl sub" x="${n.lx}" y="${n.ly + 15}" text-anchor="${n.a}">${esc(n.sub)}</text>`);
        if (n.blocked) {
          const bx = n.lx - 66, by = n.ly + 7;
          g.push(`<g class="badge-blocked"><rect x="${bx}" y="${by}" width="132" height="20" rx="10"/><circle cx="${bx + 13}" cy="${by + 10}" r="4.2"/><line x1="${bx + 10.2}" y1="${by + 12.8}" x2="${bx + 15.8}" y2="${by + 7.2}"/><text x="${bx + 24}" y="${by + 14}">1 command blocked</text></g>`);
        }
      });
      // hit targets (keyboard + pointer)
      if (o.interactive) nodes.forEach((n) => {
        if (n.running) return;
        g.push(`<g class="wp" tabindex="0" role="button" data-id="${n.id}" aria-label="${n.root ? "Overview" : `Waypoint ${n.id}, ${esc(n.t)}`}. Hold for 300 ms, or press Space, to steer."><circle class="focus-ring" cx="${n.x}" cy="${n.y}" r="${n.r + 6}"/><circle class="hit" cx="${n.x}" cy="${n.y}" r="${Math.max(n.r + 8, 16)}"/></g>`);
      });
      g.push(`<g id="hold-layer"></g>`);
      if (wheel) g.push(wheelSVG(byId(wheel.node), wheel.hover, { drawCursor: !o.interactive }));
      g.push(`</g>`);
      svg.innerHTML = g.join("");
      if (wheel) svg.classList.add("steering"); else svg.classList.remove("steering");
      const w = byId(current);
      svg.setAttribute("aria-label", `Session chart: ${nodes.length} waypoints. Current waypoint ${w.root ? "Overview" : w.id + ", " + w.t}.${wheel ? " Steering wheel open on " + (byId(wheel.node).root ? "Overview" : "step " + wheel.node) + "." : ""}`);
    }

    render();
    if (!o.interactive) return;

    /* ---------- interaction: hold 300 ms, drag, release ---------- */
    const foot = document.querySelector(".foot");
    const footDefault = foot ? foot.innerHTML : "";
    const say = (html) => { if (foot) { foot.innerHTML = html; foot.classList.add("live"); } };
    const keyWp = document.getElementById("k-wp"), keyTc = document.getElementById("k-tc");
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

    const capture = (e) => { try { capture(e); } catch (_) { /* synthetic pointer */ } };
    const toSvg = (e) => { const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; return pt.matrixTransform(svg.getScreenCTM().inverse()); };

    function fitPan(n) {
      const tx = Math.min(Math.max(n.x + pan.x, 184), W - 184), ty = Math.min(Math.max(n.y + pan.y, 232), 470);
      pan = { x: pan.x + (tx - (n.x + pan.x)), y: pan.y + (ty - (n.y + pan.y)) };
    }

    function startHold(id, e) {
      const n = byId(id);
      const c = 2 * Math.PI * (n.r + 7);
      const layer = svg.querySelector("#hold-layer");
      layer.innerHTML = `<circle class="hold-ring" cx="${n.x}" cy="${n.y}" r="${n.r + 7}" stroke-dasharray="${f(c)}" stroke-dashoffset="${f(c)}"/>`;
      const ring = layer.firstChild;
      ring.getBoundingClientRect();
      ring.style.transition = reduce ? "none" : "stroke-dashoffset 300ms linear";
      ring.style.strokeDashoffset = "0";
      hold = { id, x: e ? e.clientX : 0, y: e ? e.clientY : 0, timer: setTimeout(() => openWheel(id), 300) };
    }
    function cancelHold() {
      if (!hold) return;
      clearTimeout(hold.timer); hold = null;
      const layer = svg.querySelector("#hold-layer"); if (layer) layer.innerHTML = "";
    }
    function openWheel(id, viaKey) {
      hold = null;
      const n = byId(id);
      fitPan(n);
      const s = slotsFor(n);
      wheel = { node: id, hover: viaKey ? (s.N ? "N" : "E") : null };
      render();
      say(`Steering <b>${n.root ? "Overview" : "step " + n.id}</b>. ${viaKey ? "Arrow keys pick a direction, Enter steers, Esc cancels." : "Drag toward a direction and release."}`);
    }
    function setHover(k) {
      if (!wheel || wheel.hover === k) return;
      wheel.hover = k;
      svg.querySelectorAll(".wheel-live [data-slot]").forEach((el) => el.classList.toggle("hov", el.dataset.slot === k && !el.classList.contains("empty")));
    }
    function closeWheel(msg) {
      wheel = null; render();
      if (msg) say(msg); else if (foot) { foot.innerHTML = footDefault; foot.classList.remove("live"); }
    }
    function slotAt(e) {
      const n = byId(wheel.node), p = toSvg(e);
      const dx = p.x - pan.x - n.x, dy = p.y - pan.y - n.y;
      if (Math.hypot(dx, dy) < R_IN) return null;
      const deg = (Math.atan2(dy, dx) * 180) / Math.PI;
      const k = SLOTS[((Math.round(deg / 45) + 3) % 8 + 8) % 8];
      return slotsFor(n)[k] ? k : null;
    }
    function commit() {
      const n = byId(wheel.node), k = wheel.hover, s = k && slotsFor(n)[k];
      if (!s) return closeWheel("Cancelled. Nothing was steered.");
      wheel = null; render();
      if (s.kind === "custom") return askCustom(n);
      runStep(n, s);
    }
    function askCustom(n) {
      say(`<form id="custom"><label class="num" for="custom-in" style="color:var(--ink-3)">${n.root ? "0" : n.id}</label><input class="input" id="custom-in" placeholder="Your own order for this waypoint" autocomplete="off"><button class="btn">Steer</button><button class="btn quiet" type="button" id="custom-x">Cancel</button></form>`);
      const form = document.getElementById("custom"), inp = document.getElementById("custom-in");
      inp.focus();
      document.getElementById("custom-x").onclick = () => closeWheel();
      form.onsubmit = (ev) => { ev.preventDefault(); const t = inp.value.trim(); if (!t) return inp.focus(); runStep(n, { kind: "custom", verb: "Custom", title: t }); };
      inp.onkeydown = (ev) => { if (ev.key === "Escape") closeWheel(); };
    }
    function runStep(parent, s) {
      const id = nextId++;
      const sibs = nodes.filter((m) => m.p === parent.id).length;
      const gp = parent.p == null ? { x: parent.x - 1, y: parent.y } : byId(parent.p);
      const base = Math.atan2(parent.y - gp.y, parent.x - gp.x);
      const off = [0, 0.7, -0.7, 1.4, -1.4, 2.1][sibs % 6];
      const x = Math.min(Math.max(parent.x + Math.cos(base + off) * 118, 40), 920);
      const y = Math.min(Math.max(parent.y + Math.sin(base + off) * 118, 50), 640);
      const calls = s.kind === "allow" ? 1 : 3 + (id % 3);
      const title = s.kind === "allow" ? "Ran sqlite3 once" : s.kind === "move" || s.kind === "custom" ? s.title
        : { Deeper: "Deeper: ", "Show code": "Code walk: ", Simpler: "Simpler: ", "Zoom out": "Bigger picture: " }[s.verb] + parent.t;
      const m = { id, p: parent.id, x, y, r: 7, t: `${s.verb === "Crew proposal" ? "Crew move" : s.verb} on ${parent.root ? "Overview" : parent.id}…`,
        lx: x, ly: y + 24, a: "middle", dots: calls, shown: 0, running: true, fresh: true, sub: "Running · no questions mid-step" };
      nodes.push(m); current = id; render();
      const tools = s.kind === "allow" ? ["shell (approved once)"] : ["search", "read_file", "list_dir", "read_file", "shell"];
      let i = 0;
      const tick = () => {
        i++; m.shown = i; m.sub = `Running · call ${i}: ${tools[(i - 1) % tools.length]}`; render();
        say(`<span>${esc(m.t.replace("…", ""))} · call <span class="num">${i}</span> <code style="font-size:12px;color:var(--ink)">${tools[(i - 1) % tools.length]}</code></span>`);
        if (keyTc) keyTc.textContent = String(+keyTc.textContent + 1);
        if (i < calls) return setTimeout(tick, reduce ? 60 : 340);
        setTimeout(() => {
          m.running = false; m.fresh = false; m.t = title.length > 34 ? title.slice(0, 33) + "…" : title;
          m.sub = s.kind === "allow" ? "Ran the blocked command once · approved by you" : null; render();
          if (keyWp) keyWp.textContent = String(nodes.length);
          say(`Step <b class="num">${id}</b> landed. In the app the panel would open it now. Hold any waypoint to steer again.`);
        }, reduce ? 60 : 420);
      };
      setTimeout(tick, reduce ? 60 : 300);
    }

    svg.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (wheel) { capture(e); setHover(slotAt(e)); return; }
      const wp = e.target.closest(".wp"); if (!wp) return;
      e.preventDefault();
      capture(e);
      startHold(+wp.dataset.id, e);
    });
    svg.addEventListener("pointermove", (e) => {
      if (hold && Math.hypot(e.clientX - hold.x, e.clientY - hold.y) > 6) { cancelHold(); say("Moved before 300 ms: that pans the chart in the app. Hold still to steer."); }
      else if (wheel) setHover(slotAt(e));
    });
    svg.addEventListener("pointerup", (e) => {
      if (hold) { cancelHold(); say("Hold a little longer: the wheel opens after 300 ms."); return; }
      if (wheel) { setHover(slotAt(e)); commit(); }
    });
    svg.addEventListener("pointercancel", () => { cancelHold(); if (wheel) closeWheel(); });
    svg.addEventListener("keydown", (e) => {
      const wp = e.target.closest && e.target.closest(".wp");
      if (!wheel && wp && (e.key === " " || e.key === "Enter")) { e.preventDefault(); e.stopPropagation(); openWheel(+wp.dataset.id, true); svg.focus(); }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { if (hold) cancelHold(); if (wheel) closeWheel("Cancelled. Nothing was steered."); return; }
      if (!wheel) {
        if (e.key === " " && document.activeElement === document.body) { e.preventDefault(); openWheel(current, true); }
        return;
      }
      const avail = SLOTS.filter((k) => slotsFor(byId(wheel.node))[k]);
      const i = Math.max(0, avail.indexOf(wheel.hover));
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); setHover(avail[(i + 1) % avail.length]); }
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); setHover(avail[(i - 1 + avail.length) % avail.length]); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); commit(); }
    });
    const fit = document.querySelector('[aria-label="Fit chart to view"]');
    if (fit) fit.addEventListener("click", () => { pan = { x: 0, y: 0 }; render(); });
    if (o.wheelOpen != null) openWheel(o.wheelOpen, true);
  }

  window.AHOY = { chart, slotsFor };
})();
