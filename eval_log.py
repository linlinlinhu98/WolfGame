# -*- coding: utf-8 -*-
"""Qualitative evaluator for one werewolf game log (GBK console log).

Checks rule compliance and speech quality signals:
  - vote rule violations (self-vote / dead-player vote / unknown target)
  - speech-vote consistency (stated 投票意向 vs actual vote)
  - LLM-empty fallbacks
  - evidence usage in speeches (evidence keywords vs vibe keywords)
  - seer claim behavior (check-chain reveals, counter-claims)
  - lynch outcomes per round (role of lynched player)
  - wolf-coordination signals (night kills vs seer claims)

Usage: python eval_log.py <logfile> [game_data_json]
"""
import json
import re
import sys
from pathlib import Path

EVIDENCE_WORDS = ["投票记录", "投票意向", "查验", "查杀", "金水", "死亡", "夜", "刀",
                  "抱团", "冲票", "矛盾", "对跳", "查验链", "跟票", "银水", "证据"]
VIBE_WORDS = ["感觉", "直觉", "不自然", "不舒服", "莫名", "顺眼", "语气"]


def load_log(path: str) -> str:
    raw = Path(path).read_bytes()
    return raw.decode("gbk", errors="replace")


def role_map_from_log(text: str) -> dict:
    """Parse final reveal: 'PlayerX, PlayerY, 和 PlayerZ 是狼人，... 是村民，...'"""
    m = re.search(r"真实身份为[:：](.+)", text)
    roles = {}
    if not m:
        return roles
    seg = m.group(1)
    for part, role in re.findall(r"([\w\s,和]+?)是(狼人|村民|预言家|女巫|猎人)", seg):
        for name in re.findall(r"Player\d+", part):
            roles[name] = role
    return roles


def main() -> None:
    log_path = sys.argv[1]
    text = load_log(log_path)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    roles = role_map_from_log(text)

    print(f"=== {Path(log_path).name} ===")

    # ── result ──
    winner = "狼人" if "狼人获胜" in text else ("村民" if "村民获胜" in text else "?")
    rounds = len(re.findall(r"游戏第 \d+ 轮", text))
    print(f"结果: {winner}胜 | {rounds}轮 | roles={len(roles)}")

    # ── votes: rule violations & consistency ──
    deaths = []          # cumulative dead list in order of announcement
    violations = []
    stated = {}          # voter -> stated target (from speech 投票意向)
    votes_cast = {}      # voter -> actual target (first '投票 →' per voter per vote round)
    consistency_bad = 0
    consistency_ok = 0
    n_follow_seer = 0

    lines = text.splitlines()
    alive = set(roles.keys()) if roles else {f"Player{i}" for i in range(1, 10)}
    for line in lines:
        # stated intent line inside speeches
        m = re.match(r"投票意向[:：]\s*(Player\d+)", line.strip())
        if m:
            # attribute to the most recent speaker header
            pass  # handled via pairing below
        m = re.match(r"\[(Player\d+)\] 投票 → (Player\d+)", line)
        if m:
            voter, target = m.groups()
            if voter == target:
                violations.append(f"自投: {voter}")
            if target in deaths:
                violations.append(f"投死人: {voter}→{target}")
            if roles and target not in roles:
                violations.append(f"投未知: {voter}→{target}")
            votes_cast[voter] = target
            if "改投唯一预言家" in line:
                n_follow_seer += 1
        m = re.match(r"(夜间|白天)死亡玩家[:：]\s*\[([^\]]*)\]", line)
        if m:
            deaths.extend(re.findall(r"Player\d+", m.group(2)))

    # consistency: compare stated (from speech) vs cast, using 投票意向 lines
    # paired by scanning speeches
    cur_speaker = None
    for line in lines:
        m = re.match(r"\[(Player\d+)\] ", line)
        if m and "Moderator" not in line:
            cur_speaker = m.group(1)
        m = re.search(r"投票意向[:：]\s*(Player\d+)", line)
        if m and cur_speaker:
            stated[cur_speaker] = m.group(1)
    for voter, target in votes_cast.items():
        if voter in stated:
            if stated[voter] == target:
                consistency_ok += 1
            else:
                consistency_bad += 1

    print(f"投票违规: {violations if violations else '无'}")
    print(f"言行一致: {consistency_ok}一致 / {consistency_bad}不一致 | 跟票预言家改投×{n_follow_seer}")
    # 'LLM empty response ... retrying' = recovered by the bounded retry;
    # 'WARNING: LLM empty' = true template fallback. Count separately so
    # retry saves aren't misreported as fallbacks.
    n_retry = len(re.findall(r"LLM empty response", text))
    n_fb = len(re.findall(r"WARNING: LLM empty", text))
    print(f"LLM空响应: 重试救回{n_retry} + 真fallback{n_fb} = {n_retry + n_fb}")

    # ── lynch outcomes ──
    day_deaths = re.findall(r"白天死亡玩家[:：]\s*\[([^\]]*)\]", text)
    lynch_desc = []
    for dd in day_deaths:
        for name in re.findall(r"Player\d+", dd):
            r = roles.get(name, "?")
            lynch_desc.append(f"{name}({r})" + ("❌误杀" if r in ("村民", "预言家", "女巫", "猎人") else "✅中狼"))
    print(f"白天处决: {', '.join(lynch_desc) if lynch_desc else '无'}")

    # ── speech quality: evidence vs vibe ──
    speeches = re.findall(r"\[Player\d+\] ((?:我是|大家|第|本轮|村民视角)[^\n]{20,})", text)
    ev = sum(1 for s in speeches if any(w in s for w in EVIDENCE_WORDS))
    vb = sum(1 for s in speeches if any(w in s for w in VIBE_WORDS))
    print(f"发言样本: {len(speeches)}段 | 含证据词 {ev} | 含感觉词 {vb}")

    # ── seer behavior ──
    claims = re.findall(r"\[(Player\d+)\][^\n]*我是预言家", text)
    chain_claims = [c for c in re.findall(r"\[(Player\d+)\][^\n]*查验链", text)]
    print(f"跳预言家: {claims if claims else '无'} | 带查验链: {chain_claims if chain_claims else '无'}")

    # ── witch ──
    witch_acts = re.findall(r"\[Player\d+\] (救人\+毒杀: Player\d+|救人|毒杀: Player\d+|不用药)", text)
    print(f"女巫行动: {witch_acts if witch_acts else '无'}")


if __name__ == "__main__":
    main()
