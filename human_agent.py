# -*- coding: utf-8 -*-
"""HumanAgent — allows a human player to join a werewolf game.

The human plays alongside 8 AI agents. They receive only public information
(no private night actions of others, no internal reasoning).

Human input is via console. The game waits for human input at appropriate times.
"""
import asyncio
from typing import Any

from _vendor import Msg
from agent import PlayerAgent
from reasoning import GameEvent, EventType, WorkingMemory


class HumanAgent(PlayerAgent):
    """A human-controlled player in the werewolf game.

    Overrides the AI reasoning paths to wait for console input instead.
    Only public information is shown — the human sees what a real player sees.
    """

    def __init__(self, name: str):
        super().__init__(name)
        self._model = None  # Human doesn't need LLM

    def _ready(self) -> bool:
        return self.wm.my_role is not None

    async def __call__(
        self, msg: Msg | None = None, *, structured_model: Any = None, **kw
    ) -> Msg:
        if msg is not None:
            await self._perceive(msg)

        wm = self.wm
        if not wm.my_role:
            return Msg(name=self.name, content="", role="assistant")

        phase = wm.phase
        is_dead = self.name not in wm.alive_players and wm.round_num > 0

        if is_dead and structured_model is not None:
            return await self._human_action("猎人开枪", structured_model)
        if is_dead:
            return await self._human_speech(is_last_words=True)
        if phase == "night":
            return await self._human_action("夜晚行动", structured_model)
        if phase == "discussion":
            return await self._human_speech()
        if phase == "voting":
            return await self._human_vote(structured_model)

        return Msg(name=self.name, content="", role="assistant")

    async def _human_speech(self, is_last_words: bool = False) -> Msg:
        """Wait for human to type their speech."""
        wm = self.wm
        print(f"\n{'='*40}")
        print(f"[你的回合] {self.name} | 身份: {wm.my_role} | 第{wm.round_num}轮")
        print(f"存活: {', '.join(wm.alive_players)}")
        print(f"已死: {', '.join(wm.dead_players) if wm.dead_players else '无'}")
        if is_last_words:
            print("[遗言] 你已被淘汰，发表遗言")
        print(f"{'='*40}")
        print("输入你的发言 (回车发送):")
        text = await asyncio.get_event_loop().run_in_executor(None, input)
        return Msg(name=self.name, content=text.strip(), role="assistant",
                   metadata={"human": True})

    async def _human_vote(self, structured_model: Any) -> Msg:
        """Wait for human to vote."""
        alive = self.wm.alive_players
        print(f"\n[投票] 请从以下存活玩家中选择淘汰目标:")
        for i, p in enumerate(alive):
            if p != self.name:
                print(f"  {i+1}. {p}")
        print("输入玩家名投票:")
        target = await asyncio.get_event_loop().run_in_executor(None, input)
        target = target.strip()
        if target not in alive or target == self.name:
            print("无效目标，随机选择")
            others = [p for p in alive if p != self.name]
            target = others[0] if others else ""
        return Msg(name=self.name, content=target, role="assistant",
                   metadata={"vote": target, "human": True})

    async def _human_action(self, label: str, structured_model: Any) -> Msg:
        """Wait for human night action."""
        wm = self.wm
        role = wm.my_role
        alive = wm.alive_players

        print(f"\n{'='*40}")
        print(f"[{label}] {self.name} | 身份: {role} | 第{wm.round_num}轮")
        print(f"存活: {', '.join(alive)}")
        if role == "werewolf":
            mates = [p for p in alive
                     if self.state.get("name_to_role", {}).get(p) == "werewolf"
                     and p != self.name]
            print(f"狼队友: {', '.join(mates) if mates else '无'}")
            targets = [p for p in alive if p not in mates and p != self.name]
            print(f"可选目标: {', '.join(targets)}")
            print("输入击杀目标:")
        elif role == "seer":
            checked = wm.seer_checks
            print(f"已查验: {', '.join(f'{n}={r}' for n,r in checked.items()) if checked else '无'}")
            unchecked = [p for p in alive if p not in checked and p != self.name]
            print(f"可选: {', '.join(unchecked) if unchecked else '无'}")
            print("输入查验目标:")
        elif role == "witch":
            print(f"解药: {'可用' if not wm.healing_used else '已用'}")
            print(f"毒药: {'可用' if not wm.poison_used else '已用'}")
            nk = wm.night_killed
            if nk and nk != self.name:
                print(f"今晚 {nk} 被杀。输入 resurrect 救人, poison:PlayerX 毒人, 或 none:")
            else:
                print("输入 poison:PlayerX 毒人, 或 none:")
        elif role == "hunter":
            print("输入要带走的玩家名, 或 none 放弃:")
        else:
            print("输入 none:")

        text = await asyncio.get_event_loop().run_in_executor(None, input)
        text = text.strip()

        meta = {"human": True}
        if role == "seer":
            meta["name"] = text if text in alive else (alive[0] if alive else "")
        elif role == "witch":
            if text.lower().startswith("resurrect"):
                meta["resurrect"] = True
                self.wm.healing_used = True
            elif text.lower().startswith("poison:"):
                target = text.split(":", 1)[1].strip()
                if target in alive:
                    meta["poison"] = True
                    meta["name"] = target
                    self.wm.poison_used = True
                else:
                    meta["poison"] = False
            else:
                meta["poison"] = False
        elif role == "hunter":
            if text.lower() == "none":
                meta["shoot"] = False
            else:
                meta["shoot"] = True
                meta["name"] = text if text in alive else ""
        elif role == "werewolf":
            meta["reach_agreement"] = True
            meta["proposed_target"] = text if text in alive else ""

        return Msg(name=self.name, content=f"{role} chooses {text}",
                   role="assistant", metadata=meta)
