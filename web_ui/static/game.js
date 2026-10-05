/* 狼人杀 九人局 — 前端交互
 * 基于 SSE 实时流的游戏界面控制
 */

let mode = null, myRole = null, myName = "Player1", waiting = false;
let gs = { players: {}, round: 0, phase: "init" };
let ctx = {};
let currentNight = 0, currentDay = 0;
let allowAbstain = true;  // R1 abstain rule — set from the start-screen toggle

const evtSource = new EventSource("/stream");
evtSource.onmessage = e => handle(JSON.parse(e.data));
evtSource.onerror = () => {};
// On (re)connect: events emitted while disconnected are lost — pull a state
// snapshot so the role cards recover even if the init event was missed.
evtSource.onopen = async () => {
    if (mode !== "god") return;
    try {
        const r = await fetch("/api/state");
        const d = await r.json();
        if (d.running && d.mode === "god" && d.players && d.players.length) {
            gs.players = {};
            d.players.forEach(p => {
                gs.players[p.name] = { role: p.role, camp: p.camp, alive: p.alive };
            });
            renderPlayers();
        }
    } catch (e) {}
};

/* ── Icons (inline SVG snippets) ───────────────────────────────── */
const ICONS = {
    /* Role icons — distinct silhouettes per role:
       wolf head (ears + eye cutouts) / straw-hat farmer / crystal ball /
       pointed witch hat / bolt-action rifle. Monochrome, 24x24. */
    wolf:    '<svg viewBox="0 0 24 24"><path fill-rule="evenodd" d="M4.5 3.2 L9.4 6.6 L14.6 6.6 L19.5 3.2 L20.1 10.6 C20.1 15.9 16.7 19.7 12 21.6 C7.3 19.7 3.9 15.9 3.9 10.6 Z M8.9 10.9 a1.45 1.45 0 1 0 2.9 0 a1.45 1.45 0 1 0 -2.9 0 Z M12.2 10.9 a1.45 1.45 0 1 0 2.9 0 a1.45 1.45 0 1 0 -2.9 0 Z"/></svg>',
    villager:'<svg viewBox="0 0 24 24"><path d="M8.2 6.4 C8.2 3.6 15.8 3.6 15.8 6.4 Z M4 8.6 C4 7.3 7.5 6.5 12 6.5 C16.5 6.5 20 7.3 20 8.6 C20 9.9 16.5 10.7 12 10.7 C7.5 10.7 4 9.9 4 8.6 Z M10 11.6 a2 2 0 1 0 4 0 a2 2 0 1 0 -4 0 Z M5.4 21 C5.4 16.4 8.5 14.1 12 14.1 C15.5 14.1 18.6 16.4 18.6 21 Z"/></svg>',
    seer:    '<svg viewBox="0 0 24 24"><path fill-rule="evenodd" d="M5.6 9.4 a6.4 6.4 0 1 0 12.8 0 a6.4 6.4 0 1 0 -12.8 0 Z M12 5.8 l1.15 2.45 2.45 1.15 -2.45 1.15 -1.15 2.45 -1.15 -2.45 -2.45 -1.15 2.45 -1.15 Z M7 21 L8.5 16.9 C9.6 17.7 10.75 18.1 12 18.1 C13.25 18.1 14.4 17.7 15.5 16.9 L17 21 Z"/></svg>',
    witch:   '<svg viewBox="0 0 24 24"><path d="M11.3 2.2 C12.1 6 13.2 9.4 14.6 12.1 L8 12.1 C9.6 9.3 10.8 5.7 11.3 2.2 Z M3.2 14.7 C3.2 13.2 7.2 12.2 12 12.2 C16.8 12.2 20.8 13.2 20.8 14.7 C20.8 16.2 16.8 17.2 12 17.2 C7.2 17.2 3.2 16.2 3.2 14.7 Z"/></svg>',
    hunter:  '<svg viewBox="0 0 24 24"><path d="M20.8 5.8 L18.2 3.2 L13.3 8.1 L4.2 17.2 L6.8 19.8 L15.9 10.7 L17.5 12.3 L20.1 9.7 L18.5 8.1 Z M7.4 20.4 L5.6 18.6 L7.8 16.4 L9.6 18.2 Z"/></svg>',
    unknown: '<svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.5 3.8 10.7 9 12 5.2-1.3 9-6.5 9-12V5l-9-4zm0 10.8c-1.5 0-2.8-1.2-2.8-2.8S10.5 6.2 12 6.2s2.8 1.2 2.8 2.8-1.3 2.8-2.8 2.8z"/></svg>',
    chat:    '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>',
    sun:     '<svg viewBox="0 0 24 24"><path d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0-5h2v3h-2V2zm0 17h2v3h-2v-3zM4.2 4.2l2.1 2.1-1.4 1.4-2.1-2.1 1.4-1.4zm13.4 13.4 2.1 2.1-1.4 1.4-2.1-2.1 1.4-1.4zM2 12h3v2H2v-2zm17 0h3v2h-3v-2zM4.2 19.8l1.4-1.4 2.1 2.1-1.4 1.4-2.1-2.1zm13.4-13.4 1.4-1.4 2.1 2.1-1.4 1.4-2.1-2.1z"/></svg>',
    moon:    '<svg viewBox="0 0 24 24"><path d="M12.5 3c.5 0 1 .1 1.5.2A8 8 0 0 0 12 19 8 8 0 0 1 12.5 3z"/></svg>',
    vote:    '<svg viewBox="0 0 24 24"><path d="M18 13h-.7l-2-6H8.7l-2 6H6c-1.1 0-2 .9-2 2v5h16v-5c0-1.1-.9-2-2-2zM9.8 9h4.4l1.5 4H8.3l1.5-4zM6 20v-4h12v4H6z"/></svg>',
    skull:   '<svg viewBox="0 0 24 24"><path d="M12 2a6 6 0 0 0-6 6c0 2.2 1.2 4.2 3 5.3V17c0 1.1.9 2 2 2h2c1.1 0 2-.9 2-2v-3.7c1.8-1.1 3-3.1 3-5.3a6 6 0 0 0-6-6zm-3 14h6v1H9v-1zm1-8a1 1 0 1 1 0 2 1 1 0 0 1 0-2zm4 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/></svg>',
    heal:    '<svg viewBox="0 0 24 24"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm-1 15v-4H7v-2h4V7h2v4h4v2h-4v4h-2z"/></svg>',
    poison:  '<svg viewBox="0 0 24 24"><path d="M15 3H9v2h6V3zm3 4H6v2h12V7zm-2 4H8v10c0 1.1.9 2 2 2h4c1.1 0 2-.9 2-2V11z"/></svg>',
    check:   '<svg viewBox="0 0 24 24"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm-1 15l-5-5 1.4-1.4L11 14.2l6.6-6.6L19 9l-8 8z"/></svg>',
    target:  '<svg viewBox="0 0 24 24"><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm0 18c-4.4 0-8-3.6-8-8s3.6-8 8-8 8 3.6 8 8-3.6 8-8 8zm0-14a6 6 0 1 0 0 12 6 6 0 0 0 0-12zm0 9a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/></svg>',
    random:  '<svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zM7 10h2v7H7v-7zm4-3h2v10h-2V7zm4 6h2v4h-2v-4z"/></svg>'
};
// The role system calls it "werewolf"; the icon key is "wolf". Alias so
// ICONS[myRole] / ICONS[iconKey] never fall back to the villager icon.
ICONS.werewolf = ICONS.wolf;

/* ── Landing ───────────────────────────────────────────────────── */
function showRoleSelect() {
    document.getElementById("landing").style.display = "none";
    document.getElementById("roleSelect").style.display = "";
    const grid = document.getElementById("roleGrid");
    grid.innerHTML = "";
    ["werewolf","villager","seer","witch","hunter"].forEach(r => {
        const b = document.createElement("button");
        b.className = "role-btn";
        b.setAttribute("data-role", r);
        b.innerHTML = `<span class="role-icon icon">${ICONS[r]}</span><span>${roleLabel(r)}</span>`;
        b.onclick = () => startPlayer(r);
        grid.appendChild(b);
    });
    const rb = document.createElement("button");
    rb.className = "role-btn random";
    rb.innerHTML = `<span class="icon">${ICONS.random}</span><span>随机身份</span>`;
    rb.onclick = () => startPlayer("random");
    grid.appendChild(rb);
}

function enterGodMode() {
    mode = "god"; resetGame();
    showGame("上帝模式");
    document.getElementById("startBtn").style.display = "";
    document.getElementById("restartBtn").style.display = "none";
}

function abstainOn() {
    const el = document.getElementById("abstainToggle");
    return !el || el.checked;
}

function startGod() {
    document.getElementById("startBtn").style.display = "none";
    document.getElementById("restartBtn").style.display = "";
    allowAbstain = abstainOn();
    fetch("/api/start_god", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ abstain: allowAbstain })
    }).then(r => r.json()).then(d => {
        if (d.error) alert(d.error);
    });
}

function startPlayer(role) {
    mode = "player"; myRole = role; myName = "Player1"; resetGame();
    allowAbstain = abstainOn();
    fetch("/api/start_player", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role, abstain: allowAbstain })
    }).then(r => r.json()).then(d => {
        if (d.error) alert(d.error); else showGame("玩家模式");
    });
}

function goHome() {
    fetch("/api/reset", { method: "POST" }).then(() => location.reload());
}

function restartGame() {
    fetch("/api/reset", { method: "POST" }).then(() => {
        resetGame();
        if (mode === "god") startGod(); else enterGodMode();
    });
}

function resetGame() {
    gs = { players: {}, round: 0, phase: "init" };
    currentNight = 0; currentDay = 0;
    document.getElementById("dayLog").innerHTML = "";
    document.getElementById("nightLog").innerHTML = "";
    document.getElementById("announceBar").innerHTML = "";
    document.getElementById("playerGrid").innerHTML = "";
    document.getElementById("roundLabel").textContent = "准备中";
    document.getElementById("phaseLabel").textContent = "";
    document.getElementById("inputArea").style.display = "none";
    document.getElementById("wolfChannel").style.display = "none";
    document.getElementById("startBtn").style.display = "none";
    document.getElementById("restartBtn").style.display = "none";
    waiting = false;
}

function showGame(badge) {
    document.body.classList.add("dark");
    document.getElementById("landing").style.display = "none";
    document.getElementById("roleSelect").style.display = "none";
    document.getElementById("game").style.display = "";
    document.getElementById("modeBadge").textContent = badge;
    document.getElementById("myId").textContent = mode === "player" ? "你是" + myName : "";
    renderPlaceholders();
    if (mode === "player") setInterval(checkTurn, 250);
}

function renderPlaceholders() {
    const grid = document.getElementById("playerGrid");
    grid.innerHTML = "";
    for (let i = 1; i <= 9; i++) {
        const c = document.createElement("div");
        c.className = "pcard";
        c.id = "pcard-Player" + i;
        c.setAttribute("data-camp", "unknown");
        c.innerHTML = `<div class="role-icon icon">${ICONS.villager}</div><div class="pname">Player${i}</div><div class="pstatus">等待中</div><div class="prole">未知</div>`;
        grid.appendChild(c);
    }
}

/* ── SSE handler ───────────────────────────────────────────────── */
function handle(msg) {
    switch (msg.type) {
        case "init":
            gs.players = {};
            (msg.data.players || []).forEach(p => {
                gs.players[p.name] = { role: p.role, camp: p.camp, alive: true };
            });
            if (msg.data.my_role) {
                myRole = msg.data.my_role;
                document.getElementById("myId").innerHTML =
                    `<span class="icon">${ICONS[myRole] || ICONS.villager}</span>你是 ${esc(msg.data.my_name || "Player1")} ${roleLabel(myRole)}`;
            }
            if (msg.data.my_name) myName = msg.data.my_name;
            if (mode === "player" && myName && !gs.players[myName]) {
                gs.players[myName] = { role: myRole, camp: myRole === "werewolf" ? "werewolf" : "villager", alive: true };
            }
            document.getElementById("playerGrid").innerHTML = "";
            renderPlayers();
            break;

        case "phase_change":
            gs.phase = msg.data.phase;
            gs.round = msg.data.round || gs.round;
            document.getElementById("roundLabel").textContent = "第" + gs.round + "轮";
            document.getElementById("phaseLabel").innerHTML = phLabel(msg.data.phase);
            if (msg.data.phase === "night") {
                currentNight++; _nightHeaderShown = currentNight;
                addLog("nightLog", `<div class="round-hdr">${ICONS.moon} 第${currentNight}夜</div>`);
            }
            if (msg.data.phase === "day" || msg.data.phase === "discussion") {
                currentDay++; _dayHeaderShown = true;
                addLog("dayLog", `<div class="round-hdr">${ICONS.sun} 第${currentDay}天</div>`);
            }
            break;

        case "phase_log": {
            const text = msg.data.text || "";
            const isWolfOnly = text.includes("仅狼人可见") || text.includes("WEREWOLVES ONLY");
            if (mode === "player" && isWolfOnly && myRole !== "werewolf") break;
            const panel = msg.data.panel === "night" ? "nightLog" : "dayLog";
            const label = msg.data.label || "";
            if (label) {
                addLog(panel, `<div class="phase-label">${esc(label)}</div>`);
            }
            addLog(panel, `<div class="moderator-msg">${esc(msg.data.text)}</div>`);
            if (["天亮", "遗言", "投票结果"].includes(label)) {
                addLog("announceBar", `<span class="sys">${esc(label)}: ${esc(msg.data.text).substring(0, 80)}</span>`);
            }
            break;
        }

        case "announce":
            addLog("announceBar", `<span class="sys">${esc(msg.data.text)}</span>`);
            break;

        case "speech":
            addDayHeader();
            addLog("dayLog", `<span class="spk">[${esc(msg.data.player)}]</span> ${esc(msg.data.content)}`);
            break;

        case "vote":
            addDayHeader();
            addLog("dayLog", `<span class="vt">${ICONS.vote} ${esc(msg.data.voter || "?")} → ${esc(msg.data.target)}</span>`);
            break;

        case "death":
            (msg.data.players || []).forEach(n => { if (gs.players[n]) gs.players[n].alive = false; });
            renderPlayers();
            {
                const names = (msg.data.players || []).join(", ");
                if (msg.data.phase === "night") {
                    addLog("nightLog", `<div class="phase-label">夜间死亡 ${esc(names)}</div>`);
                }
            }
            addLog("announceBar", `<span class="sys death">${ICONS.skull} 淘汰: ${esc((msg.data.players || []).join(", "))}</span>`);
            break;

        case "night_action":
            addNightHeader(msg.data.round || currentNight);
            if (mode === "player") {
                if (msg.data.player === myName) {
                    addLog("nightLog", `<span class="spk">[你]</span> ${esc(msg.data.action)}: ${esc(msg.data.target || "无")}`);
                }
                if (myRole === "werewolf" && msg.data.player !== myName && msg.data.strategy === "kill") {
                    addLog("wolfLog", `队友 ${esc(msg.data.player)} 选择击杀: ${esc(msg.data.target || "无")}`);
                }
            } else {
                const act = msg.data.action || "行动";
                const tgt = msg.data.target && msg.data.target !== "none" ? msg.data.target : "";
                const s = msg.data.strategy || "";
                let line;
                if (s === "pass" || act === "无行动" || (!tgt && act !== "查验")) {
                    line = `<span class="spk">[${esc(msg.data.player)}]</span> ${esc(act)}`;
                } else {
                    line = `<span class="spk">[${esc(msg.data.player)}]</span> ${esc(act)}: ${esc(tgt)}`;
                }
                addLog("nightLog", line);
            }
            break;

        case "seer_result":
            if (mode === "god" || (mode === "player" && myRole === "seer")) {
                addLog("nightLog", `<span class="sys">${ICONS.check} ${esc(msg.data.target)} 身份: ${roleLabel(msg.data.role)}</span>`);
            }
            break;

        case "wolf_proposal":
            if (mode === "god" || (mode === "player" && myRole === "werewolf")) {
                addLog("wolfLog", `队友 ${esc(msg.data.player)} 选择击杀: ${esc(msg.data.target)}`);
            }
            break;

        case "game_over":
            document.getElementById("phaseLabel").textContent = "游戏结束";
            addLog("announceBar", `<span class="sys">${esc(msg.data.result || "")}</span>`);
            break;

        case "reveal":
            (msg.data.players || []).forEach(p => {
                if (gs.players[p.name]) gs.players[p.name].role = p.role;
            });
            renderPlayers();
            break;

        case "done":
            document.getElementById("phaseLabel").textContent = "结束";
            document.getElementById("inputArea").style.display = "none";
            break;

        case "_wake":
            break;

        case "reset":
            document.getElementById("phaseLabel").textContent = "已重置";
            document.getElementById("inputArea").style.display = "none";
            break;
    }
}

let _dayHeaderShown = false, _nightHeaderShown = 0;
function addDayHeader() {
    if (!_dayHeaderShown) {
        addLog("dayLog", `<div class="round-hdr">${ICONS.sun} 第${currentDay || 1}天</div>`);
        _dayHeaderShown = true;
    }
}
function addNightHeader(r) {
    if (_nightHeaderShown !== r) {
        addLog("nightLog", `<div class="round-hdr">${ICONS.moon} 第${r || currentNight}夜</div>`);
        _nightHeaderShown = r;
    }
}

/* ── Player turn input ─────────────────────────────────────────── */
async function checkTurn() {
    try {
        const r = await fetch("/api/player_state");
        const d = await r.json();
        if (d.waiting && !waiting) { waiting = true; ctx = d.info || {}; showInput(); }
        else if (!d.waiting && waiting) { waiting = false; hideInput(); }
    } catch(e) {}
}

function showInput() {
    const area = document.getElementById("inputArea");
    area.style.display = "";
    area.scrollIntoView({ behavior: "smooth" });
    document.getElementById("turnIndicator").style.display = "";
    document.getElementById("actionHint").innerHTML = buildHint();
    const ph = ctx.phase || gs.phase;
    const dead = gs.players[myName] && !gs.players[myName].alive;
    document.getElementById("speechInput").style.display = (ph === "discussion" || dead) ? "" : "none";
    document.getElementById("voteInput").style.display = ph === "voting" ? "" : "none";
    document.getElementById("actionInput").style.display = ph === "night" ? "" : "none";
    if (ph === "voting") buildVoteBtns();
    if (ph === "night") buildActionBtns();
    if (myRole === "werewolf" && ph === "night") document.getElementById("wolfChannel").style.display = "";
}

function hideInput() {
    document.getElementById("inputArea").style.display = "none";
    document.getElementById("turnIndicator").style.display = "none";
    waiting = false;
}

function buildHint() {
    const r = ctx.role || myRole, ph = gs.phase;
    if (ph === "discussion") return "轮到你发言了，请清晰表达你的推理与怀疑。";
    if (ph === "voting") {
        const rnd = (ctx && ctx.round) || gs.round || 0;
        return (allowAbstain && rnd <= 1)
            ? "选择你要投票淘汰的玩家。首轮无查验信息，也可以选择弃票。"
            : "选择你要投票淘汰的玩家。";
    }
    if (r === "werewolf") return `狼人行动：选择击杀目标，队友 ${(ctx.teammates || []).join(", ") || "无"}`;
    if (r === "witch") {
        const h = ctx.healing_used ? "已用" : "可用";
        const p = ctx.poison_used ? "已用" : "可用";
        const stage = ctx.witch_stage || "";
        if (stage === "resurrect") return `女巫 | 解药:${h} | 今晚 ${ctx.night_killed || "?"} 被杀，是否使用解药？`;
        if (stage === "poison") return `女巫 | 毒药:${p} | 选择一名玩家使用毒药，或跳过。`;
        return `女巫 | 解药:${h} 毒药:${p} | 今晚 ${ctx.night_killed || "?"} 被杀`;
    }
    if (r === "seer") {
        const s = Object.entries(ctx.seer_checks || {}).map(([n, r2]) => `${esc(n)}=${roleLabel(r2)}`).join(", ") || "无";
        return `预言家 | 已查验: ${s}`;
    }
    if (r === "hunter") return "你被淘汰，可开枪带走一名玩家，或选择放弃。";
    return "";
}

function buildVoteBtns() {
    const div = document.getElementById("voteButtons");
    div.innerHTML = "";
    Object.entries(gs.players).forEach(([n, i]) => {
        if (i.alive && n !== myName) {
            const b = document.createElement("button");
            b.className = "btn btn-primary";
            b.innerHTML = `${ICONS.vote}<span>${esc(n)}</span>`;
            b.onclick = () => send(n);
            div.appendChild(b);
        }
    });
    // R1 abstain option (rule toggle from the start screen)
    const rnd = (ctx && ctx.round) || gs.round || 0;
    if (allowAbstain && rnd <= 1) {
        const ab = document.createElement("button");
        ab.className = "btn btn-secondary";
        ab.textContent = "弃票（本轮无人出局）";
        ab.onclick = () => send("abstain");
        div.appendChild(ab);
    }
}

function buildActionBtns() {
    const div = document.getElementById("actionButtons");
    div.innerHTML = "";
    const alive = Object.entries(gs.players).filter(([n, i]) => i.alive && n !== myName).map(([n]) => n);
    const r = ctx.role || myRole;

    if (r === "werewolf") {
        const mates = new Set(ctx.teammates || []);
        alive.filter(n => !mates.has(n)).forEach(n => {
            const b = document.createElement("button");
            b.className = "btn abtn kill";
            b.innerHTML = `${ICONS.wolf}<span>击杀 ${esc(n)}</span>`;
            b.onclick = () => send(n);
            div.appendChild(b);
        });
    }
    else if (r === "seer") {
        alive.forEach(n => {
            const b = document.createElement("button");
            b.className = "btn abtn check";
            b.innerHTML = `${ICONS.check}<span>查验 ${esc(n)}</span>`;
            b.onclick = () => send(n);
            div.appendChild(b);
        });
    }
    else if (r === "witch") {
        const stage = ctx.witch_stage || "";
        if (stage === "resurrect") {
            if (!ctx.healing_used && ctx.night_killed && ctx.night_killed !== myName) {
                const hb = document.createElement("button");
                hb.className = "btn abtn heal";
                hb.innerHTML = `${ICONS.heal}<span>救 ${esc(ctx.night_killed)}</span>`;
                hb.onclick = () => send("resurrect");
                div.appendChild(hb);
            }
            const nb = document.createElement("button");
            nb.className = "btn btn-secondary";
            nb.textContent = "不救";
            nb.onclick = () => send("none");
            div.appendChild(nb);
        } else if (stage === "poison") {
            alive.forEach(n => {
                const b = document.createElement("button");
                b.className = "btn abtn poison";
                b.innerHTML = `${ICONS.poison}<span>毒 ${esc(n)}</span>`;
                b.onclick = () => send("poison:" + n);
                div.appendChild(b);
            });
            const nb = document.createElement("button");
            nb.className = "btn btn-secondary";
            nb.textContent = "不用毒";
            nb.onclick = () => send("none");
            div.appendChild(nb);
        } else {
            if (!ctx.healing_used && ctx.night_killed && ctx.night_killed !== myName) {
                const hb = document.createElement("button");
                hb.className = "btn abtn heal";
                hb.innerHTML = `${ICONS.heal}<span>救 ${esc(ctx.night_killed)}</span>`;
                hb.onclick = () => send("resurrect");
                div.appendChild(hb);
            }
            alive.forEach(n => {
                const b = document.createElement("button");
                b.className = "btn abtn poison";
                b.innerHTML = `${ICONS.poison}<span>毒 ${esc(n)}</span>`;
                b.onclick = () => send("poison:" + n);
                div.appendChild(b);
            });
            const nb = document.createElement("button");
            nb.className = "btn btn-secondary";
            nb.textContent = "跳过";
            nb.onclick = () => send("none");
            div.appendChild(nb);
        }
    }
    else if (r === "hunter") {
        alive.forEach(n => {
            const b = document.createElement("button");
            b.className = "btn abtn shoot";
            b.innerHTML = `${ICONS.target}<span>带走 ${esc(n)}</span>`;
            b.onclick = () => send(n);
            div.appendChild(b);
        });
        const nb = document.createElement("button");
        nb.className = "btn btn-secondary";
        nb.textContent = "放弃";
        nb.onclick = () => send("none");
        div.appendChild(nb);
    }
}

function submitSpeech() {
    const box = document.getElementById("speechBox");
    const t = box.value.trim();
    if (t) { send(t); box.value = ""; }
}

document.addEventListener("keydown", e => {
    if (e.key === "Enter" && document.activeElement.id === "speechBox") submitSpeech();
});

async function send(text) {
    try {
        await fetch("/api/player_input", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text })
        });
    } catch(e) { console.error("send error", e); }
    hideInput();
    waiting = false;
    await checkTurnNow();
    if (!waiting) setTimeout(checkTurnNow, 200);
}

async function checkTurnNow() {
    try {
        const r = await fetch("/api/player_state");
        const d = await r.json();
        if (d.waiting && !waiting) { waiting = true; ctx = d.info || {}; showInput(); }
        else if (!d.waiting && waiting) { waiting = false; hideInput(); }
    } catch(e) {}
}

/* ── Rendering ─────────────────────────────────────────────────── */
function renderPlayers() {
    const grid = document.getElementById("playerGrid");
    Object.entries(gs.players).forEach(([n, i]) => {
        let c = document.getElementById("pcard-" + n);
        if (!c) {
            c = document.createElement("div");
            c.id = "pcard-" + n;
            grid.appendChild(c);
        }
        c.className = "pcard" + (i.alive ? "" : " dead");
        c.setAttribute("data-camp", i.camp || "unknown");
        const showRole = mode === "god" || (mode === "player" && (n === myName || !i.alive));
        const you = (mode === "player" && n === myName) ? " (你)" : "";
        const roleKnown = showRole && i.role !== "???" && i.role !== "未知";
        const roleName = roleKnown ? roleLabel(i.role) : "未知";
        const iconKey = roleKnown ? i.role : "unknown";
        c.innerHTML = `<div class="role-icon icon">${ICONS[iconKey] || ICONS.unknown}</div><div class="pname">${esc(n)}${you}</div><div class="pstatus">${i.alive ? "存活" : "死亡"}</div><div class="prole">${roleName}</div>`;
    });
    for (let i = 1; i <= 9; i++) {
        const card = document.getElementById("pcard-Player" + i);
        if (card && !gs.players["Player" + i]) card.remove();
    }
}

function addLog(id, html) {
    const log = document.getElementById(id);
    if (!log) return;
    const d = document.createElement("div");
    d.className = "log-entry";
    d.innerHTML = html;
    log.appendChild(d);
    // Panels grow with content now — scroll the page to the newest entry
    d.scrollIntoView({ block: "nearest" });
}

function phLabel(p) {
    const map = {
        night:       `${ICONS.moon} 夜间`,
        day:         `${ICONS.sun} 白天`,
        discussion:  `${ICONS.chat} 发言`,
        voting:      `${ICONS.vote} 投票`,
        game_over:   "结束"
    };
    return map[p] || p;
}

function roleLabel(r) {
    const map = {
        werewolf: "狼人",
        seer:     "预言家",
        witch:    "女巫",
        hunter:   "猎人",
        villager: "村民"
    };
    return map[r] || r;
}

function esc(t) {
    const d = document.createElement("div");
    d.textContent = t;
    return d.innerHTML;
}
