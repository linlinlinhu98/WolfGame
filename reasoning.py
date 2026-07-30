# -*- coding: utf-8 -*-
"""Reasoning engine for Werewolf Game agents.

Architecture based on DVM (2025) + cognitive agent survey (Hu et al., 2024):

Perception → Memory (Working + Episodic + Belief) → Reasoning (Strategy + ToM + Speech)

Provides:
- GameEvent: typed, structured events instead of regex-based parsing
- WorkingMemory: current game state snapshot
- EpisodicMemory: chronological event log
- BeliefTracker: probabilistic Theory-of-Mind with evidence tracking
- StrategyPlanner: role-specific strategy selection
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


# Perception Layer: Structured Game Events

class EventType(Enum):
    ROLE_ASSIGNED = "role_assigned"
    PHASE_CHANGE = "phase_change"
    PLAYER_LIST = "player_list"
    NIGHT_DEATH = "night_death"
    PEACEFUL_NIGHT = "peaceful_night"
    SPEECH = "speech"
    VOTE_RESULT = "vote_result"
    PLAYER_ELIMINATED = "player_eliminated"
    WITCH_INFO = "witch_info"  # Witch told who died
    SEER_RESULT = "seer_result"  # Seer told check result
    HUNTER_PROMPT = "hunter_prompt"  # Hunter asked to shoot
    GAME_OVER = "game_over"


@dataclass
class GameEvent:
    """A structured game event. All perception outputs this type."""
    type: EventType
    round_num: int = 0
    speaker: str = ""           # Who sent the message
    target: str = ""            # Who the event is about
    content: str = ""           # Raw content
    metadata: dict = field(default_factory=dict)


# Memory Layer

@dataclass
class WorkingMemory:
    """Current game state. Fast-access, frequently updated."""
    phase: str = "init"              # night / day / discussion / voting / game_over
    round_num: int = 0
    alive_players: List[str] = field(default_factory=list)
    dead_players: List[str] = field(default_factory=list)
    my_role: str = ""
    my_camp: str = ""                # "werewolf" or "villager"
    my_teammates: List[str] = field(default_factory=list)  # Wolves-only
    night_killed: str = ""           # Who died tonight
    healing_used: bool = False
    poison_used: bool = False
    seer_checks: Dict[str, str] = field(default_factory=dict)  # {name: role}
    speaking_order: List[str] = field(default_factory=list)

    @property
    def n_alive(self) -> int:
        return len(self.alive_players)

    @property
    def n_dead(self) -> int:
        return len(self.dead_players)

    def snapshot(self) -> str:
        """Generate a compact text summary for LLM prompts."""
        return (
            f"第{self.round_num}轮 | 阶段:{self.phase} | "
            f"存活{self.n_alive}人:{','.join(self.alive_players)} | "
            f"已死{self.n_dead}人:{','.join(self.dead_players) if self.dead_players else '无'}"
        )


@dataclass
class EpisodicMemory:
    """Chronological event log. Used for reflection and detailed reasoning."""
    events: List[GameEvent] = field(default_factory=list)
    my_speeches: List[str] = field(default_factory=list)
    my_votes: List[Tuple[int, str]] = field(default_factory=list)  # [(round, target)]

    def add(self, event: GameEvent) -> None:
        self.events.append(event)
        if len(self.events) > 200:
            self.events = self.events[-150:]

    def recent_speeches(self, n: int = 8) -> str:
        """Get recent speeches in chronological order."""
        speeches = [e for e in self.events[-30:]
                    if e.type == EventType.SPEECH]
        lines = []
        for e in speeches[-n:]:
            lines.append(f"[{e.speaker}]: {e.content[:150]}")
        return "\n".join(lines) if lines else "（暂无讨论记录）"

    def vote_summary(self) -> str:
        """Summary of vote history."""
        votes = [e for e in self.events if e.type == EventType.VOTE_RESULT]
        if not votes:
            return "暂无投票记录"
        lines = []
        for e in votes[-5:]:
            lines.append(f"  第{e.round_num}轮: {e.content[:100]}")
        return "\n".join(lines)

    def death_timeline(self) -> str:
        """Timeline of deaths."""
        deaths = [e for e in self.events
                  if e.type in (EventType.NIGHT_DEATH, EventType.PLAYER_ELIMINATED)]
        lines = []
        for e in deaths:
            phase = "夜晚" if e.type == EventType.NIGHT_DEATH else "白天投票"
            for p in e.metadata.get("players", []):
                lines.append(f"  第{e.round_num}轮{phase}: {p} 淘汰")
        return "\n".join(lines) if lines else "暂无死亡记录"


# Belief Tracker: Probabilistic Theory of Mind

@dataclass
class PlayerBelief:
    """What this agent believes about one other player."""
    name: str
    p_werewolf: float = 0.33       # P(is werewolf)
    p_seer: float = 0.0
    p_witch: float = 0.0
    p_hunter: float = 0.0
    p_villager: float = 0.0
    evidence_for: List[str] = field(default_factory=list)    # Suspicious
    evidence_against: List[str] = field(default_factory=list)  # Trustworthy
    last_updated_round: int = 0


class BeliefTracker:
    """Probabilistic Theory-of-Mind tracker.

    Tracks P(wolf|player) with calibrated evidence updates.
    Enhanced with: trust network, role claims, contradiction detection.
    """

    def __init__(self, player_name: str):
        self.player_name = player_name
        self.beliefs: Dict[str, PlayerBelief] = {}
        # Second-order: what I think each player thinks about me
        self.perceived_by_others: Dict[str, float] = {}
        # Trust network: A trusts B (simplified)
        self.trust_edges: Dict[Tuple[str, str], float] = {}

    def initialize(
        self, all_players: List[str], my_role: str, name_to_role: Dict[str, str]
    ) -> None:
        """Set initial beliefs with prior probabilities."""
        n_wolves = sum(1 for r in name_to_role.values() if r == "werewolf")
        n_total = len(all_players)
        prior_wolf = n_wolves / n_total if n_total > 0 else 0.33
        prior_role = (1.0 - prior_wolf) / 4  # Distribute among 4 non-wolf roles

        for name in all_players:
            if name == self.player_name:
                continue
            belief = PlayerBelief(name=name)
            if my_role == "werewolf" and name_to_role.get(name) == "werewolf":
                # Known teammate → trusted
                belief.p_werewolf = 0.0
                belief.p_villager = 1.0  # Technically incorrect but for modeling
                belief.evidence_against.append("已知狼队友")
            else:
                belief.p_werewolf = prior_wolf
                belief.p_seer = prior_role
                belief.p_witch = prior_role
                belief.p_hunter = prior_role
                belief.p_villager = prior_role
            self.beliefs[name] = belief

    def _get(self, name: str) -> PlayerBelief:
        if name not in self.beliefs:
            self.beliefs[name] = PlayerBelief(name=name)
        return self.beliefs[name]

    def _adjust(
        self, name: str, delta_wolf: float,
        evidence: str, is_suspicious: bool = True
    ) -> None:
        """Adjust P(wolf) with bounded update and evidence recording."""
        b = self._get(name)
        old = b.p_werewolf
        b.p_werewolf = max(0.0, min(1.0, old + delta_wolf))
        if is_suspicious:
            b.evidence_for.append(evidence)
            if len(b.evidence_for) > 6:
                b.evidence_for = b.evidence_for[-6:]
        else:
            b.evidence_against.append(evidence)
            if len(b.evidence_against) > 6:
                b.evidence_against = b.evidence_against[-6:]

    # Evidence sources

    def observe_speech(
        self, speaker: str, content: str, is_alive: bool, round_num: int
    ) -> None:
        """Update beliefs from observed speech."""
        if speaker == self.player_name or not is_alive:
            return

        # Hollow/evasive → slightly more suspicious
        hollow_words = ["再观察", "再看看", "信息不足", "不好说", "不确定"]
        if sum(1 for w in hollow_words if w in content) >= 2:
            self._adjust(speaker, 0.04, f"R{round_num}发言空洞回避表态")

        # Specific analysis → slightly less suspicious
        specific_words = ["因为", "证据", "矛盾", "投票", "上轮", "怀疑"]
        if sum(1 for w in specific_words if w in content) >= 3:
            self._adjust(speaker, -0.03, f"R{round_num}发言有具体分析", is_suspicious=False)

        # Self-contradiction with previous stance (lightweight)
        # Will be enhanced by LLM-based analysis

    def observe_vote(
        self, voter: str, target: str, all_votes: Dict[str, int], round_num: int
    ) -> None:
        """Update beliefs from voting behavior."""
        if voter == self.player_name:
            return
        total = sum(all_votes.values())
        if total == 0:
            return
        max_votes = max(all_votes.values())

        # Bandwagon: voted with majority
        if all_votes.get(target, 0) == max_votes and max_votes >= total * 0.5:
            self._adjust(voter, 0.03, f"R{round_num}跟风投票给{target}")

        # Lone vote: unique target when there's a clear majority
        if all_votes.get(target, 0) == 1 and max_votes >= 3:
            self._adjust(voter, 0.03, f"R{round_num}孤立投票给{target}")

    def observe_night_death(
        self, dead: str, name_to_role: Dict[str, str],
        alive: List[str], round_num: int
    ) -> None:
        """Update beliefs when someone dies at night."""
        role = name_to_role.get(dead)
        if role in ("seer", "witch", "hunter"):
            # Key role killed → wolves are strategic
            for name in alive:
                if name == self.player_name:
                    continue
                b = self._get(name)
                if b.p_werewolf < 0.3:
                    self._adjust(name, 0.02, f"R{round_num}{dead}({role})被杀，低嫌疑者可能隐藏")

    def observe_role_claim(self, claimant: str, claimed_role: str, round_num: int) -> None:
        """Update beliefs when someone claims a role."""
        if claimant == self.player_name:
            return
        if claimed_role == "seer":
            self._adjust(claimant, -0.05, f"R{round_num}跳预言家", is_suspicious=False)
        # Track the claim
        b = self._get(claimant)
        if claimed_role == "seer":
            b.p_seer = min(1.0, b.p_seer + 0.3)

    def observe_contradiction(
        self, player: str, description: str, round_num: int
    ) -> None:
        """LLM-detected contradiction in a player's statements."""
        self._adjust(player, 0.08, f"R{round_num}矛盾: {description}")

    # Query interface

    def get_suspect_ranking(self, alive: List[str]) -> List[Tuple[str, float]]:
        """Return (name, P_wolf) sorted descending for alive players only."""
        ranking = []
        for name in alive:
            if name == self.player_name:
                continue
            ranking.append((name, self._get(name).p_werewolf))
        ranking.sort(key=lambda x: x[1], reverse=True)
        return ranking

    def get_trust_ranking(self, alive: List[str]) -> List[Tuple[str, float]]:
        """Return (name, trust_score) sorted descending."""
        return [(n, 1.0 - s) for n, s in self.get_suspect_ranking(alive)]

    def get_belief_summary(self, alive: List[str]) -> str:
        """Generate rich belief summary for LLM prompts."""
        ranking = self.get_suspect_ranking(alive)
        lines = ["## 对各玩家的怀疑度评估"]
        for name, score in ranking:
            b = self._get(name)
            evidence = b.evidence_for[-3:] + b.evidence_against[-2:]
            ev_str = "; ".join(evidence) if evidence else "暂无证据"

            if score > 0.6:
                label = "🔴 高度怀疑"
            elif score > 0.45:
                label = "🟡 中度怀疑"
            elif score > 0.3:
                label = "🟢 中性"
            else:
                label = "🔵 倾向信任"

            lines.append(f"- {name}: {label} (狼人概率:{score:.0%}) | {ev_str}")

        return "\n".join(lines)

    def get_self_perception(self) -> str:
        """Estimate how others might perceive me."""
        # Simplified: if I'm a wolf playing quietly, others might trust me
        # This will be enhanced with LLM analysis
        return "（心智理论: 尚未建模他人对你的看法）"


# Strategy Definitions

STRATEGIES = {
    "werewolf": [
        ("deep_cover", "深藏不露：发言像普通村民，避免引导怀疑方向，让好人互斗"),
        ("aggressor", "积极进攻：主动分析、点名怀疑好人，掌握话语权"),
        ("deflector", "转移注意：当队友被怀疑时，将焦点转向其他玩家"),
        ("counter_claim", "反跳身份：如果预言家跳了查杀你，你可以反跳预言家混淆视听"),
    ],
    "seer": [
        ("early_reveal", "早跳身份：第一轮就公布查验结果，带领好人阵营"),
        ("hide_check", "隐藏查验：以村民视角发言，暗中利用查验信息分析"),
        ("bait_wolves", "诱饵策略：故意表现得像神职，吸引狼人刀你（有女巫保护时）"),
    ],
    "witch": [
        ("save_early", "早救人：第一晚必救，保住人数优势"),
        ("save_key", "保神职：解药留给预言家等关键角色"),
        ("poison_aggressive", "激进毒人：对高度怀疑的玩家使用毒药"),
        ("hide_info", "信息隐藏：利用死亡信息暗中分析，不暴露女巫身份"),
    ],
    "hunter": [
        ("deterrent", "威慑策略：暗示自己有能力反击，让狼人忌惮"),
        ("hide_role", "隐藏身份：伪装村民，被淘汰时出奇不意带人"),
        ("confirmed_shot", "确认目标：只在对某玩家高度确定时才开枪"),
    ],
    "villager": [
        ("analyst", "分析型：积极分析发言和投票，找出逻辑漏洞"),
        ("follower", "跟随型：跟随信任的玩家（如预言家）投票"),
        ("sacrifice", "挡刀型：表现得像神职，吸引狼人刀你保护真神职"),
        ("questioner", "追问型：向发言模糊的玩家追问细节，暴露狼人"),
    ],
}
