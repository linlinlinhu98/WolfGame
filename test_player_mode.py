# -*- coding: utf-8 -*-
"""Play one full game as EACH role via the real web endpoints.

For role in [werewolf, seer, witch, hunter, villager]:
  - POST /api/start_player {role}
  - poll /api/player_state, submit legal inputs (speech/vote/night action)
  - inject a subscriber queue to record exactly what a browser would receive
  - assert no private-info leaks for that role
  - after the game, check the human's speech/vote landed in game_data
"""
import json
import os
import queue
import sys
import threading
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_ui import server as srv

PORT = 5057
BASE = f"http://127.0.0.1:{PORT}"
REPORT = []


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    REPORT.append(line)


def _req(path, data=None, method=None):
    body = json.dumps(data or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method or ("POST" if body else "GET"),
        headers={"Content-Type": "application/json"} if body else {})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def start_server():
    t = threading.Thread(
        target=lambda: srv.app.run(host="127.0.0.1", port=PORT,
                                   debug=False, use_reloader=False),
        daemon=True)
    t.start()
    for _ in range(50):
        try:
            _req("/api/status")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def pick_others(info, exclude=()):
    alive = [p for p in info["alive_players"] if p != "Player1" and p not in exclude]
    return alive


def decide(info):
    """Legal input for the current waiting state. Also exercises the exact
    structured strings the frontend sends."""
    role, phase, rnd = info["role"], info["phase"], info["round"]
    if phase == "discussion":
        return "我是好人，先听听大家的发言再做判断。"
    if phase == "voting":
        if rnd <= 1:
            return "abstain"          # exercise the R1 abstain path
        others = pick_others(info)
        return others[0] if others else "abstain"
    if phase == "night":
        if role == "werewolf":
            mates = set(info.get("teammates", []))
            others = pick_others(info, mates)
            return others[0] if others else "none"
        if role == "seer":
            checked = set(info.get("seer_checks", {}))
            others = pick_others(info, checked)
            return others[0] if others else "none"
        if role == "witch":
            can_heal = (not info["healing_used"] and info["night_killed"]
                        and info["night_killed"] in info["alive_players"])
            can_poison = not info["poison_used"]
            if can_heal and can_poison and rnd > 1:
                # combined string — exercises the resurrect+poison parser
                return "resurrect+poison:" + pick_others(info)[0]
            if can_heal:
                return "resurrect"
            if can_poison and rnd > 1:
                return "poison:" + pick_others(info)[0]
            return "none"
        if role == "hunter":
            return "none"              # pass on the shot; still a legal parse
    return "none"


ROLE_ALLOWED_LOG_MARKERS = {
    "werewolf": {"仅狼人可见", "WEREWOLVES ONLY"},
    "witch": {"仅女巫可见"},
    "seer": {"仅预言家可见"},
    "hunter": {"仅猎人可见"},
    "villager": set(),
}


def check_leaks(role, events):
    """The browser stream must not contain anything this role can't see."""
    problems = []
    allowed = ROLE_ALLOWED_LOG_MARKERS[role]
    for ev in events:
        t, d = ev.get("type"), ev.get("data") or {}
        if t == "wolf_proposal" and role != "werewolf":
            problems.append(f"leak: wolf_proposal -> {d}")
        if t == "seer_result" and role != "seer":
            problems.append(f"leak: seer_result -> {d}")
        if t == "night_action" and d.get("player") != "Player1":
            if not (role == "werewolf" and d.get("strategy") == "kill"):
                problems.append(f"leak: night_action -> {d}")
        if t == "phase_log":
            text = str(d.get("text", ""))
            for marker, owner in [("仅狼人可见", "werewolf"), ("WEREWOLVES ONLY", "werewolf"),
                                  ("仅女巫可见", "witch"), ("仅预言家可见", "seer"),
                                  ("仅猎人可见", "hunter")]:
                if marker in text and role != owner:
                    problems.append(f"leak: log「{marker}」to {role}")
    return problems


def play_as(role):
    _log(f"===== 角色: {role} =====")
    _req("/api/reset", method="POST")
    sq = queue.Queue()
    srv._register(sq)
    _req("/api/start_player", {"role": role, "abstain": True}, method="POST")

    events, submits, deadline = [], [], time.time() + 1500
    spoke = voted = acted = 0
    while time.time() < deadline:
        # drain the browser-view queue (leak check + event contract)
        while True:
            try:
                events.append(sq.get_nowait())
            except queue.Empty:
                break
        try:
            st = _req("/api/player_state")
        except Exception as e:
            _log(f"  player_state error: {e}")
            break
        if not _req("/api/status")["running"]:
            break
        if st.get("waiting"):
            info = st.get("info") or {}
            if not info.get("phase"):     # game thread still starting up
                time.sleep(0.3)
                continue
            text = decide(info)
            submits.append((info.get("phase"), info.get("round"), text))
            if info.get("phase") == "discussion":
                spoke += 1
            elif info.get("phase") == "voting":
                voted += 1
            elif info.get("phase") == "night":
                acted += 1
            _req("/api/player_input", {"text": text}, method="POST")
        else:
            time.sleep(0.3)
    srv._unregister(sq)

    problems = check_leaks(role, events)
    ev_types = {}
    for ev in events:
        ev_types[ev["type"]] = ev_types.get(ev["type"], 0) + 1
    _log(f"  events: {json.dumps(ev_types, ensure_ascii=False)}")
    _log(f"  human submits: speech={spoke} vote={voted} night_action={acted}")
    for p in problems[:5]:
        _log(f"  !! {p}")

    # did the human's contributions reach the game record?
    ok_record = "?"
    try:
        files = [f for f in os.listdir("game_data") if f.endswith(".json")]
        newest = max(files, key=lambda f: os.path.getmtime(os.path.join("game_data", f)))
        d = json.load(open(os.path.join("game_data", newest), encoding="utf-8"))
        blob = json.dumps(d, ensure_ascii=False)
        ok_record = ("speech" if "我是好人，先听听大家的发言" in blob
                     else "no-speech-in-record")
        win = str(d.get("winner", ""))
        _log(f"  game winner={win} rounds={len(d.get('rounds', []))} "
             f"human-speech-in-record={ok_record}")
    except Exception as e:
        _log(f"  game_data check failed: {e}")

    status = "PASS" if not problems else "FAIL"
    _log(f"  => {role}: {status}  (winner={win if 'win' in dir() else '?'})")
    return {"role": role, "status": status, "problems": problems,
            "events": ev_types, "speech": spoke, "vote": voted, "action": acted,
            "winner": win if 'win' in dir() else "?"}


def main():
    start_server()
    results = []
    for role in ["werewolf", "seer", "witch", "hunter", "villager"]:
        try:
            results.append(play_as(role))
        except Exception as e:
            import traceback
            _log(f"  EXCEPTION in {role}: {e}")
            REPORT.append(traceback.format_exc())
    _log("===== 汇总 =====")
    for r in results:
        _log(f"  {r['role']:9} {r['status']}  speech={r['speech']} vote={r['vote']} "
             f"action={r['action']} winner={r['winner']}")
    with open("playtest_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    _log("report -> playtest_report.txt")


if __name__ == "__main__":
    main()
