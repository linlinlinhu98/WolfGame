# -*- coding: utf-8 -*-
"""Werewolf Web Platform — God Mode + Player Mode.

Start: python server.py → http://localhost:5000
"""
import json, queue, threading, sys, os, random
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, Response, request, jsonify

app = Flask(__name__)

# Each game gets its own queue; SSE only reads from the newest one.
# Old threads write to their captured queue (nobody reads → silent discard).
_current_queue: queue.Queue = queue.Queue()

# Current game session
_session = {"running": False, "mode": None, "human": None, "agents": []}
_game_thread = None   # Reference to current game thread
_game_stop_event = threading.Event()  # Signal old game threads to stop


def _make_emit(q: queue.Queue):
    """Create an emit function bound to a specific queue."""
    def _emit(t, d):
        q.put({"type": t, "data": d})
    return _emit


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    def event_stream():
        while True:
            try:
                event = _current_queue.get(timeout=30)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/start_god", methods=["POST"])
def start_god():
    """Start god mode: 9 AI agents."""
    _stop_old_game()
    _clear_events()
    _session.update(running=True, mode="god", human=None, agents=[])
    global _game_thread
    _game_thread = threading.Thread(target=_run_god_game, daemon=True)
    _game_thread.start()
    return jsonify({"status": "ok", "mode": "god"})


@app.route("/api/start_player", methods=["POST"])
def start_player():
    """Start player mode: 1 human + 8 AI."""
    _stop_old_game()
    data = request.get_json() or {}
    role_choice = data.get("role", "random")
    _clear_events()
    _session.update(running=True, mode="player", human=None, agents=[])
    global _game_thread
    _game_thread = threading.Thread(target=_run_player_game, args=(role_choice,), daemon=True)
    _game_thread.start()
    return jsonify({"status": "ok", "mode": "player"})


@app.route("/api/player_input", methods=["POST"])
def player_input():
    """Human player submits speech/vote/action."""
    human = _session.get("human")
    if not human:
        return jsonify({"error": "无活跃玩家"}), 400
    text = (request.get_json() or {}).get("text", "")
    human.submit_input(text)
    return jsonify({"status": "ok"})


@app.route("/api/player_state")
def player_state():
    """Get current state for the human player."""
    human = _session.get("human")
    if not human:
        return jsonify({"waiting": True})
    return jsonify({"waiting": human._pending_future is not None,
                    "info": human.public_info})


@app.route("/api/status")
def api_status():
    return jsonify({"running": _session["running"], "mode": _session["mode"]})


def _clear_events():
    while not _current_queue.empty():
        _current_queue.get_nowait()


def _stop_old_game():
    """Stop any running game before starting a new one."""
    global _current_queue, _game_thread, _game_stop_event
    _game_stop_event.set()
    # Unblock SSE handler: put sentinel in old queue, then replace with new queue
    old_queue = _current_queue
    _current_queue = queue.Queue()
    old_queue.put({"type": "_wake", "data": {}})  # Unblocks old get() call
    # Resolve pending human input
    human = _session.get("human")
    if human and human._pending_future and not human._pending_future.done():
        human._pending_future.set_result("")
    _game_stop_event = threading.Event()
    _session.update(running=False, mode=None, human=None, agents=[])
    _game_thread = None


@app.route("/api/reset", methods=["POST"])
def reset():
    """Reset the game session (called on page reload/navigation)."""
    _stop_old_game()
    _clear_events()
    return jsonify({"status": "ok"})


def _run_god_game():
    import asyncio
    from agent import PlayerAgent
    from game import werewolves_game
    import game as gm
    import agent as ag
    # Capture this game's queue — old games write to their own (now dead) queue
    my_emit = _make_emit(_current_queue)
    gm._emit_web_event = my_emit
    ag._emit_web = my_emit
    agents = [PlayerAgent(f"Player{i}") for i in range(1, 10)]
    _session["agents"] = agents
    # Init event is emitted by game.py after role assignment (with correct roles)
    asyncio.run(werewolves_game(agents))
    my_emit("done", {})
    _session["running"] = False


def _run_player_game(role_choice):
    import asyncio
    from agent import PlayerAgent
    from game import werewolves_game
    from web_human import WebHumanAgent
    import game as gm
    import agent as ag
    my_emit = _make_emit(_current_queue)
    gm._emit_web_event = my_emit
    ag._emit_web = my_emit

    roles = ["werewolf"] * 3 + ["villager"] * 3 + ["seer", "witch", "hunter"]
    random.shuffle(roles)
    human_role = role_choice if role_choice in roles else roles.pop()
    if human_role not in roles and human_role != role_choice:
        roles.pop(roles.index(human_role)) if human_role in roles else None

    human = WebHumanAgent("Player1")
    ai_agents = [PlayerAgent(f"Player{i}") for i in range(2, 10)]
    all_agents = [human] + ai_agents
    random.shuffle(ai_agents)
    all_roles_list = [human_role] + roles
    random.shuffle(all_roles_list)

    all_names = [a.name for a in all_agents]
    for agent, role in zip(all_agents, all_roles_list):
        agent.state["role"] = role
        agent.state["camp"] = "werewolf" if role == "werewolf" else "villager"
        agent.state["is_alive"] = True
        agent.state["current_alive"] = all_names.copy()
        agent.state["name_to_role"] = deepcopy(
            {a.name: r for a, r in zip(all_agents, all_roles_list)})
        if hasattr(agent, 'wm'):
            agent.wm.my_role = role
            agent.wm.my_camp = "werewolf" if role == "werewolf" else "villager"
            agent.wm.alive_players = all_names.copy()
    human.update_public_info()

    _session.update(running=True, mode="player", human=human, agents=all_agents)
    # Emit init WITHOUT roles (player mode: hide roles)
    my_emit("init", {"players": [{"name": a.name, "role": "???", "camp": "???"}
                                for a in all_agents], "my_role": human_role})
    asyncio.run(werewolves_game(all_agents))
    # Reveal all roles at end
    my_emit("reveal", {"players": [{"name": a.name, "role": a.state.get("role", "?"),
                                   "camp": a.state.get("camp", "?")} for a in all_agents]})
    my_emit("done", {})
    _session["running"] = False


def emit_event(event_type, data):
    _current_queue.put({"type": event_type, "data": data})


if __name__ == "__main__":
    print("狼人杀 Web 平台")
    print("http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
