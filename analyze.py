# -*- coding: utf-8 -*-
"""Aggregate statistics across saved games in game_data/.

Usage:
    python analyze.py            # summary table
    python analyze.py --verbose  # also list each game

Metrics: camp win rate, per-role win rate, average game length.
A role "wins" when its camp wins (werewolves vs villagers+specials).
"""
import argparse
import json
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent / "game_data"

CAMP_OF = {
    "werewolf": "werewolf",
    "villager": "villager",
    "seer": "villager",
    "witch": "villager",
    "hunter": "villager",
}


def load_games() -> list[dict]:
    games = []
    if not DATA_DIR.exists():
        return games
    for f in sorted(DATA_DIR.glob("*.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                d = json.load(fp)
            if d.get("winner") in ("werewolf", "villager"):
                games.append(d)
        except (json.JSONDecodeError, OSError):
            continue
    return games


def main() -> None:
    parser = argparse.ArgumentParser(description="Werewolf game statistics")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    games = load_games()
    if not games:
        print("game_data/ 中没有有效对局数据。先运行几局游戏。")
        return

    camp_wins = defaultdict(int)
    role_stat = defaultdict(lambda: [0, 0])  # role -> [wins, games]
    total_rounds = 0

    for g in games:
        winner = g["winner"]
        camp_wins[winner] += 1
        total_rounds += len(g.get("rounds", []))
        for p in g.get("players", []):
            role = p.get("role", "unknown")
            won = CAMP_OF.get(role) == winner
            role_stat[role][0] += int(won)
            role_stat[role][1] += 1
        if args.verbose:
            print(f"  {g.get('timestamp', '?')[:19]}  {winner:>9} 胜  "
                  f"({len(g.get('rounds', []))}轮)  id={g.get('id', '?')}")

    n = len(games)
    print(f"\n{'='*46}")
    print(f"  总对局: {n}   平均轮次: {total_rounds/n:.1f}")
    print(f"{'='*46}")
    print(f"  阵营胜率")
    for camp in ("werewolf", "villager"):
        w = camp_wins[camp]
        label = "狼人" if camp == "werewolf" else "好人"
        print(f"    {label}: {w}/{n} = {w/n:.1%}")
    print(f"{'='*46}")
    print(f"  角色胜率 (出场次数)")
    label_map = {"werewolf": "狼人", "villager": "村民", "seer": "预言家",
                 "witch": "女巫", "hunter": "猎人"}
    for role in ("werewolf", "villager", "seer", "witch", "hunter"):
        w, t = role_stat.get(role, [0, 0])
        if t:
            print(f"    {label_map.get(role, role)}: {w}/{t} = {w/t:.1%}  (出场{t}次)")
    print(f"{'='*46}\n")


if __name__ == "__main__":
    main()
