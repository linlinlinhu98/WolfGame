# -*- coding: utf-8 -*-
"""Memory system for Werewolf agents.

Three-layer memory architecture:
  1. WorkingMemory  — current game state (fast access)
  2. EpisodicMemory — chronological event log with importance weighting
  3. SemanticMemory — extracted facts and patterns (LLM-assisted)

Each agent has their OWN memory — no information leakage between agents.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


# Memory Entry

class EntryType(Enum):
    SYSTEM = "system"         # Moderator announcements
    SPEECH = "speech"         # Player discussion speech
    VOTE = "vote"             # Voting action
    NIGHT_ACTION = "night"    # Night kill/check/heal/poison
    DEATH = "death"           # Player elimination
    REVEAL = "reveal"         # Role reveal (seer claim, etc.)
    MY_ACTION = "my_action"   # Agent's own action
    MY_SPEECH = "my_speech"   # Agent's own speech
    REFLECTION = "reflection" # Post-game reflection


@dataclass
class MemoryEntry:
    """A single memory entry — what happened, when, who was involved."""
    type: EntryType
    round_num: int
    speaker: str              # Who said/did this
    content: str              # What was said/done
    importance: int = 1       # 1-5, higher = more important
    tags: List[str] = field(default_factory=list)  # e.g. ["accusation", "defense"]
    metadata: dict = field(default_factory=dict)     # Extra structured data


# Episodic Memory — chronological log with retrieval

class EpisodicMemory:
    """Agent's personal memory store. Each agent has their own instance.

    Only stores what THIS agent observed — no information leakage.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.entries: List[MemoryEntry] = []
        self._speech_index: Dict[str, List[int]] = {}  # speaker → entry indices
        self._round_index: Dict[int, List[int]] = {}   # round → entry indices

    def add(self, entry: MemoryEntry) -> None:
        """Add a memory entry with automatic indexing."""
        idx = len(self.entries)
        self.entries.append(entry)

        # Index by speaker
        if entry.speaker not in self._speech_index:
            self._speech_index[entry.speaker] = []
        self._speech_index[entry.speaker].append(idx)

        # Index by round
        r = entry.round_num
        if r not in self._round_index:
            self._round_index[r] = []
        self._round_index[r].append(idx)

        # Auto-tag based on content
        self._auto_tag(entry)

        # Trim if too large
        if len(self.entries) > 300:
            self.entries = self.entries[-200:]
            self._rebuild_indices()

    def _auto_tag(self, entry: MemoryEntry) -> None:
        """Automatically tag entries based on content patterns."""
        c = entry.content
        if entry.type != EntryType.SPEECH:
            return
        tags = []
        if any(w in c for w in ["怀疑", "可疑", "是狼", "狼人", "suspect", "wolf"]):
            tags.append("accusation")
        if any(w in c for w in ["信任", "好人", "相信", "村民", "trust", "good"]):
            tags.append("defense")
        if any(w in c for w in ["我是预言家", "我是女巫", "我是猎人", "I am seer", "I am witch"]):
            tags.append("role_claim")
        if any(w in c for w in ["投票", "投给", "vote"]):
            tags.append("vote_intent")
        if any(w in c for w in ["昨晚", "查验", "杀了", "killed", "checked"]):
            tags.append("night_info")
        entry.tags.extend(tags)

    def _rebuild_indices(self) -> None:
        """Rebuild all indices after trimming."""
        self._speech_index.clear()
        self._round_index.clear()
        for i, e in enumerate(self.entries):
            self._speech_index.setdefault(e.speaker, []).append(i)
            self._round_index.setdefault(e.round_num, []).append(i)

    # Retrieval

    def by_speaker(self, name: str, max_n: int = 20) -> List[MemoryEntry]:
        """Get all entries from a specific player."""
        indices = self._speech_index.get(name, [])[-max_n:]
        return [self.entries[i] for i in indices]

    def by_round(self, round_num: int) -> List[MemoryEntry]:
        """Get all entries from a specific round."""
        indices = self._round_index.get(round_num, [])
        return [self.entries[i] for i in indices]

    def by_tag(self, tag: str, max_n: int = 20) -> List[MemoryEntry]:
        """Get entries with a specific tag."""
        matches = [e for e in self.entries if tag in e.tags]
        return matches[-max_n:]

    def recent(self, n: int = 10) -> List[MemoryEntry]:
        """Get the most recent entries."""
        return self.entries[-n:]

    def speeches_only(self, max_n: int = 30) -> List[MemoryEntry]:
        """Get only speech entries."""
        return [e for e in self.entries if e.type == EntryType.SPEECH][-max_n:]

    def my_speeches(self) -> List[str]:
        """Get my own speeches in order."""
        return [e.content for e in self.entries if e.type == EntryType.MY_SPEECH]

    # Formatted output for LLM prompts

    def discussion_by_round(self) -> str:
        """Format all discussion speeches grouped by round. Rich context."""
        rounds: Dict[int, List[MemoryEntry]] = {}
        for e in self.entries:
            if e.type in (EntryType.SPEECH, EntryType.MY_SPEECH):
                rounds.setdefault(e.round_num, []).append(e)
            elif e.type == EntryType.DEATH:
                rounds.setdefault(e.round_num, []).append(e)
            elif e.type == EntryType.REVEAL:
                rounds.setdefault(e.round_num, []).append(e)

        if not rounds:
            return "（暂无讨论记录）"

        lines = []
        for r in sorted(rounds.keys()):
            entries = rounds[r]
            lines.append(f"\n## 第{r}轮")
            for e in entries:
                if e.type == EntryType.DEATH:
                    lines.append(f"  💀 {e.content}")
                elif e.type == EntryType.REVEAL:
                    lines.append(f"  📢 {e.speaker}: {e.content}")
                elif e.type == EntryType.SPEECH:
                    tag_str = f" [{','.join(e.tags)}]" if e.tags else ""
                    lines.append(f"  [{e.speaker}]{tag_str}: {e.content[:200]}")
                elif e.type == EntryType.MY_SPEECH:
                    lines.append(f"  [你]{tag_str}: {e.content[:200]}" if e.tags else f"  [你]: {e.content[:200]}")
        return "\n".join(lines)

    def vote_history(self) -> str:
        """Format voting history."""
        votes = [e for e in self.entries if e.type == EntryType.VOTE]
        if not votes:
            return "暂无投票记录"
        lines = ["\n## 投票历史"]
        for e in votes:
            lines.append(f"  第{e.round_num}轮: {e.content[:150]}")
        return "\n".join(lines)

    def death_timeline(self) -> str:
        """Format death timeline."""
        deaths = [e for e in self.entries if e.type == EntryType.DEATH]
        if not deaths:
            return "暂无死亡记录"
        lines = ["\n## 死亡时间线"]
        for e in deaths:
            lines.append(f"  第{e.round_num}轮: {e.content[:150]}")
        return "\n".join(lines)

    def who_said_what_about(self, target: str, max_n: int = 10) -> str:
        """Find all mentions of a specific player. Like 'search memory'."""
        mentions = []
        for e in self.entries:
            if e.type in (EntryType.SPEECH, EntryType.MY_SPEECH) and target in e.content:
                mentions.append(e)
        if not mentions:
            return f"没有人提到过{target}。"
        lines = [f"\n## 关于{target}的讨论"]
        for e in mentions[-max_n:]:
            lines.append(f"  第{e.round_num}轮 [{e.speaker}]: {e.content[:200]}")
        return "\n".join(lines)

    def full_context_for_prompt(self, max_speeches: int = 25) -> str:
        """Build complete context for a discussion LLM prompt.

        Includes: my status, death timeline, vote history, all speeches by round,
        and what was said about top suspects.
        """
        parts = [
            self.death_timeline(),
            self.vote_history(),
            "\n## 讨论记录（按轮次）",
            self.discussion_by_round(),
        ]
        return "\n".join(parts)


# Agent Persona — unique personality for each agent

@dataclass
class Persona:
    """An agent's unique personality and analysis style."""
    name: str
    style: str            # "analytical", "aggressive", "cautious", "cooperative"
    verbosity: str        # "concise", "normal", "detailed"
    risk_tolerance: str   # "low", "medium", "high"
    description: str      # Natural language description

    def to_prompt(self) -> str:
        return self.description


# Pre-built personas for diversity
PERSONAS = [
    Persona("analytical", "analytical", "detailed", "low",
            "你是分析型玩家。你习惯引用具体的发言内容，用逻辑推理找出矛盾。"
            "你会仔细对比每个人在不同轮次的发言，寻找前后不一致的地方。"),
    Persona("aggressive", "aggressive", "normal", "high",
            "你是激进型玩家。你敢于第一个提出怀疑，不怕得罪人。"
            "你相信早期施压能逼出狼人的破绽。你的发言直接有力。"),
    Persona("cautious", "cautious", "concise", "low",
            "你是谨慎型玩家。你不急于表态，先听完整轮发言再下结论。"
            "你的发言简短但精准，不浪费字数。"),
    Persona("leader", "analytical", "detailed", "medium",
            "你是领袖型玩家。你喜欢总结大家的观点、提出投票方案、带领讨论方向。"
            "你有气场，说话有说服力，好人阵营愿意跟随你。"),
    Persona("skeptical", "aggressive", "normal", "high",
            "你是质疑型玩家。你对每个人的发言都持怀疑态度，不断追问细节。"
            "你擅长发现逻辑漏洞，但有时会过度怀疑导致好人互打。"),
    Persona("peacemaker", "cautious", "normal", "low",
            "你是调和型玩家。当讨论陷入僵局时，你尝试找到共识。"
            "你注重团队协作，但也因此有时显得不够果断。"),
    Persona("observer", "cautious", "detailed", "low",
            "你是观察型玩家。你默默地记录每个人的发言和投票，在关键时刻给出精准分析。"
            "你的发言可能不多，但每次都有实质内容。"),
    Persona("bluffer", "aggressive", "normal", "high",
            "你是诈唬型玩家。你有时故意说一些有误导性的话来测试其他人的反应。"
            "你善于伪装，无论是好人还是狼人，你都能扮演好自己的角色。"),
    Persona("detective", "analytical", "detailed", "medium",
            "你是侦探型玩家。你把狼人杀当作推理游戏，建立假设、收集证据、验证猜想。"
            "你的分析条理清晰，像在破案。"),
]


# ══════════════════════════════════════════════════════════════════
# Speech Summary — compressed memory from raw speeches
# ══════════════════════════════════════════════════════════════════

@dataclass
class SpeechSummary:
    """Structured facts extracted from a single speech. Much more compact
    than raw text — typically 50-100 chars vs 200+ chars raw."""
    speaker: str
    round_num: int
    accusations: List[str] = field(default_factory=list)   # "PlayerX" — suspects as wolf
    defenses: List[str] = field(default_factory=list)       # "PlayerX" — trusts/defends
    claims: List[str] = field(default_factory=list)         # "自称预言家" — about SELF
    reported: List[str] = field(default_factory=list)       # "PlayerX自称预言家" — about OTHERS (may be false!)
    vote_hint: str = ""
    key_point: str = ""


def extract_speech_facts(speaker: str, content: str) -> SpeechSummary:
    """Extract key facts from a speech using lightweight pattern matching.

    No extra LLM call — uses regex patterns for speed. Covers ~80% of cases.
    For the remaining 20%, stores the first sentence as key_point.
    """
    import re
    # Normalize Chinese player references to PlayerN format
    content = re.sub(r'(\d+)号玩家', r'Player\1', content)
    content = re.sub(r'(?<!\w)(\d+)号(?!玩家)', r'Player\1', content)
    # Normalize "结果为【女巫】" or "结果是女巫" → "是女巫"
    content = re.sub(r'结果\s*(?:是|为)\s*[：:【\[\s]*\s*(预言家|女巫|猎人|村民|狼人)\s*[】\]\s]*', r'是\1', content)
    content = re.sub(r'身份\s*(?:是|为)\s*[：:【\[\s]*\s*(预言家|女巫|猎人|村民|狼人)\s*[】\]\s]*', r'是\1', content)
    s = SpeechSummary(speaker=speaker, round_num=0)

    # Accusations: "怀疑PlayerX", "PlayerX很可疑", "PlayerX是狼"
    for m in re.finditer(
        r'(?:怀疑|投(?:票)?给|打算投|会投|投死|觉得|认为)\s*(Player\d)\s*(?:是狼|可疑|有问题|像狼|很怪|不对劲)?',
        content
    ):
        target = m.group(1)
        if target != speaker:
            s.accusations.append(target)

    # Defenses: "PlayerX是好人", "相信PlayerX", "信任PlayerX", "PlayerX没问题"
    for m in re.finditer(
        r'(Player\d)\s*(?:是好人|没问题|可以信|值得信|是村民|是预言家|是女巫|是猎人)',
        content
    ):
        target = m.group(1)
        if target != speaker:
            s.defenses.append(target)
    for m in re.finditer(r'(?:相信|信任|保)\s*(Player\d)', content):
        target = m.group(1)
        if target != speaker and target not in s.defenses:
            s.defenses.append(target)

    # Self role claims: "我是预言家", "我跳预言家"
    for role_word, role_name in [("预言家", "预言家"), ("seer", "预言家"),
                                  ("女巫", "女巫"), ("witch", "女巫"),
                                  ("猎人", "猎人"), ("hunter", "猎人")]:
        if re.search(rf'(?:我是|我跳|我是真){role_word}', content):
            s.claims.append(f"自称{role_name}")
            break

    # Reported claims about OTHERS: "PlayerX自称预言家", "PlayerX跳了预言家"
    # These may be FALSE — the speaker could be lying or mistaken
    for m in re.finditer(
        r'(Player\d)[，,\s]*(?:自称|声称|跳了?|说自己是|是|身份是|结果为|结果是)\s*(预言家|女巫|猎人|村民|狼人)',
        content
    ):
        target = m.group(1)
        if target != speaker:
            s.reported.append(f"{target}自称{m.group(2)}")

    # Vote hint
    vm = re.search(r'(?:投票|投|出)\s*(Player\d)', content)
    if vm:
        s.vote_hint = vm.group(1)

    # Key point: first meaningful sentence
    sentences = re.split(r'[。！？.!?]', content)
    for sent in sentences:
        sent = sent.strip()
        if len(sent) > 15:
            s.key_point = sent[:100]
            break
    if not s.key_point:
        s.key_point = content[:100]

    return s


def format_speech_summaries(summaries: List[SpeechSummary], max_per_round: int = 8) -> str:
    """Format speech summaries as standalone sentences with clear subject.

    Each fact is a complete sentence like 'Player5自称预言家。Player5怀疑Player2。'
    This prevents the LLM from confusing who said what about whom.
    """
    if not summaries:
        return "（暂无发言记录）"

    lines = []
    for s in summaries[-max_per_round:]:
        facts = []
        if s.claims:
            for c in s.claims:
                facts.append(f"{s.speaker}{c}。")
        if s.accusations:
            for a in s.accusations:
                facts.append(f"{s.speaker}怀疑{a}是狼。")
        if s.defenses:
            for d in s.defenses:
                facts.append(f"{s.speaker}说{d}是好人。")
        if s.reported:
            for r in s.reported:
                facts.append(f"{s.speaker}声称:{r}。（未经证实）")
        if s.vote_hint:
            facts.append(f"{s.speaker}要投票给{s.vote_hint}。")
        if not facts:
            facts.append(f"{s.speaker}: {s.key_point}")
        lines.append(" ".join(facts))
    return "\n".join(lines)


def format_round_summary(summaries: List[SpeechSummary]) -> str:
    """Compact round summary with clear subject-verb-object sentences."""
    if not summaries:
        return ""
    facts = []
    for s in summaries:
        for a in s.accusations:
            facts.append(f"{s.speaker}怀疑{a}")
        for d in s.defenses:
            facts.append(f"{s.speaker}信{d}")
        for c in s.claims:
            facts.append(f"{s.speaker}{c}")
        for r in s.reported:
            facts.append(f"{s.speaker}称{r}")
        if s.vote_hint:
            facts.append(f"{s.speaker}要投{s.vote_hint}")
    return "；".join(facts) if facts else ""
