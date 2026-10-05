# -*- coding: utf-8 -*-
"""PlayerAgent for Werewolf Game — Layered Cognitive Architecture with Memory.

Architecture:

   Perception ──→ Memory ──→ Reasoning ──→ Action ──→ Reflection
   (event       (working+  (strategy+    (vote/      (post-game
    parsing)     episodic+  ToM+         night/       review)
                 semantic)  speech)      discuss)

Key features:
  - FULL discussion history in each prompt (all rounds)
  - Unique persona per agent (9 different styles)
  - Memory retrieval: "what did X say about Y?"
  - Post-game reflection with LLM self-review
"""
import asyncio, os, re, random
from typing import Dict, List, Any

from _vendor import (
    Msg, ReActAgentBase, OpenAIChatModel, DeepSeekMultiAgentFormatter,
)

# Web UI event emission (no-op if web_ui not loaded)
try:
    from web_ui.server import emit_event as _emit_web
except ImportError:
    _emit_web = lambda t, d: None

from reasoning import (
    GameEvent, EventType,
    WorkingMemory, BeliefTracker,
)
from memory import (
    MemoryEntry, EntryType,
    EpisodicMemory, Persona, PERSONAS,
    SpeechSummary, extract_speech_facts,
    format_speech_summaries, format_round_summary,
)
import config

StateDictType = Dict[str, Any]


class PlayerAgent(ReActAgentBase):
    """Werewolf agent with full cognitive architecture.

    Layers:
      1. Perception  — raw messages → structured GameEvents → MemoryEntry
      2. Memory      — WorkingMemory + EpisodicMemory + BeliefTracker(ToM)
      3. Reasoning   — strategy plan → full-context speech
      4. Action      — execute plan
      5. Reflection  — post-game LLM self-review
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

        # LLM Backend — task-based routing (not cascading):
        #   - reasoner: discussion speeches (long, stylistic, strategic)
        #   - chat:     votes / night actions (short structured decisions)
        # Research on agentic model routing shows upfront task routing beats
        # per-call escalation cascades, which restart at every agent step.
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")
        self.model = OpenAIChatModel(
            model_name="deepseek-reasoner", api_key=api_key, stream=False,
            client_kwargs={"base_url": "https://api.deepseek.com"},
        )
        self.fast_model = OpenAIChatModel(
            model_name="deepseek-chat", api_key=api_key, stream=False,
            client_kwargs={"base_url": "https://api.deepseek.com"},
        )
        self.formatter = DeepSeekMultiAgentFormatter()

        # Memory Layer
        self.wm = WorkingMemory()
        self.memory = EpisodicMemory(name)
        self.bt: BeliefTracker | None = None
        self._speech_summaries: List[SpeechSummary] = []  # Compressed memory

        # Persona (randomly assigned, unique per agent)
        self.persona: Persona = random.choice(PERSONAS)

        # Legacy state (game.py compatibility).
        # NOTE: no "name_to_role" here on purpose — agents must never hold the
        # ground-truth role map. Werewolf teammates arrive via TEAM_INFO only.
        self.state: Dict[str, Any] = {
            "role": None, "camp": None, "game_phase": None,
            "is_alive": True,
            "current_alive": [], "dead_players": [],
            "night_killed": None, "witch_healing_used": False,
            "witch_poison_used": False, "seer_checked": {},
            "round_discussions": [], "round_votes": [],
            "my_speeches": [], "suspected_werewolves": [],
        }

        self._parsed_msg_ids: set = set()

    # Public API

    def reset_subscribers(self, a, b=None) -> None:
        if b is None:
            self._subscribers[self.name] = [_ for _ in a if _ != self]
        else:
            self._subscribers[a] = [_ for _ in b if _ != self]

    def remove_subscribers(self, name: str) -> None:
        self._subscribers.pop(name, None)

    async def __call__(
        self, msg: Msg | None = None, *, structured_model: Any = None, **kw
    ) -> Msg:
        if not self._ready():
            return Msg(name=self.name, content="初始化中...", role="assistant")
        if msg is not None:
            await self._perceive(msg)
        plan = await self._reason(structured_model)
        return await self._act(plan, structured_model)

    async def reply(self, msg: Msg) -> Msg:
        return await self.__call__(msg)

    async def observe(self, msg: Msg | List[Msg] | None) -> None:
        if not msg:
            return
        for m in ([msg] if isinstance(msg, Msg) else msg):
            if isinstance(m, Msg):
                await self._perceive(m)

    # LAYER 1: Perception → structured GameEvent → MemoryEntry

    async def _perceive(self, msg: Msg) -> None:
        mid = getattr(msg, "id", id(msg))
        if mid in self._parsed_msg_ids:
            return
        self._parsed_msg_ids.add(mid)

        content = (msg.content or "").strip()
        sender = msg.name
        event = self._classify(content, sender)
        if event:
            event.round_num = max(self.wm.round_num, 1)
            # Update working memory + beliefs
            self._update_wm(event)
            # Store as MemoryEntry for rich retrieval
            self._store_memory(event, sender, content)
            self._sync_legacy()

    def _classify(self, content: str, sender: str) -> GameEvent | None:
        """Classify raw message into structured GameEvent."""
        if not content:
            return None
        # Role assignment
        if "ONLY]" in content and self.name in content and "your role is" in content:
            role = content.split("your role is")[-1].strip().strip(".")
            return GameEvent(EventType.ROLE_ASSIGNED, speaker="Moderator",
                           target=self.name, metadata={"role": role})
        # Werewolf teammate notification (private channel)
        if "你的狼队友是" in content or "your werewolf teammates are" in content.lower():
            mates = re.findall(r'(Player\d+)', content.split("狼队友是")[-1]
                               if "狼队友是" in content else content)
            return GameEvent(EventType.TEAM_INFO, speaker="Moderator",
                           target=self.name, metadata={"teammates": mates})
        # Phase changes
        if "天黑了" in content or "Night has fallen" in content:
            return GameEvent(EventType.PHASE_CHANGE, speaker="Moderator",
                           metadata={"phase": "night"})
        if "投票淘汰" in content or "vote to eliminate" in content:
            return GameEvent(EventType.PHASE_CHANGE, speaker="Moderator",
                           metadata={"phase": "voting"})
        if "轮到你发言" in content or "开始讨论" in content:
            return GameEvent(EventType.PHASE_CHANGE, speaker="Moderator",
                           metadata={"phase": "discussion"})
        if "天亮了" in content:
            return GameEvent(EventType.PHASE_CHANGE, speaker="Moderator",
                           metadata={"phase": "day"})
        # Player list
        if "存活玩家有" in content or "alive players are" in content.lower():
            return self._parse_player_list(content)
        # Death
        if "被淘汰的玩家有" in content or "has been eliminated" in content:
            return self._parse_death(content)
        if "平安夜" in content or "peaceful" in content:
            return GameEvent(EventType.PEACEFUL_NIGHT, speaker="Moderator")
        # Witch info
        # Witch info: English "tonight X is eliminated" or Chinese "今晚X被淘汰"
        if ("witch" in content.lower() and "tonight" in content and "eliminated" in content) \
                or ("你是女巫" in content and "今晚" in content and "被淘汰" in content):
            m = re.search(r"tonight (\w+) is eliminated", content)
            if not m:
                m = re.search(r"今晚(\w+)被淘汰", content)
            if m:
                return GameEvent(EventType.WITCH_INFO, speaker="Moderator", target=m.group(1))
        if "WERE ATTACKED AND KILLED" in content or "你被狼人袭击了" in content:
            return GameEvent(EventType.WITCH_INFO, speaker="Moderator",
                           target=self.name, metadata={"self_attacked": True})
        # Seer result
        if "你查验了" in content or "You've checked" in content:
            return self._parse_seer_result(content)
        # Hunter prompt
        if "你是猎人" in content and "被淘汰" in content:
            return GameEvent(EventType.HUNTER_PROMPT, speaker="Moderator", target=self.name)
        # Self eliminated
        if self.name in content and "你已被淘汰" in content:
            return GameEvent(EventType.PLAYER_ELIMINATED, speaker="Moderator", target=self.name)
        # Game over
        if "游戏结束" in content or "werewolves win" in content or "villagers win" in content:
            return GameEvent(EventType.GAME_OVER, speaker="Moderator",
                           metadata={"result": content[:200]})
        # Speech
        if sender != "Moderator" and content and len(content) > 20:
            if not re.match(r'^\w+\s+(chooses|passes|kill|check|shoot|resurrect)', content):
                return GameEvent(EventType.SPEECH, speaker=sender, content=content)
        # Vote result
        if "投票结果为" in content:
            return GameEvent(EventType.VOTE_RESULT, speaker="Moderator", content=content[:200])
        # Full vote detail: "投票明细: Player1→Player7, Player2→Player1, ..."
        if "投票明细" in content:
            pairs = re.findall(r'(Player\d+)\s*[→\->]+\s*(Player\d+)', content)
            if pairs:
                return GameEvent(EventType.VOTE_DETAIL, speaker="Moderator",
                               metadata={"votes": {v: t for v, t in pairs}})
        return None

    def _parse_player_list(self, c: str) -> GameEvent:
        sep = "are" if "are" in c else "有"
        s = c.split(sep)[-1].strip()
        names = []
        seen = set()
        for p in s.replace("和", ",").split(","):
            m = re.search(r'(Player\d+)', p.strip())
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                names.append(m.group(1))
        return GameEvent(EventType.PLAYER_LIST, speaker="Moderator",
                        metadata={"alive": names})

    def _parse_death(self, c: str) -> GameEvent:
        sep = ":" if ":" in c else "有"
        names = []
        for p in c.split(sep)[-1].replace("和", ",").split(","):
            m = re.search(r'(Player\d+)', p.strip())
            if m:
                names.append(m.group(1))
        return GameEvent(EventType.NIGHT_DEATH, speaker="Moderator",
                        metadata={"players": names})

    def _parse_seer_result(self, c: str) -> GameEvent:
        m = re.search(r'查验了(\w+).*身份是(\w+)', c)
        if not m:
            m = re.search(r"checked (\w+).*result is: (\w+)", c)
        if m:
            return GameEvent(EventType.SEER_RESULT, speaker="Moderator",
                           target=m.group(1), metadata={"role": m.group(2)})
        return GameEvent(EventType.SEER_RESULT, speaker="Moderator", content=c)

    # LAYER 2: Memory — update WM + store in EpisodicMemory

    def _update_wm(self, event: GameEvent) -> None:
        wm = self.wm
        et = event.type

        if et == EventType.ROLE_ASSIGNED:
            role = event.metadata["role"]
            wm.my_role = role
            wm.my_camp = "werewolf" if role == "werewolf" else "villager"
            wm.round_num = 0
            all_p = [f"Player{i}" for i in range(1, 10)]
            self.bt = BeliefTracker(self.name)
            self.bt.initialize(all_p, role)

        elif et == EventType.TEAM_INFO:
            wm.my_teammates = list(event.metadata.get("teammates", []))
            if self.bt:
                self.bt.mark_teammates(wm.my_teammates)

        elif et == EventType.PHASE_CHANGE:
            wm.phase = event.metadata["phase"]
            if event.metadata["phase"] == "night":
                wm.round_num += 1  # Everyone increments on nightfall

        elif et == EventType.PLAYER_LIST:
            wm.alive_players = event.metadata["alive"]

        elif et == EventType.NIGHT_DEATH:
            for p in event.metadata["players"]:
                if p not in wm.dead_players:
                    wm.dead_players.append(p)
                if p in wm.alive_players:
                    wm.alive_players.remove(p)
            wm.night_killed = event.metadata["players"][0] if event.metadata["players"] else ""
            if self.bt:
                # Only roles THIS agent legitimately knows: itself, its seer
                # checks, and wolf teammates. Never the ground-truth role map.
                known_roles = {self.name: wm.my_role, **wm.seer_checks}
                for mate in wm.my_teammates:
                    known_roles[mate] = "werewolf"
                for p in event.metadata["players"]:
                    self.bt.observe_night_death(
                        p, known_roles,
                        wm.alive_players, wm.round_num)

        elif et == EventType.PLAYER_ELIMINATED:
            if event.target not in wm.dead_players:
                wm.dead_players.append(event.target)
            if event.target in wm.alive_players:
                wm.alive_players.remove(event.target)

        elif et == EventType.WITCH_INFO:
            wm.night_killed = event.target

        elif et == EventType.SEER_RESULT:
            if event.target and "role" in event.metadata:
                wm.seer_checks[event.target] = event.metadata["role"]

        elif et == EventType.HUNTER_PROMPT:
            # Hunter is killed at night — mark as dead so _reason routes correctly
            if event.target not in wm.dead_players:
                wm.dead_players.append(event.target)
            if event.target in wm.alive_players:
                wm.alive_players.remove(event.target)

        elif et == EventType.SPEECH:
            if self.bt and event.speaker != self.name:
                is_alive = event.speaker in wm.alive_players
                self.bt.observe_speech(event.speaker, event.content, is_alive, wm.round_num)

        elif et == EventType.VOTE_DETAIL:
            # Feed the full voter→target map into the belief tracker so
            # bandwagon / lone-vote evidence is actually recorded.
            if self.bt:
                votes = event.metadata.get("votes", {})
                from collections import Counter
                counts = dict(Counter(votes.values()))
                for voter, target in votes.items():
                    self.bt.observe_vote(voter, target, counts, wm.round_num)

        elif et == EventType.GAME_OVER:
            wm.phase = "game_over"

    def _store_memory(self, event: GameEvent, sender: str, content: str) -> None:
        """Convert GameEvent → MemoryEntry and store for later retrieval."""
        r = max(self.wm.round_num, 1)

        if event.type == EventType.SPEECH:
            if sender == self.name:
                entry = MemoryEntry(EntryType.MY_SPEECH, r, sender, content, importance=3,
                                    metadata={"role": self.wm.my_role})
            else:
                importance = 2
                # Boost importance if speech mentions me or key game terms
                if self.name in content:
                    importance = 4
                elif any(w in content for w in ["怀疑", "预言家", "狼人", "查杀"]):
                    importance = 3
                entry = MemoryEntry(EntryType.SPEECH, r, sender, content,
                                    importance=importance,
                                    metadata={"is_alive": sender in self.wm.alive_players})
                # Extract compressed summary for efficient memory
                summary = extract_speech_facts(sender, content)
                summary.round_num = r
                self._speech_summaries.append(summary)
                # Feed role claims into the belief tracker
                if self.bt and summary.claims:
                    claim_map = {"自称预言家": "seer", "自称女巫": "witch", "自称猎人": "hunter"}
                    for c in summary.claims:
                        if c in claim_map:
                            self.bt.observe_role_claim(sender, claim_map[c], r)

        elif event.type in (EventType.NIGHT_DEATH, EventType.PLAYER_ELIMINATED):
            entry = MemoryEntry(EntryType.DEATH, r, "Moderator", content, importance=5,
                               metadata={"players": event.metadata.get("players", [])})

        elif event.type == EventType.VOTE_RESULT:
            entry = MemoryEntry(EntryType.VOTE, r, "Moderator", content, importance=4)

        elif event.type == EventType.ROLE_ASSIGNED:
            entry = MemoryEntry(EntryType.SYSTEM, r, "Moderator",
                               f"你的身份是{event.metadata['role']}", importance=5)

        elif event.type == EventType.SEER_RESULT:
            ck = f"查验{event.target}，身份是{event.metadata.get('role','?')}"
            entry = MemoryEntry(EntryType.REVEAL, r, "Moderator", ck, importance=5)

        elif event.type == EventType.GAME_OVER:
            entry = MemoryEntry(EntryType.SYSTEM, r, "Moderator", content, importance=5)

        else:
            entry = MemoryEntry(EntryType.SYSTEM, r, sender, content[:200], importance=1)

        self.memory.add(entry)

    def _sync_legacy(self) -> None:
        s, wm = self.state, self.wm
        s["role"] = wm.my_role or s.get("role")
        s["camp"] = wm.my_camp or s.get("camp")
        s["game_phase"] = wm.phase or s.get("game_phase")
        s["is_alive"] = self.name in wm.alive_players if wm.alive_players else True
        s["current_alive"] = list(wm.alive_players)
        s["dead_players"] = list(wm.dead_players)
        s["night_killed"] = wm.night_killed
        s["witch_healing_used"] = wm.healing_used
        s["witch_poison_used"] = wm.poison_used
        if wm.seer_checks:
            s["seer_checked"] = dict(wm.seer_checks)
        s["my_speeches"] = self.memory.my_speeches()
        s["round_discussions"] = [
            (e.speaker, e.content) for e in self.memory.entries
            if e.type in (EntryType.SPEECH, EntryType.MY_SPEECH, EntryType.REFLECTION)
        ]

    # LAYER 3: Reasoning — personalized prompts + full context

    async def _reason(self, structured_model: Any = None) -> Dict[str, Any]:
        wm = self.wm
        if not wm.my_role or not wm.phase:
            return self._empty()

        is_dead = self.name not in wm.alive_players and wm.round_num > 0

        # Dead player actions (hunter shot) take priority over game_over
        if is_dead and structured_model is not None:
            return await self._decide("hunter_shot")

        # Game over — reflection disabled (not yet useful for learning)
        if wm.phase == "game_over":
            return self._empty()

        if is_dead:
            return await self._discuss(is_last_words=True)

        if wm.phase == "night":
            # Only acting roles make night decisions
            if wm.my_role in ("werewolf", "seer", "witch"):
                return await self._decide("night")
            return self._empty()
        if wm.phase == "discussion":
            return await self._discuss()
        if wm.phase == "voting":
            return await self._decide("vote")

        return self._empty()

    async def _decide(self, mode: str) -> Dict[str, Any]:
        """LLM tactical decision — first reasons, then decides."""
        # For vote mode: use stated target from discussion speech if available
        if mode == "vote" and hasattr(self, '_stated_vote') and self._stated_vote:
            stated = self._stated_vote
            alive = self.wm.alive_players
            role = self.wm.my_role
            # Village-camp vote coordination (research basis — LSPO,
            # arXiv:2502.04686): the emergent highest-win-rate villager
            # strategy is a SINGLE seer claimant leading a unified vote on
            # their kill target. A seer claim may surface AFTER my speech,
            # so the stated-vote shortcut must yield to this newer, stronger
            # public evidence. Narrow gate: exactly one claimant, target
            # alive, I am not that seer, I am not a wolf.
            if role != "werewolf" and role != "seer":
                claimants, kills = self._seer_claim_intel()
                if len(claimants) == 1:
                    seer = next(iter(claimants))
                    # Gate: a claimed chain that contradicts the engine death
                    # record is a provable fake — never let it steer the vote.
                    if self._chain_contradictions(seer):
                        print(f"[{self.name}] {seer}查验链与死亡记录矛盾，不跟票")
                    else:
                        targets = [t for t in kills.get(seer, [])
                                   if t in alive and t != self.name]
                        if targets and stated != targets[0]:
                            print(f"[{self.name}] 改投唯一预言家{seer}的查杀{targets[0]}（原声明{stated}）")
                            return self._decision_to_plan(mode, targets[0])
            # Validate: must be alive, not self, not wolf teammate
            if stated in alive and stated != self.name:
                mates = self._get_wolf_mates(alive) if role == "werewolf" else []
                if stated not in mates:
                    print(f"[{self.name}] 投票 → {stated} (发言已声明)")
                    return self._decision_to_plan(mode, stated)
                else:
                    print(f"[{self.name}] 发言声明投队友{stated}，改LLM决定")

        prompt = self._build_decision_prompt(mode)
        text = await self._llm(prompt, max_t=600, temp=0.7, fast=True)
        # Split reasoning and decision
        reasoning, decision = self._split_reasoning(text)
        if reasoning:
            # Filter wolf self-reveal in console output
            clean = reasoning
            for phrase in ["作为狼人", "我是狼人", "我们狼人", "狼队友", "作为一只狼"]:
                clean = clean.replace(phrase, "基于局势")
            if len(clean) > 200:
                clean = clean[:200]
            print(f"[{self.name}] 推理: {clean}")
        target = self._parse_target(decision, mode)
        return self._decision_to_plan(mode, target)

    @staticmethod
    def _split_reasoning(text: str) -> tuple:
        """Split LLM output into (reasoning, decision_line)."""
        text = text.strip()
        # Try "决策: X" format
        m = re.search(r'决策\s*[:：]\s*(.+)', text, re.IGNORECASE)
        if m:
            decision = m.group(1).strip()
            reasoning = text[:m.start()].strip()
            # Clean up "推理:" prefix
            reasoning = re.sub(r'^推理\s*[:：]\s*', '', reasoning).strip()
            return reasoning, decision
        # Fallback: last line is decision
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        if len(lines) >= 2:
            return '\n'.join(lines[:-1]), lines[-1]
        return '', text

    async def _discuss(self, is_last_words: bool = False) -> Dict[str, Any]:
        """Two-pass discussion: internal reasoning → public speech."""
        prompt = self._build_discussion_prompt(is_last_words)
        # deepseek-reasoner counts chain-of-thought against max_tokens —
        # 1500 was frequently exhausted by the CoT, returning EMPTY content
        # (~20% of speeches fell back in observed games). Give the CoT room.
        text = await self._llm(prompt, max_t=4000, temp=0.9)
        if not text or len(text) < 10:
            print(f"[{self.name}] WARNING: LLM empty, using fallback")
            return self._fallback_plan(is_last_words=is_last_words)

        # Split internal reasoning from public speech
        internal, public = self._split_internal_public(text)
        if internal:
            print(f"[{self.name}] 内部: {internal[:150]}")

        # Extract stated vote target from RAW speech (before cleaning/truncation).
        # Research basis (arXiv:2606.00476): machine-checkable explicit commitments
        # cut conclusion→action inconsistency from ~25% to ~2%. Priority:
        #   1. Explicit "投票意向: PlayerX" line (last one wins)
        #   2. Last FIRST-PERSON declaration ("我投X" / "本轮我投票给X")
        # Never extract from descriptions of OTHER players' votes.
        self._stated_vote = self._extract_stated_vote(public)

        public = self._clean_text(public)
        # Keep the commitment line if truncation cut it off
        if self._stated_vote and "投票意向" not in public:
            public = public.rstrip("。！？!?.") + f"\n投票意向: {self._stated_vote}"
        print(f"[{self.name}] {public}")
        _emit_web("speech", {"player": self.name, "content": public})

        self.memory.add(MemoryEntry(
            EntryType.MY_SPEECH, self.wm.round_num, self.name, public,
            importance=3, metadata={"role": self.wm.my_role}
        ))

        suspect = self._extract_suspect(public)
        return {"speech": public, "suspect": suspect, "strategy": "discuss",
                "vote_plan": suspect}

    @staticmethod
    def _split_internal_public(text: str) -> tuple:
        """Split '内部: (reasoning)\n公开: (speech)' into (internal, public)."""
        text = text.strip()
        m = re.search(r'公开\s*[:：]\s*(.+)', text, re.DOTALL | re.IGNORECASE)
        if m:
            public = m.group(1).strip()
            internal_m = re.search(r'内部\s*[:：]\s*(.+?)(?=公开\s*[:：]|$)', text, re.DOTALL)
            internal = internal_m.group(1).strip() if internal_m else ""
            return internal, public
        # Fallback: first line is internal, rest is public
        lines = text.split('\n')
        if len(lines) >= 2:
            return lines[0], '\n'.join(lines[1:])
        return '', text

    @staticmethod
    def _extract_stated_vote(speech: str) -> str | None:
        """Extract MY declared vote target from my own speech.

        Only matches explicit commitments, never descriptions of others:
          1. "投票意向: PlayerX" machine-checkable line (last occurrence)
          2. First-person declaration, e.g. "我投Player3" / "本轮我投票给Player3"
             (last occurrence — declarations usually come at the end)
        """
        if not speech:
            return None
        m = re.findall(r'投票意向\s*[:：]?\s*(Player\d+)', speech)
        if m:
            return m[-1]
        m = re.findall(
            r'(?:我|本轮|今天|这轮)[^。！？!?\n]{0,12}?(?:投(?:票|死|出)?|票)(?:给|向)?\s*(Player\d+)',
            speech,
        )
        if m:
            return m[-1]
        return None

    # Prompt Builders — personalized + full context

    def _build_decision_prompt(self, mode: str) -> str:
        """Decision prompt — compact, critical info first, format last."""
        wm = self.wm
        alive = wm.alive_players
        role = wm.my_role
        camp_label = "狼人" if wm.my_camp == "werewolf" else "好人"
        n_alive = wm.n_alive
        max_wolves = min(3, (n_alive - 1) // 2)

        # ── Line 1: Identity (MUST be read first) ──
        identity = f"你是{self.name}，身份{role}({camp_label})。存活{n_alive}人，最多{max_wolves}狼。"

        # ── Forbidden rules (compact, at top) ──
        forbid = []
        if wm.dead_players:
            forbid.append(f"禁止投已死玩家: {', '.join(wm.dead_players)}")
        if role == "werewolf":
            mates = self._get_wolf_mates(alive)
            if mates:
                forbid.append(f"禁止杀/投狼队友: {', '.join(mates)}")
        if role == "seer" and mode == "night":
            already = [p for p in alive if p in wm.seer_checks]
            if already:
                forbid.append(f"禁止重复查验: {', '.join(already)}")
        forbid_block = "\n".join(f"❌ {f}" for f in forbid) if forbid else ""

        # ── Summaries (bounded: current round full, past rounds one line
        # each — keeps prompts lean as games lengthen; context rot research:
        # Anthropic "Effective context engineering for AI agents", 2025) ──
        cur_sum = [s for s in self._speech_summaries
                   if s.round_num == wm.round_num]
        past_sum = [s for s in self._speech_summaries
                    if s.round_num < wm.round_num]
        sum_parts = []
        if past_sum:
            by_round: Dict[int, list] = {}
            for s in past_sum:
                by_round.setdefault(s.round_num, []).append(s)
            for r in sorted(by_round):
                line = format_round_summary(by_round[r])
                if line:
                    sum_parts.append(f"第{r}轮摘要: {line}")
        if cur_sum:
            sum_parts.append(
                "本轮发言:\n" + format_speech_summaries(cur_sum, max_per_round=12)
            )
        summaries = "\n".join(sum_parts) if sum_parts else "暂无讨论"

        # ── Body (mode-specific) ──
        if mode == "vote":
            my_speeches = self.memory.my_speeches()
            last_speech = my_speeches[-1] if my_speeches else ""
            my_speech_block = f"你发言说过: 「{last_speech[:150]}」\n" if last_speech else ""
            seer_intel = self._seer_claim_block() if role != "werewolf" else ""
            seer_intel_block = f"{seer_intel}\n" if seer_intel else ""
            # R1 abstain hint (config-gated): the structured vote model
            # accepts "abstain" in round 1 (config.ALLOW_R1_ABSTAIN) — tell
            # the agent here so its decision can use it deliberately.
            if role != "werewolf" and self.wm.round_num <= 1 and config.ALLOW_R1_ABSTAIN:
                seer_intel_block += (
                    "ℹ️ 第1轮弃票规则: 第1天无查验、无证据时，你可以投 abstain（弃票）。"
                    "有明确证据就正常投。无证据弃票是保留信息的合理选择。\n"
                )
            ledger = self._ledger_block()
            ledger_block = f"\n{ledger}\n" if ledger else ""
            body = (
                f"存活玩家: {', '.join(alive)}\n"
                f"{my_speech_block}"
                f"{seer_intel_block}"
                f"❌ 禁止投自己！禁止投已死玩家！\n"
                f"投票必须与发言一致！发言说投谁就投谁。\n"
                f"（例外：发言后若唯一预言家报了查杀，跟票查杀对象是好人最高胜率策略，优于你的次要怀疑。）\n"
                f"讨论摘要:\n{summaries}\n"
                f"{ledger_block}"
            )
        elif mode == "night":
            body = f"{self._night_role_body()}"
        elif mode == "hunter_shot":
            body = f"存活: {', '.join(alive)}"
        else:
            body = summaries

        # ── Format ──
        fmt = "推理: (一句话)\n决策: [玩家名]"
        if role == "witch" and mode == "night":
            fmt = "推理: (一句话)\n决策: resurrect 或 poison:玩家名 或 resurrect+poison:玩家名 或 none"

        return (
            f"{identity}\n"
            + (f"{forbid_block}\n" if forbid_block else "")
            + f"\n{body}\n\n"
            f"{fmt}"
        )

    def _get_wolf_mates(self, alive: list) -> list:
        """Get alive wolf teammates (excluding self).

        Teammates come from the private TEAM_INFO channel (wm.my_teammates),
        never from a ground-truth role map held locally.
        """
        return [m for m in self.wm.my_teammates
                if m in alive and m != self.name]

    def _seer_claim_intel(self) -> tuple:
        """Scan speech summaries for seer claims and their kill targets.

        Returns (claimants, kills):
          claimants — set of alive speakers who publicly claimed 预言家
          kills     — {claimant: [alive players they accused as werewolf]}

        Used for village-side vote coordination. Research basis:
        - LSPO (arXiv:2502.04686): the emergent highest-win-rate villager
          strategy is a single seer claimant coordinating a unified vote.
        - Werewolf Arena (arXiv:2407.13943): believability of the claim
          (check-chain detail) decides whether coordination helps or
          backfires — hence the caller must compare claimants when >1.
        Dead claimants are dropped: their info was already acted on or died
        with them, and "following" a dead claimant's target is exploitable.
        """
        alive = self.wm.alive_players
        claimants = set()
        kills: Dict[str, list] = {}
        for s in self._speech_summaries:
            if s.speaker not in alive or s.speaker == self.name:
                continue
            if any("预言家" in c for c in s.claims):
                claimants.add(s.speaker)
        for c in claimants:
            targets = []
            for s in self._speech_summaries:
                if s.speaker != c:
                    continue
                for a in s.accusations:
                    if a in alive and a != self.name and a not in targets:
                        targets.append(a)
            kills[c] = targets
        return claimants, kills

    def _chain_contradictions(self, claimant: str) -> list:
        """Cross-validate a seer claimant's claimed check chain against the
        engine-pushed death timeline in WorkingMemory.public_ledger.

        Deterministic, LLM-free: a claim of "第R轮查PlayerX" where PlayerX
        was already dead BEFORE night R (died in any round d < R) is a
        provable impossibility — the real seer checks living players only.
        Conservative by design: deaths in the SAME round as the claimed
        check (d == R) are never flagged, because a night-R check precedes
        the day-R lynch and may coincide with the night-R wolf kill.

        Research basis: Werewolf Arena (arXiv:2407.13943) — check-chain
        detail is THE discriminator villagers use between competing seer
        claims; a fake chain is checkable against the public record, the
        same grounding principle as the ledger (arXiv:2409.01575: state
        lives in the rule layer, LLM claims are verified against it).

        Returns a list of human-readable impossibility strings (empty =
        chain consistent with the record, or no dated checks found).
        """
        wm = self.wm
        if not wm.public_ledger:
            return []
        # Player → round they died in (first death wins)
        died_at: Dict[str, int] = {}
        for e in wm.public_ledger:
            for p in (e.get("night_deaths") or []):
                died_at.setdefault(p, e["round"])
            if e.get("lynched"):
                died_at.setdefault(e["lynched"], e["round"])
            for p in (e.get("day_deaths") or []):
                died_at.setdefault(p, e["round"])
        # Claimant's own speeches (full text kept in memory entries)
        texts = [
            e.content for e in self.memory.entries
            if e.speaker == claimant
            and e.type in (EntryType.SPEECH, EntryType.MY_SPEECH)
        ]
        bad = []
        for t in texts:
            # "第R轮查(验)PlayerX" — with round dating
            for m in re.finditer(r'第\s*(\d+)\s*轮[^。！？!?\n]{0,15}?查(?:验)?[^。！？!?\n]{0,8}?(Player\d+)', t):
                r = int(m.group(1))
                tgt = m.group(2)
                d = died_at.get(tgt)
                if d is not None and d < r:
                    bad.append(f"{claimant}声称第{r}轮查验{tgt}，但{tgt}第{d}轮已出局——对死人查验是不可能的，该查验链系编造")
            # "查(验)PlayerX（第R轮）" — round in trailing parentheses
            for m in re.finditer(r'查(?:验)?\s*(Player\d+)[^。！？!?\n]{0,4}?第\s*(\d+)\s*轮', t):
                tgt = m.group(1)
                r = int(m.group(2))
                d = died_at.get(tgt)
                if d is not None and d < r:
                    bad.append(f"{claimant}声称第{r}轮查验{tgt}，但{tgt}第{d}轮已出局——对死人查验是不可能的，该查验链系编造")
        return bad

    def _seer_claim_block(self) -> str:
        """Dynamic seer-claim intel line for prompts (all camps see claims)."""
        claimants, kills = self._seer_claim_intel()
        if not claimants:
            return ""
        if len(claimants) == 1:
            seer = next(iter(claimants))
            targets = kills.get(seer, [])
            t_str = f"，查杀 {', '.join(targets)}" if targets else "（尚未报查杀）"
            # Deterministic chain×death-record cross-validation
            contra = self._chain_contradictions(seer)
            if contra:
                return (
                    f"【神职信息】{seer} 是目前唯一跳预言家的玩家{t_str}。\n"
                    f"⚠️ 查验链交叉验证（对照【权威记录】死亡时间线，确定性结论）:\n"
                    + "\n".join(f"- {c}" for c in contra)
                    + "\n查验链与公开死亡记录矛盾→TA几乎必是假预言家（狼），不要跟票其查杀对象。"
                )
            return (
                f"【神职信息】{seer} 是目前唯一跳预言家的玩家{t_str}。"
                f"无对跳→声明可信度较高（且已通过死亡记录交叉验证），好人应集中投其查杀对象。"
            )
        detail_parts = []
        contra_map = {}
        for c in sorted(claimants):
            contra = self._chain_contradictions(c)
            contra_map[c] = contra
            tag = " ⚠️查验链与死亡记录矛盾" if contra else ""
            detail_parts.append(f"{c}查杀{', '.join(kills.get(c, [])) or '无'}{tag}")
        detail = "；".join(detail_parts)
        lines = [
            f"【神职信息】{len(claimants)}人对跳预言家: {detail}。",
            f"分辨真伪: 报得出逐轮完整查验链(第R轮查X=结果)的更可信；"
            f"只喊查杀报不出查验链的大概率是狼。",
        ]
        flagged = [c for c in sorted(claimants) if contra_map[c]]
        if flagged:
            lines.append("⚠️ 查验链交叉验证（对照【权威记录】，确定性结论）:")
            for c in flagged:
                for s in contra_map[c]:
                    lines.append(f"- {s}")
            if len(flagged) < len(claimants):
                lines.append(f"查验链与死亡记录矛盾者必是假预言家——跟票 {'、'.join(c for c in sorted(claimants) if not contra_map[c])} 的查杀对象。")
            else:
                lines.append("两人查验链都与死亡记录矛盾——都不可信，勿跟任何一方的查杀。")
        return "\n".join(lines)

    def _ledger_block(self) -> str:
        """Authoritative public record, rendered verbatim from the
        engine-pushed ground truth in WorkingMemory.public_ledger.

        Research basis:
        - CoALA (arXiv:2309.02427): working memory should be a data
          structure written outside the LLM and READ into prompts — not the
          LLM's own recollection of scrolled context.
        - 'Werewolf Agent That does not Truly Trust LLMs'
          (arXiv:2409.01575): game state must live outside the LLM; the LLM
          only verbalizes it. (Eval game 10: an agent "remembered" a vote
          that never happened and lynched the hunter on it.)
        - Lost in the Middle (arXiv:2307.03172): critical facts go near the
          END of the prompt — callers append this block late.

        Also derives said-vs-cast mismatches (stated 投票意向 in speeches vs
        engine-recorded votes) — the classic human 'flip-flop' wolf signal.
        Only the first ballot is compared: re-votes have restricted
        candidates, so flips there are expected.
        """
        wm = self.wm
        if not wm.public_ledger:
            return ""
        lines = [
            "【权威记录】游戏引擎实况，绝对准确。凡引用历史投票/死亡必须照抄此表，"
            "禁止凭记忆复述或推测（记忆会出错，此表不会）。"
        ]
        for e in sorted(wm.public_ledger, key=lambda x: x["round"]):
            seg = [f"第{e['round']}轮"]
            nd = e.get("night_deaths") or []
            seg.append(f"夜死:{','.join(nd) if nd else '无'}")
            votes = e.get("votes") or {}
            if votes:
                seg.append("投票:" + ",".join(f"{v}→{t}" for v, t in votes.items()))
            if e.get("tie"):
                seg.append(f"平票:{'/'.join(e['tie'])}")
            rc = e.get("revote_counts") or {}
            if rc:
                seg.append("重投:" + ",".join(f"{k}×{v}" for k, v in rc.items()))
            if votes or e.get("tie"):
                lynched = e.get("lynched")
                seg.append(f"出局:{lynched}" if lynched else "出局:无")
            extra_day = [p for p in (e.get("day_deaths") or [])
                         if p != e.get("lynched")]
            if extra_day:
                seg.append(f"白天死亡:{','.join(extra_day)}")
            lines.append(" | ".join(seg))
        # Said-vs-cast mismatches
        mismatches = []
        for e in wm.public_ledger:
            votes = e.get("votes") or {}
            rnd = e["round"]
            for voter, target in votes.items():
                if voter == self.name:
                    continue
                stated = None
                for s in self._speech_summaries:
                    if s.speaker == voter and s.round_num == rnd and s.stated_vote:
                        stated = s.stated_vote  # last one wins
                if stated and stated != target:
                    mismatches.append(
                        f"{voter}第{rnd}轮声称投{stated}、实际投{target}"
                    )
        if mismatches:
            lines.append(
                "【言行不一】声称投票≠实际投票"
                "（可能是听完发言后合理改票，需其本人解释；解释不清的才是狼信号）："
            )
            lines.append("；".join(mismatches[-8:]))
        return "\n".join(lines)

    def _night_role_body(self) -> str:
        wm = self.wm
        role = wm.my_role
        alive = wm.alive_players

        if role == "werewolf":
            mates = self._get_wolf_mates(alive)
            dead_mates = [m for m in wm.my_teammates
                         if m in wm.dead_players and m != self.name]
            non_wolves = [p for p in alive if p not in mates and p != self.name]
            mate_str = f"队友: {', '.join(mates)}" if mates else "你没有队友了！"
            dead_str = f"（已死队友: {', '.join(dead_mates)}）" if dead_mates else ""
            return (
                f"【狼人杀人】{mate_str}{dead_str}\n"
                f"🔴 狼队友（禁止杀）: {', '.join(mates)}\n"
                f"🟢 好人（可杀）: {', '.join(non_wolves)}\n"
                f"杀队友=自杀=狼人必输！只从🟢好人中选一个。"
            )
        elif role == "seer":
            ck = wm.seer_checks
            c_str = ", ".join(f"{n}({r})" for n, r in ck.items()) or "无"
            unchecked = [p for p in alive if p not in ck and p != self.name]
            # Also filter dead players from unchecked
            unchecked = [p for p in unchecked if p not in wm.dead_players]
            dead_list = [p for p in wm.dead_players]
            dead_warn = f"\n已死玩家（不可查）: {', '.join(dead_list)}" if dead_list else ""
            already = [p for p in alive if p in ck]
            repeat_warn = ""
            if already:
                repeat_warn = f"\n❌ 已查过: {', '.join(already)} — 禁止重复查验！只能从'未查'列表中选。"
            return (
                f"【预言家查验】已查:{c_str}{repeat_warn}\n"
                f"未查:{', '.join(unchecked) if unchecked else '无'}{dead_warn}\n"
                f"从'未查'列表中回复一个玩家名。"
            )
        elif role == "witch":
            h_ok = not wm.healing_used
            p_ok = not wm.poison_used
            nk = wm.night_killed
            # Night-1 poison hard ban: with zero information, blind poison
            # hits a wolf only 3/8 vs 5/8 a villager — negative EV (prompt
            # discipline alone failed in S09). Enforced again in
            # _parse_target so the LLM cannot bypass it.
            poison_banned = wm.round_num <= 1
            # Build candidate list for poison (all alive except self, and except saved player)
            poison_candidates = [p for p in alive if p != self.name]
            poison_list = ", ".join(poison_candidates) if poison_candidates else "无"
            if poison_banned:
                poison_line = "\n🚫 首夜禁毒（规则）: 第1夜信息量为零，毒药盲毒必是负期望，今晚禁止用毒。"
            else:
                poison_line = f"\n毒药可选: {poison_list}"
            if nk == self.name:
                heal = f"\n你被狼袭击了！不能自救。{poison_line}"
                heal += f"\n回复: none（不用毒）" + ("" if poison_banned else " 或 poison:玩家名")
            elif h_ok and nk:
                heal = f"\n今晚{nk}被杀。建议救TA。{poison_line}"
                heal += f"\n回复: resurrect（救人） 或 none（都不用）" + (
                    "" if poison_banned else "；也可 poison:玩家名 或 resurrect+poison:玩家名（边救边毒）"
                )
            else:
                heal = poison_line
                heal += f"\n回复: none（不用毒）" + ("" if poison_banned else " 或 poison:玩家名")
            strategy = ""
            if h_ok and wm.round_num == 1:
                strategy = "\n策略提示: 第1轮被刀的很可能是好人，强烈建议救！"
            if p_ok and wm.round_num >= 2:
                strategy += "\n策略提示: 毒药还没用，对高度怀疑的玩家使用。"
            # Ground the poison decision in the authoritative record: voting
            # coalitions and death timeline are the witch's ONLY evidence for
            # a poison call — without them she was poisoning on vibes (S09:
            # night-1 blind poison killed a villager, village lost in 1 round).
            ledger = self._ledger_block()
            ledger_block = f"\n{ledger}\n" if ledger else ""
            return (
                f"【女巫】解药:{'可用' if h_ok else '已用'} 毒药:{'可用' if p_ok else '已用'}{heal}{strategy}\n"
                f"用毒前必读【权威记录】——投票抱团/言行不一是唯一可靠证据，无证据就不用毒:{ledger_block}"
            )
        return "回复none。"

    def _build_discussion_prompt(self, is_last_words: bool = False) -> str:
        """Discussion prompt — critical rules first, context middle, format last."""
        wm = self.wm
        alive = wm.alive_players
        role = wm.my_role
        camp = "狼人" if wm.my_camp == "werewolf" else "好人"
        is_wolf = role == "werewolf"
        n_alive = wm.n_alive
        max_wolves = min(3, (n_alive - 1) // 2)

        # ═══════════════════════════════════════════
        # SECTION 1: IDENTITY + CRITICAL RULES (top — must read)
        # ═══════════════════════════════════════════
        lines = []
        lines.append(f"═══ 你是 {self.name} | 身份 {role}({camp}) | 第{wm.round_num}轮 | 存活{n_alive}人 最多{max_wolves}狼 ═══")

        # Critical prohibitions — one line each, at top
        dead_list = wm.dead_players
        if dead_list:
            lines.append(f"❌ 已死（禁止投/禁止杀/禁止查）: {', '.join(dead_list)}")
        if is_wolf:
            mates = self._get_wolf_mates(alive)
            if mates:
                lines.append(f"❌ 你的狼队友（禁止杀/禁止投）: {', '.join(mates)}")
            lines.append(f"存活狼人: {len(mates)+1}个。杀队友=自杀。")
        if role == "seer":
            ck = wm.seer_checks
            if ck:
                lines.append(f"🔮 你查验过: {', '.join(f'{n}={r}' for n,r in ck.items())}")
        if role == "witch":
            lines.append(f"🧙 解药{'已用' if wm.healing_used else '可用'} | 毒药{'已用' if wm.poison_used else '可用'}")
        if is_last_words:
            lines.append(f"⚠️ 你是{self.name}，已被淘汰，这是遗言！")
        if wm.round_num <= 1:
            lines.append("⚠️ 这是第1轮，没有历史发言。不要编造'前几轮''之前他说过'等内容！")
        lines.append("⚠️ 不要声称别人投了谁——除非【权威记录】里白纸黑字写着。记录里没有的说法就是编造，会被当场拆穿。")
        lines.append("⚠️ 如果有人自称预言家并给出查验结果（'PlayerX是狼人/好人'），不要直接否定说'没有依据'——查验本身就是依据！考虑TA可能是真预言家的可能性。观察有没有人能验证这个说法。")

        # ═══════════════════════════════════════════
        # SECTION 2: CONTEXT (summaries, votes)
        # ═══════════════════════════════════════════
        # Current round speeches before me
        all_summaries = self._speech_summaries
        spoke = []
        seen = set()
        for s in all_summaries:
            if s.speaker != self.name and s.speaker not in seen:
                seen.add(s.speaker)
                spoke.append(s.speaker)
        pos = len(spoke) + 1
        current_round = wm.round_num
        before_me = [s for s in all_summaries if s.round_num == current_round and s.speaker in spoke]
        current_context = format_speech_summaries(before_me) if before_me else "（你是本轮第一个发言）"

        # Past rounds
        past_summaries = [s for s in all_summaries if s.round_num < current_round]
        past_context = ""
        if past_summaries:
            by_round = {}
            for s in past_summaries:
                by_round.setdefault(s.round_num, []).append(s)
            past_lines = []
            for r in sorted(by_round.keys()):
                line = format_round_summary(by_round[r])
                if line:
                    past_lines.append(f"第{r}轮: {line}")
            if past_lines:
                past_context = "\n".join(past_lines[-5:])

        # (The old "【投票记录】" block rendered Moderator→text garbage from
        # VOTE entries and was a confabulation source — replaced by the
        # authoritative _ledger_block(), appended near the end of the prompt
        # per Lost-in-the-Middle placement.)

        # ═══════════════════════════════════════════
        # SECTION 3: STRATEGY + FORMAT (at end — recency)
        # ═══════════════════════════════════════════
        strategy = self._strategy_hint()

        # ═══════════════════════════════════════════
        # VERIFICATION: Cross-check public claims against private knowledge
        # ═══════════════════════════════════════════
        verify_lines = []
        for s in all_summaries:
            if s.speaker == self.name:
                continue
            for rpt in s.reported:
                if self.name in rpt:
                    claim = rpt.replace(self.name, "").strip()
                    if role == "seer" and ("预言家" in claim or "seer" in claim.lower()):
                        verify_lines.append(f"⚠️ {s.speaker}声称{self.name}{claim}。你是真预言家！{s.speaker}在假冒你的身份！必须在发言中揭穿TA！")
                    elif role == "hunter" and ("猎人" in claim or "hunter" in claim.lower()):
                        verify_lines.append(f"✅ {s.speaker}说你是猎人——这是真的！{s.speaker}能准确说出你的身份，说明TA很可能是真预言家或有特殊信息。你应该公开承认并支持TA！")
                    elif role == "witch" and ("女巫" in claim or "witch" in claim.lower()):
                        verify_lines.append(f"✅ {s.speaker}说你是女巫——这是真的！只有真预言家才能查验出你的身份。{s.speaker}就是真预言家！你不用暴露自己是女巫，但必须在发言中说'我相信{s.speaker}是真预言家'，带领好人投票。好人阵营需要领袖！")
                    elif role == "werewolf" and ("狼人" in claim or "werewolf" in claim.lower()):
                        verify_lines.append(f"🔴 {s.speaker}说你是狼人——TA说对了！你必须反驳TA、引导怀疑转向TA或别人。")
                    elif role == "villager" and ("村民" in claim or "villager" in claim.lower()):
                        verify_lines.append(f"✅ {s.speaker}说你是村民——这是真的。TA怎么知道的？如果是预言家查了你，TA值得信任。")
                    else:
                        verify_lines.append(f"❓ {s.speaker}声称{self.name}{claim}。你的真实身份是{role}。判断TA是在说谎还是猜的。")
            for c in s.claims:
                if role == "seer" and "预言家" in c:
                    verify_lines.append(f"⚠️ {s.speaker}{c}！你是真预言家，TA是假的！必须揭穿！")
                if role == "witch" and "女巫" in c:
                    verify_lines.append(f"⚠️ {s.speaker}{c}！你是真女巫，TA是假的！")
                if role == "hunter" and "猎人" in c:
                    verify_lines.append(f"⚠️ {s.speaker}{c}！你是真猎人，TA是假的！")

        # Seer: MUST use check results in speech
        if role == "seer" and wm.seer_checks:
            ck_list = "，".join(f"{n}是{r}" for n, r in wm.seer_checks.items())
            good_list = [n for n, r in wm.seer_checks.items() if r != "werewolf"]
            wolf_list = [n for n, r in wm.seer_checks.items() if r == "werewolf"]
            if wolf_list:
                format_line = (
                    f"🔴 你查到狼人: {', '.join(wolf_list)}！必须跳身份！\n"
                    f"内部: (跳预言家)\n"
                    f"公开: 我是预言家！查验链: {ck_list}。{', '.join(wolf_list)}是铁狼，"
                    f"所有人跟我投{wolf_list[0]}！（报完整查验链自证，才不会被当成假预言家）\n"
                    f"投票意向: {wolf_list[0]}"
                )
            else:
                format_line = (
                    f"你的查验记录: {ck_list}。"
                    + (f" {', '.join(good_list)}是好人，你必须在发言中保护TA、引导投票远离TA！" if good_list else "")
                    + "\n内部: (策略)\n公开: (如果信息足够可跳身份，否则以村民视角保护查验的好人，指出怀疑对象+投谁)\n投票意向: (你要投的玩家，单独一行)"
                )
        elif is_wolf:
            format_line = (
                "内部: (伪装策略一句话)\n"
                "公开: (以村民视角发言，指出怀疑对象+理由+投谁，60-150字)\n"
                "投票意向: (你要投的玩家，单独一行)"
            )
        else:
            format_line = (
                "内部: (反事实自检: 我指控TA依赖的最关键证据是什么？去掉这条证据我还怀疑TA吗？若否→放弃指控)\n"
                "公开: (指出怀疑对象+一条可验证证据[发言矛盾/投票抱团/死亡信息]+投谁，60-150字。"
                "没有证据就分析局势，不要凭'感觉可疑'点名——无证据怀疑=替狼人刀人)\n"
                "投票意向: (你要投的玩家，单独一行)"
            )

        # ── Assemble ──
        parts = [
            "\n".join(lines),
            "",
        ]
        if verify_lines:
            parts.append("【⚠️ 验证：别人说了关于你的事——用你的私人知识判断真假】")
            parts.extend(verify_lines)
            parts.append("")
        if past_context:
            parts.append(f"【历史摘要】\n{past_context}\n")
        parts.append(f"【本轮发言】第{pos}位\n{current_context}")
        seer_intel = self._seer_claim_block()
        if seer_intel:
            parts.append(seer_intel)
        parts.append(f"\n【策略】\n{strategy}")
        # Authoritative ledger AFTER strategy, right before the format
        # section — recency position (Lost in the Middle, arXiv:2307.03172).
        ledger = self._ledger_block()
        if ledger:
            parts.append(f"\n{ledger}")
        # Verification at the VERY END (recency bias — LLM sees this right before output)
        if verify_lines:
            parts.append(f"\n【‼️ 发布前必须检查：有人说了关于你的话，你私下知道真假！】")
            parts.extend(verify_lines)
            parts.append("你在回复中必须处理以上信息。如果是真的→公开确认。如果是假的→公开揭穿。不能沉默！")
        # Dead player warning at the VERY END (can't miss)
        dead_warn_end = ""
        if wm.dead_players:
            dead_warn_end = f"\n⚠️ 已死玩家: {', '.join(wm.dead_players)}。你绝对不能投已死的人！只能投存活玩家。"
        parts.append(
            f"\n【回复格式】\n{format_line}{dead_warn_end}"
            f"\n‼️ 发言最后必须单独一行写「投票意向: PlayerX」（X为存活玩家、不能是自己）。"
            f"投票环节将以此为准，必须与你的发言一致。"
            f"引用历史投票或死亡信息时，必须照抄【权威记录】，一个字都不许改。"
            f"\n‼️ 上面的规则是给你的分析纪律，不是发言内容：公开发言里禁止出现"
            f"「照抄」「权威记录」「提示词」「规则」这类指令词，不要当众念记录表——"
            f"直接用自己的话说出事实和推理。也禁止逐字复读其他玩家说过的话或提示中的措辞。"
        )

        return "\n".join(parts)

    def _strategy_hint(self) -> str:
        """Role-specific strategy guidance with win condition."""
        role = self.wm.my_role
        if role == "werewolf":
            return (
                "🎯 胜利条件: 存活狼人数 ≥ 存活好人数（狼人达到半数即获胜）。\n"
                "⚠️ 铁律: 绝不说自己是狼人！不说'狼队友''我们狼人'！\n"
                "数值推理: 共3狼。每死一个队友，剩2狼需存活≥2好人时获胜。\n"
                "策略: 伪装村民视角发言，怀疑好人、保护队友。\n"
                "投票时: 投你发言中怀疑的人，保持言行一致不暴露。"
            )
        elif role == "seer":
            # Check if seer has found a wolf
            has_wolf_check = any(r == "werewolf" for r in self.wm.seer_checks.values())
            wolf_names = [n for n, r in self.wm.seer_checks.items() if r == "werewolf"]
            check_list = ", ".join(f"{n}={r}" for n, r in self.wm.seer_checks.items()) or "无"

            base = (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                f"你的查验记录: {check_list}\n"
            )
            if has_wolf_check:
                base += (
                    f"🔴 你已验证 {', '.join(wolf_names)} 是狼人！\n"
                    "‼️ 本轮讨论必须跳身份！公开发言必须包含:\n"
                    f"1) '我是预言家' 2) 完整查验链（{check_list}，逐轮报出）\n"
                    f"3) '所有人跟我投{wolf_names[0]}'——明确号召统一投票。\n"
                    "查验链越完整越可信；只喊查杀不报查验链会被当成假预言家投死！\n"
                )
            else:
                base += (
                    "【跳身份时机】\n"
                    "- 查到狼人 → 下一轮讨论必须立即跳，报完整查验链！\n"
                    "- 全是金水 → 保持隐藏！无查杀的空泛声明易被当假预言家投死，\n"
                    "  且暴露后当晚会吃狼刀，信息随你进坟墓。\n"
                    "- 例外: 你即将被投出局/残局决胜轮 → 跳出并报全部查验链自证。\n"
                    "【保护查证的好人】\n"
                    "- 查验到好人→发言中为他辩护，引导投票远离他（不暴露原因）。\n"
                )
            base += (
                "【识别假预言家】\n"
                "- 别人也自称预言家→他必然是假货。用查验结果揭穿。\n"
                "- 假预言家当晚没被刀→铁狼！真预言家是狼优先目标。\n"
            )
            return base
        elif role == "witch":
            silver = [s for s in self.wm.my_saves if s in self.wm.alive_players]
            silver_line = (
                f"【银水】你救过 {', '.join(silver)}——被狼刀的人大概率是好人（狼极少自刀），"
                f"相当于你的半个金水。白天若有人要投TA→暗中为TA辩护、引导票远离，"
                f"但绝不暴露你是女巫、不说'昨晚'之类的夜知信息。\n"
                if silver else ""
            )
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。根据存活人数可推算最多剩几只狼。\n"
                "策略: 伪装村民，用夜间死亡信息暗中分析。第1轮必救。\n"
                f"{silver_line}"
                "【毒药纪律】毒药不可逆: 无信息盲毒命中狼的概率仅3/8，误杀好人5/8——期望为负！\n"
                "  只在有明确狼证据（预言家查杀/投票抱团铁证）或残局锁定狼人时使用。\n"
                "⚠️ 铁律: 不透露你是女巫。不说'昨晚谁死了'等暴露夜知信息的话。\n"
                "❌ 绝对禁止假跳预言家！你是女巫不是预言家。假跳预言家=混淆好人=帮狼人赢！\n"
                "【预言家声明响应】唯一预言家报查杀→跟票其查杀对象。对跳→跟查验链完整者。\n"
                "发言中: 以村民视角分析，指出怀疑对象和理由，声明投票目标。\n"
                "投票时: 投你发言中怀疑的人。"
            )
        elif role == "hunter":
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。如果只剩4人而游戏未结束→最多1狼！\n"
                "策略: 伪装村民。你的威慑力在死后开枪。\n"
                "❌ 铁律: 绝不假跳预言家！你是猎人不是预言家。假跳=混浠好人=帮狼赢！\n"
                "如果有人正确说出了你是猎人→那个人是真预言家！你必须公开确认！\n"
                "【预言家声明响应】唯一预言家报查杀→跟票其查杀对象。对跳→跟查验链完整者。\n"
                "发言中: 以村民视角分析，指出怀疑对象和理由，声明投票目标。\n"
                "投票时: 投你发言中怀疑的人。"
            )
        elif role == "villager":
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。只剩4人→最多1只狼！\n"
                "你的武器是逻辑分析。用发言矛盾+投票记录+死亡信息推理。\n"
                "【证据纪律】指控必须引用一条可验证证据（发言矛盾/投票抱团/死亡信息）。\n"
                "  只有'感觉可疑''行为不自然'不是证据——凭感觉投票=替狼人刀人。\n"
                "  反过来: 无证据就点名别人的人本身可疑——狼必须制造投票目标，\n"
                "  第1轮无证据强打是唯一可验证的狼行为信号。\n"
                "【预言家声明响应】\n"
                "- 只有1人跳预言家并报查杀→默认跟随，投其查杀对象。好人票分散=失败主因。\n"
                "- 2人对跳→比查验链：报得出逐轮查验记录(第R轮查X=结果)的更可信；\n"
                "  只喊查杀报不出查验链的大概率是狼。\n"
                "【投票分析】看投票记录找规律：如果某几个人每轮都投同一个目标，\n"
                "  他们很可能是狼团队在统一冲票。分散的投票更像好人。\n"
                "❌ 严禁假跳: 绝不说'我是预言家''我是女巫''我是猎人'！\n"
                "   你假跳神职=帮狼人混淆视听=毁掉好人阵营。\n"
                "✅ 你应该: 分析发言矛盾+投票抱团，指出最可疑的人。\n"
                "发言中: 指出具体怀疑对象+具体证据+声明投谁。\n"
                "投票时: 投你发言中怀疑的人。言行必须一致！"
            )
        return "分析局势，指出怀疑对象和理由。"

    # LLM Call

    async def _llm(self, sys_prompt: str, max_t: int = 500, temp: float = 0.9,
                   fast: bool = False) -> str:
        """Call the LLM. fast=True routes to the cheap chat model for short
        structured decisions; default (reasoner) is used for speeches.

        Timeouts: 25s was far too short for a reasoning model (~20% of calls
        failed in observed games). Now 60s reasoner / 30s chat.
        """
        model = self.fast_model if fast else self.model
        timeout = 30 if fast else 60
        try:
            formatted = await self.formatter.format([
                Msg("system", sys_prompt, "system"),
                Msg("user", "请回复。", "user"),
            ])
            valid = [m for m in formatted if isinstance(m, dict) and m.get("content")]
            if not valid:
                return ""
            resp = await asyncio.wait_for(
                model(valid, temperature=temp, top_p=0.9, max_tokens=max_t),
                timeout=timeout,
            )
            text = self._extract_text(resp)
            # Empty-response retry: deepseek-reasoner counts chain-of-thought
            # against max_tokens — when the CoT exhausts the budget the API
            # returns EMPTY content with finish_reason=length. Detected here
            # and retried ONCE with the reasoner's hard cap (8000) before the
            # caller ever sees the failure (observed: ~20% empty at max_t=1500,
            # ~3% at 4000; one bounded retry costs ~5s, a fallback speech
            # costs a round of low-quality discussion).
            if not text and not fast and max_t < 8000:
                print(f"[{self.name}] LLM empty response (CoT exhausted max_tokens), retrying with 8000")
                resp = await asyncio.wait_for(
                    model(valid, temperature=temp, top_p=0.9, max_tokens=8000),
                    timeout=timeout,
                )
                text = self._extract_text(resp)
                # Second-level rescue: still empty at the reasoner's hard cap
                # (rare, ww_T1: 2/5 retries) → one attempt via the chat model,
                # which has no CoT budget to exhaust. Real content beats a
                # fallback template.
                if not text:
                    print(f"[{self.name}] reasoner still empty, falling back to chat model")
                    resp = await asyncio.wait_for(
                        self.fast_model(valid, temperature=temp, top_p=0.9, max_tokens=1500),
                        timeout=timeout,
                    )
                    text = self._extract_text(resp)
            return text
        except Exception as e:
            print(f"[{self.name}] LLM error ({'chat' if fast else 'reasoner'}): {e}")
            return ""

    def _extract_text(self, resp: Any) -> str:
        if resp is None:
            return ""
        # OpenAI SDK v1: ChatCompletion object
        if hasattr(resp, "choices") and resp.choices:
            choice = resp.choices[0]
            if hasattr(choice, "message") and choice.message:
                msg = choice.message
                if hasattr(msg, "content") and isinstance(msg.content, str):
                    return msg.content.strip()
                # Tool calls / structured output
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    return str(msg.tool_calls)
            # Legacy: content directly on choice
            if hasattr(choice, "content") and isinstance(choice.content, str):
                return choice.content.strip()
            return str(choice)
        # Fallback: resp.content (old API or custom objects)
        if hasattr(resp, "content"):
            c = resp.content
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c if isinstance(b, dict)).strip()
        return str(resp).strip() if resp else ""

    # Decision Parsing

    def _parse_target(self, text: str, mode: str) -> str:
        text = text.strip()
        alive = self.wm.alive_players
        role = self.wm.my_role

        if role == "witch" and mode == "night":
            is_self_attacked = self.wm.night_killed == self.name
            heal_gone = self.wm.healing_used
            # Resurrect intent: explicit "resurrect" or "救" NOT preceded by 不
            # (old code matched "救" anywhere, so "我不救人" was read as save)
            wants_save = ("resurrect" in text.lower()) or bool(re.search(r'(?<!不)救', text))
            if is_self_attacked or heal_gone:
                wants_save = False
            m = re.search(r'poison\s*[:：]?\s*(Player\d)', text, re.IGNORECASE)
            poison_target = m.group(1) if m and m.group(1) in alive and m.group(1) != self.name else None
            # Hard rule enforcement: night-1 poison is banned regardless of
            # what the LLM outputs (negative EV with zero information —
            # see _night_role_body).
            if poison_target and self.wm.round_num <= 1:
                print(f"[{self.name}] 首夜禁毒规则: 忽略毒药指令({poison_target})")
                poison_target = None
            if wants_save and poison_target:
                return f"resurrect+{poison_target}"  # Both potions in one night
            if wants_save:
                return "resurrect"
            if poison_target:
                return poison_target
            if any(w in text.lower() for w in ("none", "不用", "放弃", "pass")):
                return "none"
            m = re.search(r'(Player\d)', text)
            return m.group(1) if m and m.group(1) in alive and m.group(1) != self.name else "none"

        if mode == "hunter_shot":
            if any(w in text.lower() for w in ("none", "放弃", "pass", "不")):
                return "none"
            m = re.search(r'(Player\d)', text)
            return m.group(1) if m and m.group(1) in alive and m.group(1) != self.name else "none"

        # Werewolves: get mates for teammate protection
        mates = []
        if role == "werewolf" and mode in ("night", "vote"):
            mates = self._get_wolf_mates(alive)

        # Seer: prevent re-checking already-verified players
        already_checked = []
        if role == "seer" and mode == "night":
            already_checked = list(self.wm.seer_checks.keys())

        # Extract a valid target from LLM response
        m = re.search(r'(Player\d)', text)
        if m:
            candidate = m.group(1)
            # Werewolves cannot target teammates (night or vote)
            if mates and candidate in mates:
                print(f"[{self.name}] 拒绝{'杀' if mode=='night' else '投'}队友{candidate}，重选")
                non_wolves = [p for p in alive if p not in mates and p != self.name]
                return random.choice(non_wolves) if non_wolves else ""
            # Seer cannot re-check already checked players
            if already_checked and candidate in already_checked:
                print(f"[{self.name}] 拒绝重复查验{candidate}，重选")
                unchecked = [p for p in alive if p not in already_checked and p != self.name]
                return random.choice(unchecked) if unchecked else ""
            if candidate in alive and candidate != self.name:
                return candidate
        # R1 abstain (config-gated): honor an explicit abstain intent when no
        # valid player target was extracted. Checked AFTER the player regex so
        # "我不弃票，我投Player5" resolves to Player5, not abstain.
        if mode == "vote" and self.wm.round_num <= 1 and config.ALLOW_R1_ABSTAIN:
            if re.search(r'abstain|弃票', text, re.IGNORECASE):
                print(f"[{self.name}] 决策: 弃票")
                return "abstain"
        # Fallback: LLM output malformed → pick a RANDOM valid target.
        # (A deterministic first-alive fallback makes all timed-out agents
        # gang up on the same player — correlated failure bias.)
        if mates:
            non_wolves = [p for p in alive if p not in mates and p != self.name]
            if non_wolves:
                pick = random.choice(non_wolves)
                print(f"[{self.name}] LLM输出异常，从好人中随机选: {pick}")
                return pick
            return ""
        if already_checked:
            unchecked = [p for p in alive if p not in already_checked and p != self.name]
            if unchecked:
                pick = random.choice(unchecked)
                print(f"[{self.name}] LLM输出异常，从未查中随机选: {pick}")
                return pick
            return ""
        others = [p for p in alive if p != self.name]
        return random.choice(others) if others else ""

    def _extract_suspect(self, text: str) -> str:
        for pat in [
            r'(?:怀疑|投|投票给|投死)\s*(Player\d)',
            r'(Player\d)\s*(?:最|很|非常)\s*(?:可疑|像狼|是狼)',
        ]:
            m = re.search(pat, text)
            if m and m.group(1) != self.name:
                return m.group(1)
        if self.bt:
            for name, _ in self.bt.get_suspect_ranking(self.wm.alive_players):
                if name != self.name:
                    return name
        return ""

    def _decision_to_plan(self, mode: str, target: str) -> Dict[str, Any]:
        plan = {"suspect": target, "vote_plan": target, "strategy": mode}
        role = self.wm.my_role
        if mode == "vote":
            plan["strategy"] = "vote"
        elif mode == "night":
            if role == "werewolf":
                plan["strategy"] = "kill"
            elif role == "seer":
                plan["strategy"] = "check"
            elif role == "witch":
                if target == "resurrect":
                    self.wm.healing_used = True
                    if self.wm.night_killed:
                        self.wm.my_saves.append(self.wm.night_killed)  # 银水记录
                    plan.update(strategy="resurrect", suspect=self.wm.night_killed)
                elif target.startswith("resurrect+"):
                    self.wm.healing_used = True
                    self.wm.poison_used = True
                    if self.wm.night_killed:
                        self.wm.my_saves.append(self.wm.night_killed)  # 银水记录
                    plan.update(strategy="both", suspect=target.split("+", 1)[1])
                elif target == "none":
                    plan.update(strategy="pass", suspect=None)
                else:
                    self.wm.poison_used = True
                    plan["strategy"] = "poison"
        elif mode == "hunter_shot":
            plan["strategy"] = "shoot" if target != "none" else "pass"
            if target == "none":
                plan["suspect"] = None
        return plan

    def _empty(self) -> Dict:
        return {"suspect": None, "strategy": "wait", "vote_plan": "",
                "speech": "", "should_reveal_role": False}

    def _fallback_plan(self, is_last_words: bool = False) -> Dict:
        """Fallback when LLM fails — still produce a reasonable speech.

        Templates are split by context: an ALIVE player must never say
        "before I was eliminated" (observed in real games — it confused
        other agents' fact-checking).
        """
        import random
        alive = self.wm.alive_players
        target = ""
        if self.bt:
            for n, _ in self.bt.get_suspect_ranking(alive):
                if n != self.name:
                    target = n
                    break
        if not target:
            others = [p for p in alive if p != self.name]
            target = random.choice(others) if others else "?"
        if is_last_words:
            tpls = [
                f"我是{self.name}，已经被淘汰了。回顾这局游戏，我认为{target}最值得怀疑。希望大家仔细分析投票记录，找出真正的狼人。",
                f"很遗憾我被淘汰了。我的建议是重点关注{target}，他的逻辑链有明显的漏洞。好人阵营加油。",
            ]
        else:
            tpls = [
                f"我是{self.name}。这轮我重点怀疑{target}，他的发言缺乏实质分析，投票也跟风中立。我投{target}。",
                f"听完大家的发言，我认为{target}的立场最不清晰，既没有给出有效信息，也没有明确站边。本轮我投{target}。",
                f"从目前的信息看，{target}的行为模式最可疑。我本轮投{target}，也欢迎大家提出反证。",
            ]
        speech = random.choice(tpls)
        # Keep the machine-checkable commitment line consistent
        if not is_last_words and target != "?":
            speech += f"\n投票意向: {target}"
        print(f"[{self.name}] {speech}")
        self.memory.add(MemoryEntry(
            EntryType.MY_SPEECH, self.wm.round_num, self.name, speech,
            importance=2, metadata={"role": self.wm.my_role}
        ))
        return {"speech": speech, "suspect": target, "strategy": "discuss",
                "vote_plan": target}

    # LAYER 4: Action

    async def _act(self, plan: Dict, structured_model: Any = None) -> Msg:
        wm = self.wm
        role = wm.my_role or "unknown"

        if structured_model is not None:
            if wm.phase == "night" or self.name not in wm.alive_players:
                return self._structured_action(plan, role)
            if wm.phase == "voting":
                return self._structured_vote(plan)

        text = plan.get("speech", "")
        if not text:
            text = self._fallback(plan.get("suspect", ""))
        return Msg(name=self.name, content=text, role="assistant",
                   metadata={"strategy": plan.get("strategy", ""),
                             "target": plan.get("suspect", "none"),
                             "vote": plan.get("vote_plan", "")})

    def _structured_vote(self, plan: Dict) -> Msg:
        target = plan.get("vote_plan", "")
        alive = self.wm.alive_players
        r1_abstain = (self.wm.round_num <= 1 and config.ALLOW_R1_ABSTAIN)
        if target == "abstain" and r1_abstain:
            print(f"[{self.name}] 投票 → abstain（弃票）")
            self.state.setdefault("round_votes", []).append(
                {"round": self.wm.round_num, "target": "abstain"}
            )
            return Msg(name=self.name, content="abstain", role="assistant",
                       metadata={"vote": "abstain"})
        if target not in alive or target == self.name:
            # R1: abstain beats a random lynch when the decision failed
            if r1_abstain:
                print(f"[{self.name}] 投票 → abstain（弃票，决策无有效目标）")
                self.state.setdefault("round_votes", []).append(
                    {"round": self.wm.round_num, "target": "abstain"}
                )
                return Msg(name=self.name, content="abstain", role="assistant",
                           metadata={"vote": "abstain"})
            if self.bt:
                for name, _ in self.bt.get_suspect_ranking(alive):
                    if name != self.name:
                        target = name
                        break
            if target not in alive or target == self.name:
                others = [p for p in alive if p != self.name]
                target = random.choice(others) if others else ""
        print(f"[{self.name}] 投票 → {target}")
        _emit_web("vote", {"voter": self.name, "target": target})
        # Record for post-game data collection
        self.state.setdefault("round_votes", []).append(
            {"round": self.wm.round_num, "target": target}
        )
        return Msg(name=self.name, content=target, role="assistant",
                   metadata={"vote": target})

    def _structured_action(self, plan: Dict, role: str) -> Msg:
        strategy = plan.get("strategy", "")
        target = plan.get("suspect", "")
        meta = {}
        if role == "seer":
            meta["name"] = target
        elif role == "witch":
            if strategy == "resurrect":
                meta["resurrect"] = True
            elif strategy == "poison":
                meta["poison"], meta["name"] = True, target
            elif strategy == "both":
                meta["resurrect"] = True
                meta["poison"], meta["name"] = True, target
            else:
                meta["poison"] = False
        elif role == "hunter":
            meta["shoot"] = strategy == "shoot"
            if meta["shoot"]:
                meta["name"] = target
        elif role == "werewolf":
            meta["reach_agreement"] = True
            meta["proposed_target"] = target
        action_labels = {"kill": "击杀", "check": "查验", "resurrect": "救人",
                         "poison": "毒杀", "both": "救人+毒杀", "shoot": "开枪", "pass": "无行动"}
        label = action_labels.get(strategy, strategy)
        if strategy == "pass":
            print(f"[{self.name}] {label}")
        else:
            print(f"[{self.name}] {label}: {target}")
        _emit_web("night_action", {"player": self.name, "action": label,
                                    "target": target, "strategy": strategy})
        # Record for post-game data collection
        self.state.setdefault("actions", []).append(
            {"round": self.wm.round_num, "action": strategy, "target": target}
        )
        return Msg(name=self.name,
                   content=f"{role} chooses {target}" if target else f"{role} passes",
                   role="assistant", metadata=meta)

    # Helpers

    def _ready(self) -> bool:
        return bool(self.model and self.wm.my_role)

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        role = self.wm.my_role or ""
        # Remove role reveal for non-seer
        if role and role != "seer":
            text = re.sub(rf'(?:我|I)\s*(?:是|am)\s*(?:a\s*)?{role}', '', text, flags=re.IGNORECASE)
        # Werewolves: filter self-revealing phrases
        if role == "werewolf":
            for phrase in ["我的狼队友", "我们狼人", "狼队友是", "作为狼人"]:
                text = text.replace(phrase, "")
        # Filter invalid behavioral observations (text-only agents can't see faces)
        for phrase in ["观察他的表情", "看他的反应", "注意他的眼神", "他的表情",
                       "看他的样子", "眼神不对劲", "表情不对", "面色", "神情",
                       "看着很紧张", "看起来紧张", "表现得很紧张"]:
            text = text.replace(phrase, "")
        # Filter wrong player counts (11-20)
        for n in range(11, 21):
            text = re.sub(rf'(?<!\d){n}\s*[人 names 个 位]', f'{self.wm.n_alive}人', text)
        if len(text) > 300:
            # Truncate at last complete sentence within 200-300 range
            text = text[:300]
            last = max((text.rfind(c) for c in '。！？.!?'), default=-1)
            if last > 180:
                text = text[:last + 1]
            # If no good boundary found, force cut at last comma or space
            elif len(text) >= 300:
                alt = max((text.rfind(c) for c in '，, '), default=-1)
                if alt > 200:
                    text = text[:alt] + '。'
        return text.strip()

    def _fallback(self, suspect: str) -> str:
        alive = self.wm.alive_players
        target = suspect
        if not target or target == self.name:
            if self.bt:
                for name, _ in self.bt.get_suspect_ranking(alive):
                    if name != self.name:
                        target = name
                        break
        if not target or target == self.name:
            others = [p for p in alive if p != self.name]
            target = others[0] if others else "Player1"
        return random.choice([
            f"我注意到{target}的发言有矛盾，建议重点关注。",
            f"结合前面的讨论，我怀疑{target}。",
            f"{target}的逻辑链不完整，我今天投{target}。",
        ])

    # Serialization

    def state_dict(self) -> StateDictType:
        return {"name": self.name, "state": self.state}

    def load_state_dict(self, d: StateDictType) -> None:
        self.name = d["name"]
        self.state.update(d.get("state", {}))
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if api_key:
            self.model = OpenAIChatModel(
                model_name="deepseek-reasoner", api_key=api_key, stream=False,
                client_kwargs={"base_url": "https://api.deepseek.com"},
            )
            self.fast_model = OpenAIChatModel(
                model_name="deepseek-chat", api_key=api_key, stream=False,
                client_kwargs={"base_url": "https://api.deepseek.com"},
            )
        self.formatter = DeepSeekMultiAgentFormatter()
