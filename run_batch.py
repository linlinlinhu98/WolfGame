# -*- coding: utf-8 -*-
"""Batch god-mode games for statistics.

Usage: python run_batch.py [N]
Runs N sequential 9-AI games (same setup as web god mode) and prints a
per-game summary plus a final win-rate tally. Game data is written to
game_data/ by collect_game_data() as usual.
"""
import asyncio
import glob
import json
import sys
import time

from agent import PlayerAgent
from game import werewolves_game


def _latest_game_data():
    """werewolves_game() returns None — the result lands in game_data/."""
    files = glob.glob("game_data/*.json")
    if not files:
        return {}
    return max(files, key=lambda f: __import__("os").path.getmtime(f))


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    tally = {"villager": 0, "werewolf": 0, "unknown": 0}
    rounds_list = []
    for i in range(1, n + 1):
        agents = [PlayerAgent(f"Player{j}") for j in range(1, 10)]
        t0 = time.time()
        asyncio.run(werewolves_game(agents))
        try:
            d = json.load(open(_latest_game_data(), encoding="utf-8"))
            win = str(d.get("winner", "")).lower()
            if "werewolf" in win or "狼" in win:
                winner = "werewolf"
            elif "villager" in win or "民" in win or "好人" in win:
                winner = "villager"
            else:
                winner = "unknown"
            rounds = len(d.get("rounds", []))
        except Exception:
            winner, rounds = "unknown", 0
        tally[winner] = tally.get(winner, 0) + 1
        rounds_list.append(rounds)
        print(f"[{i}/{n}] winner={winner} rounds={rounds} elapsed={time.time()-t0:.0f}s")
        sys.stdout.flush()
    total = sum(tally.values())
    print("=" * 46)
    print(f"batch done: {total} games, avg rounds: {sum(rounds_list)/total:.1f}" if total else "no games")
    for k, v in tally.items():
        print(f"  {k:9}: {v}/{total} = {v/total*100:.1f}%" if total else f"  {k}: {v}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
