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

        # LLM Backend
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not set")
        self.model = OpenAIChatModel(
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

        # Legacy state (game.py compatibility)
        self.state: Dict[str, Any] = {
            "role": None, "camp": None, "game_phase": None,
            "is_alive": True, "name_to_role": {},
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
            self.bt.initialize(all_p, role, self.state.get("name_to_role", {}))

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
                for p in event.metadata["players"]:
                    self.bt.observe_night_death(
                        p, self.state.get("name_to_role", {}),
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
        prompt = self._build_decision_prompt(mode)
        text = await self._llm(prompt, max_t=400, temp=0.7)
        # Split reasoning and decision
        reasoning, decision = self._split_reasoning(text)
        if reasoning:
            print(f"[{self.name}] 推理: {reasoning[:200]}")
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
        text = await self._llm(prompt, max_t=800, temp=0.9)
        if not text or len(text) < 10:
            print(f"[{self.name}] WARNING: LLM empty, using fallback")
            return self._fallback_plan()

        # Split internal reasoning from public speech
        internal, public = self._split_internal_public(text)
        if internal:
            print(f"[{self.name}] 内部: {internal[:150]}")
        public = self._clean_text(public)
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

    # Prompt Builders — personalized + full context

    def _build_decision_prompt(self, mode: str) -> str:
        """Decision prompt — uses Fact Checklist + Recency Repetition.

        Inspired by research on U-shaped attention (Chowdhury 2025):
        - Critical facts placed at BOTH start and end
        - LLM must acknowledge facts before reasoning
        - Token budget <500 to keep facts in attention window
        """
        wm = self.wm
        alive = wm.alive_players
        role = wm.my_role
        camp_label = "狼人" if wm.my_camp == "werewolf" else "好人"

        # Build fact checklist — MUST include agent's own name
        n_alive = wm.n_alive
        max_wolves = min(3, (n_alive - 1) // 2)  # Game continues → good > wolves
        facts = [f"我是{self.name}, 身份={role}({camp_label})"]
        facts.append(f"存活{n_alive}人→最多{max_wolves}狼")
        if wm.dead_players:
            facts.append(f"已死={','.join(wm.dead_players)}")
        if wm.healing_used:
            facts.append(f"解药=已用")
        if wm.poison_used:
            facts.append(f"毒药=已用")
        if wm.seer_checks:
            items = [f"{n}={r}" for n, r in wm.seer_checks.items()]
            facts.append(f"查验={', '.join(items)}")
        fact_block = " | ".join(facts)

        # Summaries in the middle (short, compressed)
        summaries = format_speech_summaries(self._speech_summaries, max_per_round=10)
        if not summaries.strip():
            summaries = "暂无讨论"

        # Build by mode
        if mode == "vote":
            # Include agent's own speech to enforce speech-vote consistency
            my_speeches = self.memory.my_speeches()
            last_speech = my_speeches[-1] if my_speeches else ""
            my_speech_block = f"你上一轮发言: 「{last_speech[:200]}」\n" if last_speech else ""
            body = (
                f"存活: {', '.join(alive)}\n\n"
                f"{my_speech_block}"
                f"讨论摘要:\n{summaries}"
            )
        elif mode == "night":
            body = f"{self._night_role_body()}\n\n{summaries}"
        elif mode == "hunter_shot":
            body = f"存活: {', '.join(alive)}\n\n{summaries}"
        else:
            body = summaries

        # Fact checklist at START (primacy) + END (recency)
        vote_extra = ""
        if mode == "vote":
            vote_extra = (
                "⚠️ 你的投票必须与你发言中怀疑的人一致！\n"
                "如果你发言说了怀疑PlayerX，就必须投PlayerX。言行不一会被识破。\n"
            )
            # Wolves: never vote to eliminate teammates
            if role == "werewolf":
                mates = self._get_wolf_mates(alive)
                if mates:
                    vote_extra += (
                        f"🔴 绝对禁止投狼队友: {', '.join(mates)}！"
                        f"投队友等于自杀。只能投好人。\n"
                    )
        return (
            f"已知: {fact_block}\n\n"
            f"{body}\n\n"
            f"{vote_extra}"
            f"再次确认: {fact_block}\n"
            f"基于以上事实推理,输出: 推理: 我怀疑[玩家名]因为...(一句话)\n决策: [玩家名]"
        )

    def _get_wolf_mates(self, alive: list) -> list:
        """Get alive wolf teammates (excluding self)."""
        return [p for p in alive
                if self.state.get("name_to_role", {}).get(p) == "werewolf"
                and p != self.name]

    def _night_role_body(self) -> str:
        wm = self.wm
        role = wm.my_role
        alive = wm.alive_players

        if role == "werewolf":
            mates = self._get_wolf_mates(alive)
            dead_mates = [p for p in wm.dead_players
                         if self.state.get("name_to_role", {}).get(p) == "werewolf"]
            non_wolves = [p for p in alive if p not in mates and p != self.name]
            mate_str = f"队友: {', '.join(mates)}" if mates else "你没有队友了！"
            dead_str = f"（已死队友: {', '.join(dead_mates)}）" if dead_mates else ""
            return (
                f"【狼人杀人】{mate_str}{dead_str}\n"
                f"⚠️ 绝对禁止杀队友: {', '.join(mates)} ← 这些玩家你绝不能选！\n"
                f"只能从好人中选: {', '.join(non_wolves)}\n"
                f"优先杀预言家/女巫/猎人。只回复一个存活好人名。"
            )
        elif role == "seer":
            ck = wm.seer_checks
            c_str = ", ".join(f"{n}({r})" for n, r in ck.items()) or "无"
            unchecked = [p for p in alive if p not in ck and p != self.name]
            # Also filter dead players from unchecked
            unchecked = [p for p in unchecked if p not in wm.dead_players]
            dead_list = [p for p in wm.dead_players]
            dead_warn = f"\n已死玩家（不可查）: {', '.join(dead_list)}" if dead_list else ""
            return (
                f"【预言家查验】已查:{c_str}\n"
                f"未查:{', '.join(unchecked) if unchecked else '无'}{dead_warn}\n"
                f"只回复存活玩家名。"
            )
        elif role == "witch":
            h_ok = not wm.healing_used
            p_ok = not wm.poison_used
            nk = wm.night_killed
            # Build candidate list for poison (all alive except self, and except saved player)
            poison_candidates = [p for p in alive if p != self.name]
            poison_list = ", ".join(poison_candidates) if poison_candidates else "无"
            if nk == self.name:
                heal = f"\n你被狼袭击了！不能自救。"
                heal += f"\n毒药可选: {poison_list}"
                heal += f"\n回复: none（不用毒） 或 poison:玩家名"
            elif h_ok and nk:
                heal = f"\n今晚{nk}被杀。建议救TA。"
                heal += f"\n毒药可选: {poison_list}"
                heal += f"\n回复: resurrect（救人） 或 poison:玩家名 或 none（都不用）"
            else:
                heal = f"\n毒药可选: {poison_list}"
                heal += f"\n回复: poison:玩家名 或 none（不用毒）"
            strategy = ""
            if h_ok and wm.round_num == 1:
                strategy = "\n策略提示: 第1轮被刀的很可能是好人，强烈建议救！"
            if p_ok and wm.round_num >= 2:
                strategy += "\n策略提示: 毒药还没用，对高度怀疑的玩家使用。"
            return (
                f"【女巫】解药:{'可用' if h_ok else '已用'} 毒药:{'可用' if p_ok else '已用'}{heal}{strategy}"
            )
        return "回复none。"

    def _build_discussion_prompt(self, is_last_words: bool = False) -> str:
        """Two-pass discussion prompt: fact grounding → strategy → speech.

        Like Claude Code's harness: first verify what's known, then generate.
        Wolves are allowed strategic deception but NOT fabricating facts.
        """
        wm = self.wm
        alive = wm.alive_players
        role = wm.my_role
        camp = "狼人" if wm.my_camp == "werewolf" else "好人"
        is_wolf = role == "werewolf"

        # Progressive memory
        all_summaries = self._speech_summaries
        spoke = [s.speaker for s in all_summaries if s.speaker != self.name]
        seen = set()
        spoke = [s for s in spoke if not (s in seen or seen.add(s))]
        pos = len(spoke) + 1
        still = [p for p in alive if p not in spoke and p != self.name]
        current_round = wm.round_num
        before_me = [s for s in all_summaries
                     if s.round_num == current_round and s.speaker in spoke]
        current_context = format_speech_summaries(before_me) if before_me else "（你是本轮第一个发言）"

        past_summaries = [s for s in all_summaries if s.round_num < current_round]
        past_context = ""
        if past_summaries:
            by_round: dict[int, list] = {}
            for s in past_summaries:
                by_round.setdefault(s.round_num, []).append(s)
            past_lines = []
            for r in sorted(by_round.keys()):
                line = format_round_summary(by_round[r])
                if line:
                    past_lines.append(f"第{r}轮: {line}")
            if past_lines:
                past_context = "\n".join(past_lines)

        # Known facts (verifiable from memory)
        n_total = 9
        n_alive = wm.n_alive
        n_dead = wm.n_dead
        n_wolves_total = 3  # 9-player game: 3 wolves

        # Deduction: if game continues, wolves haven't won → good > wolves
        # n_alive = n_good + n_wolves. So n_wolves < n_alive/2
        max_remaining_wolves = (n_alive - 1) // 2
        if max_remaining_wolves > 3:
            max_remaining_wolves = 3

        # Count confirmed dead wolves (known to this agent)
        confirmed_dead_wolves = 0
        if is_wolf:
            for p in wm.dead_players:
                if self.state.get("name_to_role", {}).get(p) == "werewolf":
                    confirmed_dead_wolves += 1

        known_facts = [f"你是{self.name}，身份{role}。"]
        known_facts.append(
            f"共{n_total}人，{n_wolves_total}狼。存活{n_alive}人，已死{n_dead}人。"
        )
        known_facts.append(
            f"⚠️ 核心推理：游戏未结束→狼人未达半数→当前最多剩{max_remaining_wolves}个狼人！"
        )
        if is_wolf:
            mates = [p for p in alive
                     if self.state.get("name_to_role", {}).get(p) == "werewolf"
                     and p != self.name]
            known_facts.append(f"狼队友: {', '.join(mates) if mates else '无'}。"
                             f" 存活狼人共{len(mates)+1}个。")
        if wm.dead_players:
            known_facts.append(f"已死: {', '.join(wm.dead_players)}。")
        if confirmed_dead_wolves > 0:
            known_facts.append(f"已确认死亡的狼人: {confirmed_dead_wolves}个。")
        if wm.seer_checks:
            items = [f"{n}={r}" for n, r in wm.seer_checks.items()]
            known_facts.append(f"查验结果: {', '.join(items)}。")
        if wm.healing_used:
            known_facts.append("解药已用。")
        if wm.poison_used:
            known_facts.append("毒药已用。")

        # Deception rules
        if is_wolf:
            deception_rules = (
                "⚠️ 发言规则（狼人）:\n"
                "你可以: 伪装身份、假装怀疑好人、引导投票方向。\n"
                "禁止: 编造不存在的发言、说错人数/名单、暴露狼队友。"
            )
        else:
            if role in ("seer", "witch", "hunter"):
                deception_rules = (
                    "⚠️ 发言规则（神职）:\n"
                    "可以说谎隐藏身份，但不要假跳其他神职。\n"
                    "怀疑对象+分析理由+投票目标，三者必须一致。"
                )
            else:
                deception_rules = (
                    "⚠️ 发言规则（村民）:\n"
                    "你是村民，必须说真话。绝对禁止假跳预言家/女巫/猎人！\n"
                    "假跳不会帮好人，只会让真神职被怀疑、让狼人有机可乘。\n"
                    "你的任务: 分析矛盾→指出嫌疑→投票淘汰狼人。"
                )

        last_note = ""
        if is_last_words:
            last_note = f"⚠️ 你({self.name})已被淘汰，遗言！不要说'我会'等未来式。\n"

        suspect_context = ""
        if self.bt:
            top = self.bt.get_suspect_ranking(alive)[:2]
            for name, score in top:
                if score > 0.35:
                    mentions = [s for s in all_summaries
                               if name in s.accusations or name in s.defenses]
                    if mentions:
                        suspect_context += f"关于{name}: {format_speech_summaries(mentions, max_per_round=3)}\n"

        return (
            f"9人局 | 第{wm.round_num}轮 | 存活{wm.n_alive}已死{wm.n_dead} | "
            f"你是{role}({camp}) | 第{pos}位发言\n\n"
            f"【已知事实】\n" + "\n".join(f"- {f}" for f in known_facts) + "\n\n"
            f"【你的性格】{self.persona.to_prompt()}\n{self._strategy_hint()}\n"
            f"{last_note}\n"
            f"【前几轮摘要】\n{past_context}\n\n"
            f"【本轮之前发言】\n{current_context}\n\n"
            f"{suspect_context}"
            f"【你的怀疑度】\n{self.bt.get_belief_summary(alive) if self.bt else '暂无'}\n\n"
            f"【认知边界】\n"
            f"你是文字AI，只能从发言内容/投票记录/死亡信息中推理。\n"
            f"禁止说'观察表情''注意反应''看他眼神'等——你看不到别人。\n"
            f"禁止编造别人没说过的话。\n\n"
            f"【发言要求】\n"
            f"如有线索: 指出怀疑对象+理由（发言矛盾/投票异常/查验结果）+声明投谁。\n"
            f"如无线索: 排除你信任的人，从剩余存活着中随机指一个，说明是随机选择。\n"
            f"例: '目前信息不足，我排除Player3和Player5(我觉得是好人)，从其余人中随机投Player8。'\n"
            f"无论哪种情况，都要明确说投谁。发言60-150字。\n"
            f"{deception_rules}\n\n"
            f"格式:\n"
            f"内部: (作为{role}，一句话策略)\n公开: (你的发言)"
        )

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
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "【跳身份时机】\n"
                "- 查到狼人 → 第2轮必须跳！说出查验历史+狼人是谁。\n"
                "- 连续查到好人 → 第2-3轮可跳，建立好人信任网。\n"
                "- 跳身份的风险: 当晚大概率被狼杀，但你的信息能帮好人赢。\n"
                "【识别假预言家】\n"
                "- 如果别人也自称预言家→他必然是狼/愚民。查验结果对比即可揭穿。\n"
                "- 假预言家当晚没被刀→铁狼！因为真预言家是狼优先杀的目标。\n"
                "【保护查证的好人】\n"
                "- 查验到好人→在发言中为他辩护，引导投票远离他。\n"
                "- 查验到狼人→坚决带队推他出局。\n"
                "【发言模板】\n"
                "跳身份: '我是预言家，第1晚查了PlayerX是好人/狼人...'\n"
                "未跳: 以村民视角分析，暗中保护查证的好人。"
            )
        elif role == "witch":
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。根据存活人数可推算最多剩几只狼。\n"
                "策略: 伪装村民，用夜间死亡信息暗中分析。第1轮必救。\n"
                "⚠️ 铁律: 不透露你是女巫。不说'昨晚谁死了'等暴露夜知信息的话。\n"
                "发言中: 以村民视角分析，指出怀疑对象和理由，声明投票目标。\n"
                "投票时: 投你发言中怀疑的人。"
            )
        elif role == "hunter":
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。如果只剩4人而游戏未结束→最多1狼！\n"
                "策略: 伪装村民。你的威慑力在死后开枪。\n"
                "⚠️ 铁律: 绝不假跳预言家/女巫！你是猎人（死后才能证明），假跳只会混乱好人阵营。\n"
                "发言中: 以村民视角分析，指出怀疑对象和理由，声明投票目标。\n"
                "投票时: 投你发言中怀疑的人。"
            )
        elif role == "villager":
            return (
                "🎯 胜利条件: 所有狼人被淘汰（共3狼）。\n"
                "数值推理: 游戏继续→狼人未达半数。如果只剩4人→最多1只狼，不是3只！\n"
                "你的武器是逻辑分析。用发言矛盾+投票记录+死亡信息推理。\n"
                "❌ 严禁假跳: 绝不说'我是预言家''我是女巫''我是猎人'！\n"
                "   你假跳神职=帮狼人混淆视听=毁掉好人阵营。\n"
                "✅ 你应该: 分析发言中的矛盾，指出谁最可疑并解释为什么。\n"
                "发言中: 指出具体怀疑对象+分析理由+声明投谁。\n"
                "投票时: 投你发言中怀疑的人。言行必须一致！"
            )
        return "分析局势，指出怀疑对象和理由。"

    # LLM Call

    async def _llm(self, sys_prompt: str, max_t: int = 500, temp: float = 0.9) -> str:
        try:
            formatted = await self.formatter.format([
                Msg("system", sys_prompt, "system"),
                Msg("user", "请回复。", "user"),
            ])
            valid = [m for m in formatted if isinstance(m, dict) and m.get("content")]
            if not valid:
                return ""
            resp = await asyncio.wait_for(
                self.model(valid, temperature=temp, top_p=0.9, max_tokens=max_t),
                timeout=25,
            )
        except Exception as e:
            print(f"[{self.name}] LLM error: {e}")
            return ""
        return self._extract_text(resp)

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
            if not is_self_attacked and not heal_gone and ("resurrect" in text.lower() or "救" in text):
                return "resurrect"
            m = re.search(r'poison\s*[:：]?\s*(Player\d)', text, re.IGNORECASE)
            if m and m.group(1) in alive and m.group(1) != self.name:
                return m.group(1)
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

        # Extract a valid target from LLM response
        m = re.search(r'(Player\d)', text)
        if m:
            candidate = m.group(1)
            # Werewolves cannot target teammates (night or vote)
            if mates and candidate in mates:
                print(f"[{self.name}] 拒绝{'杀' if mode=='night' else '投'}队友{candidate}，重选")
                non_wolves = [p for p in alive if p not in mates and p != self.name]
                return non_wolves[0] if non_wolves else ""
            if candidate in alive and candidate != self.name:
                return candidate
        # Fallback: LLM output malformed → pick first valid non-self, non-teammate target
        if mates:
            non_wolves = [p for p in alive if p not in mates and p != self.name]
            if non_wolves:
                print(f"[{self.name}] LLM输出异常，从好人中选: {non_wolves[0]}")
                return non_wolves[0]
            return ""
        others = [p for p in alive if p != self.name]
        return others[0] if others else ""

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
                    plan.update(strategy="resurrect", suspect=self.wm.night_killed)
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

    def _fallback_plan(self) -> Dict:
        """Fallback when LLM fails — still produce a reasonable speech."""
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
            target = others[0] if others else "?"
        tpls = [
            f"我是{self.name}，已经被淘汰了。回顾这局游戏，我认为{target}的发言最值得怀疑。希望大家仔细分析投票记录，找出真正的狼人。",
            f"作为{self.wm.my_role}，我在被淘汰前观察到{target}的行为模式很可疑。他在发言中多次回避关键问题，投票也跟风。请大家关注他。",
            f"很遗憾我被淘汰了。我的建议是重点关注{target}，他的逻辑链有明显的漏洞。好人阵营加油。",
        ]
        speech = random.choice(tpls)
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
        if target not in alive or target == self.name:
            if self.bt:
                for name, _ in self.bt.get_suspect_ranking(alive):
                    if name != self.name:
                        target = name
                        break
            if target not in alive or target == self.name:
                others = [p for p in alive if p != self.name]
                target = others[0] if others else ""
        print(f"[{self.name}] 投票 → {target}")
        _emit_web("vote", {"voter": self.name, "target": target})
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
                         "poison": "毒杀", "shoot": "开枪", "pass": "无行动"}
        label = action_labels.get(strategy, strategy)
        if strategy == "pass":
            print(f"[{self.name}] {label}")
        else:
            print(f"[{self.name}] {label}: {target}")
        _emit_web("night_action", {"player": self.name, "action": label,
                                    "target": target, "strategy": strategy})
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
                model_name="deepseek-chat", api_key=api_key, stream=False,
                client_kwargs={"base_url": "https://api.deepseek.com"},
            )
        self.formatter = DeepSeekMultiAgentFormatter()
