let mode = null, myRole = null, myName = "Player1", waiting = false;
let gs = { players: {}, round: 0, phase: "init" };
let ctx = {};
let currentNight = 0, currentDay = 0;

const evtSource = new EventSource("/stream");
evtSource.onmessage = e => handle(JSON.parse(e.data));
evtSource.onerror = () => {};

// Landing
function showRoleSelect() {
    document.getElementById("landing").style.display = "none";
    document.getElementById("roleSelect").style.display = "";
    const grid = document.getElementById("roleGrid");
    grid.innerHTML = "";
    ["werewolf","villager","seer","witch","hunter"].forEach(r => {
        const b = document.createElement("button"); b.className = "role-btn";
        b.textContent = roleLabel(r) + " " + r; b.onclick = () => startPlayer(r);
        grid.appendChild(b);
    });
    const rb = document.createElement("button"); rb.className = "role-btn random";
    rb.textContent = "随机"; rb.onclick = () => startPlayer("random"); grid.appendChild(rb);
}

function enterGodMode() {
    mode = "god"; resetGame();
    showGame("上帝模式");
    // Show "开始游戏", hide "开始新游戏"
    document.getElementById("startBtn").style.display = "";
    document.getElementById("restartBtn").style.display = "none";
}
function startGod() {
    // Hide "开始游戏", show "开始新游戏"
    document.getElementById("startBtn").style.display = "none";
    document.getElementById("restartBtn").style.display = "";
    fetch("/api/start_god", { method: "POST" }).then(r => r.json()).then(d => {
        if (d.error) alert(d.error);
    });
}
function startPlayer(role) {
    mode = "player"; myRole = role; myName = "Player1"; resetGame();
    fetch("/api/start_player", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role }) })
        .then(r => r.json()).then(d => {
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
    document.getElementById("landing").style.display = "none";
    document.getElementById("roleSelect").style.display = "none";
    document.getElementById("game").style.display = "";
    document.getElementById("modeBadge").textContent = badge;
    document.getElementById("myId").textContent = mode === "player" ? "你是 " + myName : "";
    // Render 9 placeholder cards immediately
    renderPlaceholders();
    if (mode === "player") setInterval(checkTurn, 250);
}
function renderPlaceholders() {
    const grid = document.getElementById("playerGrid");
    grid.innerHTML = "";
    for (let i = 1; i <= 9; i++) {
        const c = document.createElement("div");
        c.className = "pcard"; c.id = "pcard-Player" + i;
        c.innerHTML = `<div class="pname">Player${i}</div><div class="pstatus">等待中</div><div class="prole">???</div>`;
        grid.appendChild(c);
    }
}

// SSE handler
function handle(msg) {
    switch (msg.type) {
        case "init":
            gs.players = {}; (msg.data.players||[]).forEach(p => { gs.players[p.name] = { role: p.role, camp: p.camp, alive: true }; });
            if (msg.data.my_role) {
                myRole = msg.data.my_role;
                document.getElementById("myId").textContent = "你是 " + (msg.data.my_name||"Player1") + " · " + roleLabel(myRole);
            }
            if (msg.data.my_name) myName = msg.data.my_name;
            // Player mode: add the human player to gs.players since they're not in the AI list
            if (mode === "player" && myName && !gs.players[myName]) {
                gs.players[myName] = { role: myRole, camp: myRole === "werewolf" ? "werewolf" : "villager", alive: true };
            }
            // Force full re-render: clear grid first, then rebuild all cards
            document.getElementById("playerGrid").innerHTML = "";
            renderPlayers();
            break;
        case "phase_change":
            gs.phase = msg.data.phase; gs.round = msg.data.round || gs.round;
            document.getElementById("roundLabel").textContent = "第" + gs.round + "轮";
            document.getElementById("phaseLabel").textContent = phLabel(msg.data.phase);
            if (msg.data.phase === "night") { currentNight++; _nightHeaderShown = currentNight;
                addLog("nightLog", `<div class="round-hdr">🌙 第${currentNight}夜</div>`); }
            if (msg.data.phase === "day" || msg.data.phase === "discussion") { currentDay++; _dayHeaderShown = true;
                addLog("dayLog", `<div class="round-hdr">☀️ 第${currentDay}天</div>`); }
            break;
        case "phase_log":
            // In player mode, hide wolf-only discussions from non-wolves
            {
                const text = msg.data.text || "";
                const isWolfOnly = text.includes("仅狼人可见") || text.includes("WEREWOLVES ONLY");
                if (mode === "player" && isWolfOnly && myRole !== "werewolf") break;
                const panel = msg.data.panel === "night" ? "nightLog" : "dayLog";
                const label = msg.data.label || "";
                if (label) {
                    addLog(panel, `<div class="phase-label">【${label}】</div>`);
                }
                addLog(panel, `<div class="moderator-msg">📢 ${esc(msg.data.text)}</div>`);
                if (label === "天亮" || label === "遗言" || label === "投票结果") {
                    addLog("announceBar", `<span class="sys">${label}: ${esc(msg.data.text).substring(0,80)}</span>`);
                }
            }
            break;
        case "announce":
            addLog("announceBar", `<span class="sys">${esc(msg.data.text)}</span>`); break;
        case "speech":
            addDayHeader();
            addLog("dayLog", `<span class="spk">[${msg.data.player}]</span> ${esc(msg.data.content)}`); break;
        case "vote":
            addDayHeader();
            addLog("dayLog", `<span class="vt">🗳 ${msg.data.voter||"?"} → ${msg.data.target}</span>`); break;
        case "death":
            (msg.data.players||[]).forEach(n => { if (gs.players[n]) gs.players[n].alive = false; });
            renderPlayers();
            {
                const names = (msg.data.players||[]).join(", ");
                if (msg.data.phase === "night") {
                    addLog("nightLog", `<div class="phase-label">【夜间死亡】${names}</div>`);
                }
                // Day death only shows in announce bar, not in dayLog
            }
            addLog("announceBar", `<span class="sys death">💀 淘汰: ${(msg.data.players||[]).join(", ")}</span>`);
            break;
        case "night_action":
            addNightHeader(msg.data.round || currentNight);
            // Player mode: only show own actions + public info (seer check, deaths)
            if (mode === "player") {
                if (msg.data.player === myName) {
                    addLog("nightLog", `[你] ${msg.data.action}: ${msg.data.target||"无"}`);
                }
                // Wolf teammates see each other's proposals
                if (myRole === "werewolf" && msg.data.player !== myName && msg.data.strategy === "kill") {
                    addLog("wolfLog", `队友 ${msg.data.player} 选择击杀: ${msg.data.target||"无"}`);
                }
            } else {
                // God mode: show all night actions
                const act = msg.data.action || "行动";
                const tgt = msg.data.target && msg.data.target !== "none" ? msg.data.target : "";
                const s = msg.data.strategy || "";
                let line;
                if (s === "pass" || act === "无行动" || (!tgt && act !== "查验")) {
                    line = `[${msg.data.player}] ${act}`;
                } else {
                    line = `[${msg.data.player}] ${act}: ${tgt}`;
                }
                addLog("nightLog", line);
            }
            break;
        case "seer_result":
            // Player mode: only show to seer
            if (mode === "god" || (mode === "player" && myRole === "seer"))
                addLog("nightLog", `<span class="sys">🔮 ${msg.data.target} 身份: ${roleLabel(msg.data.role)}</span>`);
            break;
        case "wolf_proposal":
            // Player mode: only visible to werewolves
            if (mode === "god" || (mode === "player" && myRole === "werewolf"))
                addLog("wolfLog", `队友 ${msg.data.player} 选择击杀: ${msg.data.target}`);
            break;
        case "game_over":
            document.getElementById("phaseLabel").textContent = "游戏结束";
            addLog("announceBar", `<span class="sys">${esc(msg.data.result||"")}</span>`); break;
        case "reveal":
            (msg.data.players||[]).forEach(p => { if (gs.players[p.name]) gs.players[p.name].role = p.role; });
            renderPlayers(); break;
        case "done":
            document.getElementById("phaseLabel").textContent = "结束";
            document.getElementById("inputArea").style.display = "none"; break;
        case "_wake":
            // SSE internal: queue switch sentinel, ignore
            break;
        case "reset":
            // Backend session was reset
            document.getElementById("phaseLabel").textContent = "已重置";
            document.getElementById("inputArea").style.display = "none"; break;
    }
}

let _dayHeaderShown = false, _nightHeaderShown = 0;
function addDayHeader() {
    if (!_dayHeaderShown) {
        addLog("dayLog", `<div class="round-hdr">☀️ 第${currentDay||1}天</div>`);
        _dayHeaderShown = true;
    }
}
function addNightHeader(r) {
    if (_nightHeaderShown !== r) {
        addLog("nightLog", `<div class="round-hdr">🌙 第${r||currentNight}夜</div>`);
        _nightHeaderShown = r;
    }
}

// Turn detection (player mode)
async function checkTurn() {
    try {
        const r = await fetch("/api/player_state");
        const d = await r.json();
        if (d.waiting && !waiting) { waiting = true; ctx = d.info||{}; showInput(); }
        else if (!d.waiting && waiting) { waiting = false; hideInput(); }
    } catch(e) {}
}
function showInput() {
    const area = document.getElementById("inputArea"); area.style.display = "";
    area.scrollIntoView({ behavior: "smooth" });
    document.getElementById("turnIndicator").style.display = "";
    document.getElementById("actionHint").innerHTML = buildHint();
    // Use server's phase (ctx.phase) — more reliable than SSE gs.phase
    const ph = ctx.phase || gs.phase;
    const dead = gs.players[myName] && !gs.players[myName].alive;
    document.getElementById("speechInput").style.display = (ph === "discussion" || dead) ? "" : "none";
    document.getElementById("voteInput").style.display = ph === "voting" ? "" : "none";
    document.getElementById("actionInput").style.display = ph === "night" ? "" : "none";
    if (ph === "voting") buildVoteBtns();
    if (ph === "night") buildActionBtns();
    if (myRole === "werewolf" && ph === "night") document.getElementById("wolfChannel").style.display = "";
}
function hideInput() { document.getElementById("inputArea").style.display = "none"; document.getElementById("turnIndicator").style.display = "none"; waiting = false; }

function buildHint() {
    const r = ctx.role||myRole, ph = gs.phase;
    if (ph === "discussion") return "轮到你发言了";
    if (ph === "voting") return "选择你要投票淘汰的玩家";
    if (r === "werewolf") return `选择击杀目标 | 队友: ${(ctx.teammates||[]).join(", ")||"无"}`;
    if (r === "witch") {
        const h=ctx.healing_used?"已用":"可用", p=ctx.poison_used?"已用":"可用";
        const stage = ctx.witch_stage || "";
        if (stage === "resurrect") return `女巫 | 解药:${h} | 今晚 ${ctx.night_killed||"?"} 被杀。要救吗？`;
        if (stage === "poison") return `女巫 | 毒药:${p} | 要毒谁？`;
        return `女巫 | 解药:${h} 毒药:${p} | 今晚 ${ctx.night_killed||"?"} 被杀`;
    }
    if (r === "seer") { const s=Object.entries(ctx.seer_checks||{}).map(([n,r])=>`${n}=${roleLabel(r)}`).join(", ")||"无"; return `预言家 | 已查: ${s}`; }
    if (r === "hunter") return "你被淘汰，可带走一人或放弃"; return "";
}
function buildVoteBtns() {
    const div = document.getElementById("voteButtons"); div.innerHTML = "";
    Object.entries(gs.players).forEach(([n,i]) => { if(i.alive&&n!==myName){const b=document.createElement("button");b.className="vbtn";b.textContent=n;b.onclick=()=>send(n);div.appendChild(b);}});
}
function buildActionBtns() {
    const div = document.getElementById("actionButtons"); div.innerHTML = "";
    const alive = Object.entries(gs.players).filter(([n,i])=>i.alive&&n!==myName).map(([n])=>n);
    const r = ctx.role||myRole;
    if (r==="werewolf") {
        const mates=new Set(ctx.teammates||[]);
        alive.filter(n=>!mates.has(n)).forEach(n=>{
            const b=document.createElement("button");b.className="abtn kill";
            b.textContent="🔪 "+n;b.onclick=()=>send(n);div.appendChild(b);
        });
    }
    else if (r==="seer") {
        alive.forEach(n=>{const b=document.createElement("button");b.className="abtn";
            b.textContent="🔍 "+n;b.onclick=()=>send(n);div.appendChild(b);});
    }
    else if (r==="witch") {
        const stage = ctx.witch_stage || "";
        if (stage === "resurrect") {
            // Step 1: Heal or skip
            if (!ctx.healing_used && ctx.night_killed && ctx.night_killed !== myName) {
                const hb = document.createElement("button"); hb.className = "abtn heal";
                hb.textContent = "💚 救 " + ctx.night_killed;
                hb.onclick = () => send("resurrect"); div.appendChild(hb);
            }
            const nb = document.createElement("button"); nb.className = "abtn";
            nb.textContent = "不救"; nb.onclick = () => send("none"); div.appendChild(nb);
        } else if (stage === "poison") {
            // Step 2: Poison someone or skip
            alive.forEach(n => {
                const b = document.createElement("button"); b.className = "abtn poison";
                b.textContent = "☠️ 毒 " + n;
                b.onclick = () => send("poison:" + n); div.appendChild(b);
            });
            const nb = document.createElement("button"); nb.className = "abtn";
            nb.textContent = "不用毒"; nb.onclick = () => send("none"); div.appendChild(nb);
        } else {
            // Fallback: show both (shouldn't happen normally)
            if (!ctx.healing_used && ctx.night_killed && ctx.night_killed !== myName) {
                const hb = document.createElement("button"); hb.className = "abtn heal";
                hb.textContent = "💚 救 " + ctx.night_killed;
                hb.onclick = () => send("resurrect"); div.appendChild(hb);
            }
            alive.forEach(n => {
                const b = document.createElement("button"); b.className = "abtn poison";
                b.textContent = "☠️ 毒 " + n;
                b.onclick = () => send("poison:" + n); div.appendChild(b);
            });
            const nb = document.createElement("button"); nb.className = "abtn";
            nb.textContent = "跳过"; nb.onclick = () => send("none"); div.appendChild(nb);
        }
    }
    else if (r==="hunter") {
        alive.forEach(n=>{const b=document.createElement("button");b.className="abtn";
            b.textContent="🎯 "+n;b.onclick=()=>send(n);div.appendChild(b);});
        const nb=document.createElement("button");nb.className="abtn";
        nb.textContent="放弃";nb.onclick=()=>send("none");div.appendChild(nb);
    }
}
function submitSpeech() { const box=document.getElementById("speechBox"); const t=box.value.trim(); if(t){send(t);box.value="";} }
document.addEventListener("keydown", e => { if(e.key==="Enter"&&document.activeElement.id==="speechBox") submitSpeech(); });
async function send(text) {
    try {
        await fetch("/api/player_input",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
    } catch(e) { console.error("send error", e); }
    hideInput();
    waiting = false;
    // Poll immediately, then again quickly if still waiting
    await checkTurnNow();
    if (!waiting) setTimeout(checkTurnNow, 200);
}
async function checkTurnNow() {
    try {
        const r = await fetch("/api/player_state");
        const d = await r.json();
        if (d.waiting && !waiting) { waiting = true; ctx = d.info||{}; showInput(); }
        else if (!d.waiting && waiting) { waiting = false; hideInput(); }
    } catch(e) {}
}

function renderPlayers() {
    const grid = document.getElementById("playerGrid");
    Object.entries(gs.players).forEach(([n,i]) => {
        let c = document.getElementById("pcard-" + n);
        if (!c) {
            c = document.createElement("div"); c.id = "pcard-" + n;
            grid.appendChild(c);
        }
        c.className = "pcard" + (i.alive ? "" : " dead");
        const showRole = mode === "god" || (mode === "player" && (n === myName || !i.alive));
        const you = (mode === "player" && n === myName) ? " (你)" : "";
        c.innerHTML = `<div class="pname">${n}${you}</div><div class="pstatus">${i.alive?"存活":"死亡"}</div><div class="prole">${showRole&&i.role!=="???"?roleLabel(i.role):"???"}</div>`;
    });
    // Remove stale cards for players not in gs.players
    for (let i = 1; i <= 9; i++) {
        const card = document.getElementById("pcard-Player" + i);
        if (card && !gs.players["Player" + i]) {
            card.remove();
        }
    }
}
function addLog(id, html) {
    const log = document.getElementById(id); if (!log) return;
    const d = document.createElement("div"); d.className = "log-entry"; d.innerHTML = html;
    log.appendChild(d); log.scrollTop = log.scrollHeight;
}
function phLabel(p) { return {night:"🌙",day:"☀️",discussion:"💬",voting:"🗳️",game_over:"🏁"}[p]||p; }
function roleLabel(r) { return {werewolf:"🐺狼",seer:"🔮预",witch:"🧙巫",hunter:"🏹猎",villager:"👨‍🌾民"}[r]||r; }
function esc(t) { const d=document.createElement("div"); d.textContent=t; return d.innerHTML; }
