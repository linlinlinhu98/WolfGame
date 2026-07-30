# -*- coding: utf-8 -*-
"""The main entry point for the werewolf game.

Modes:
  God mode:  Watch 9 AI agents play. Full output with internal reasoning.
  Player mode: Play as a human alongside 8 AI agents.

Usage:
  python main.py          # God mode (default)
  python main.py player   # Player mode
  python main.py web      # God mode + web visualization
"""
import asyncio
import sys
import random
import numpy as np
from copy import deepcopy

from agent import PlayerAgent
from game import werewolves_game


async def god_mode(start_web: bool = False):
    """God mode: 9 AI agents, full output visible."""
    if start_web:
        import threading
        threading.Thread(target=start_web_ui, daemon=True).start()

    players = [PlayerAgent(f"Player{i}") for i in range(1, 10)]
    await werewolves_game(players)


async def player_mode():
    """Player mode: 1 human + 8 AI agents."""
    from human_agent import HumanAgent

    # Let human choose their name
    print("狼人杀 - 玩家模式")
    print("可选身份: werewolf, villager, seer, witch, hunter")
    role_choice = input("选择你的身份 (回车随机): ").strip().lower()

    # Assign roles
    roles = ["werewolf"] * 3 + ["villager"] * 3 + ["seer", "witch", "hunter"]
    random.shuffle(roles)

    human_name = "Player1"
    human_role = None

    if role_choice and role_choice in ["werewolf", "villager", "seer", "witch", "hunter"]:
        # Remove one of the chosen role from the pool
        human_role = role_choice
        roles.remove(role_choice)
        print(f"你选择了: {human_role}")
    else:
        human_role = roles.pop()
        print(f"随机分配: {human_role}")

    # Create agents: 1 human + 8 AI
    human = HumanAgent(human_name)
    ai_agents = [PlayerAgent(f"Player{i}") for i in range(2, 10)]

    # Assign roles: human gets their chosen role, rest shuffled
    random.shuffle(ai_agents)
    all_roles = [human_role] + roles
    random.shuffle(all_roles)

    # Tell each agent their role
    name_to_role = {}
    all_agents = [human] + ai_agents
    all_player_names = [a.name for a in all_agents]

    # Manually set up roles (skip the normal werewolves_game setup)
    for agent, role in zip(all_agents, all_roles):
        agent.state["role"] = role
        agent.state["camp"] = "werewolf" if role == "werewolf" else "villager"
        agent.state["is_alive"] = True
        agent.state["current_alive"] = all_player_names.copy()
        agent.state["name_to_role"] = deepcopy(
            {a.name: r for a, r in zip(all_agents, all_roles)}
        )
        if hasattr(agent, 'wm'):
            agent.wm.my_role = role
            agent.wm.my_camp = "werewolf" if role == "werewolf" else "villager"
            agent.wm.alive_players = all_player_names.copy()

    print(f"\n你的身份: {human_role} ({'狼人' if human_role == 'werewolf' else '好人'}阵营)")
    if human_role == "werewolf":
        mates = [a.name for a, r in zip(all_agents, all_roles)
                if r == "werewolf" and a.name != human_name]
        print(f"你的狼队友: {', '.join(mates)}")
    print()

    await werewolves_game(all_agents)


def start_web_ui():
    """Start the visualization web server in a background thread."""
    try:
        from web_ui.server import app
        print("\n可视化平台: http://localhost:5000")
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
    except ImportError:
        print("需要安装 flask: pip install flask")
    except Exception as e:
        print(f"可视化平台启动失败: {e}")


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "god"

    if mode == "player":
        await player_mode()
    elif mode == "web":
        await god_mode(start_web=True)
    else:
        await god_mode()


if __name__ == "__main__":
    asyncio.run(main())
