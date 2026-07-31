# -*- coding: utf-8 -*-
"""WebHumanAgent — human player controlled via web UI.

Uses asyncio.Future to wait for input from the web interface.
The server sets the future result when the user submits their action.
"""
import asyncio
from typing import Any

from _vendor import Msg
from agent import PlayerAgent


class WebHumanAgent(PlayerAgent):
    """Human player agent that waits for web UI input.

    Instead of console input, uses asyncio.Future objects that are
    resolved by the Flask server when the user submits their action.
    """

    def __init__(self, name: str):
        super().__init__(name)
        self._pending_future: asyncio.Future | None = None
        # Public state for the web UI to display
        self.public_info = {"role": "", "camp": "", "alive_players": [],
                           "dead_players": [], "round": 0, "phase": ""}

    def _ready(self) -> bool:
        return self.wm.my_role is not None

    async def _wait_for_input(self) -> str:
        """Create a future and wait for the server to resolve it."""
        self._pending_future = asyncio.Future()
        result = await self._pending_future
        self._pending_future = None
        return result

    def submit_input(self, text: str) -> None:
        """Called by Flask server when user submits."""
        if self._pending_future and not self._pending_future.done():
            self._pending_future.set_result(text)

    def update_public_info(self, msg_content: str = ""):
        wm = self.wm
        info = {
            "role": wm.my_role, "camp": wm.my_camp,
            "alive_players": list(wm.alive_players),
            "dead_players": list(wm.dead_players),
            "round": wm.round_num, "phase": wm.phase,
            "night_killed": wm.night_killed,
            "healing_used": wm.healing_used,
            "poison_used": wm.poison_used,
            "seer_checks": dict(wm.seer_checks),
        }
        # Detect witch stage from prompt content
        if wm.my_role == "witch" and wm.phase == "night":
            if "resurrect" in msg_content.lower() or "救" in msg_content or "解药" in msg_content:
                info["witch_stage"] = "resurrect"
            elif "poison" in msg_content.lower() or "毒药" in msg_content or "毒杀" in msg_content:
                info["witch_stage"] = "poison"
        # Add teammate info for werewolves
        if wm.my_role == "werewolf":
            info["teammates"] = [p for p in wm.alive_players
                if self.state.get("name_to_role", {}).get(p) == "werewolf"
                and p != self.name]
            info["dead_teammates"] = [p for p in wm.dead_players
                if self.state.get("name_to_role", {}).get(p) == "werewolf"]
        self.public_info = info

    async def __call__(
        self, msg: Msg | None = None, *, structured_model: Any = None, **kw
    ) -> Msg:
        msg_text = ""
        if msg is not None:
            msg_text = (msg.content or "") if hasattr(msg, 'content') else str(msg)
            await self._perceive(msg)
        self.update_public_info(msg_text)

        wm = self.wm
        if not wm.my_role:
            return Msg(name=self.name, content="", role="assistant")

        is_dead = self.name not in wm.alive_players and wm.round_num > 0

        if is_dead and structured_model is not None:
            action = await self._wait_for_input()
            return self._build_action_msg("hunter_shot", action)
        if is_dead:
            speech = await self._wait_for_input()
            return Msg(name=self.name, content=speech, role="assistant",
                       metadata={"human": True})
        if wm.phase == "night":
            action = await self._wait_for_input()
            return self._build_action_msg("night", action)
        if wm.phase == "discussion":
            speech = await self._wait_for_input()
            return Msg(name=self.name, content=speech, role="assistant",
                       metadata={"human": True})
        if wm.phase == "voting":
            vote = await self._wait_for_input()
            alive = wm.alive_players
            if vote not in alive or vote == self.name:
                others = [p for p in alive if p != self.name]
                vote = others[0] if others else ""
            return Msg(name=self.name, content=vote, role="assistant",
                       metadata={"vote": vote, "human": True})

        return Msg(name=self.name, content="", role="assistant")

    def _build_action_msg(self, mode: str, text: str) -> Msg:
        text = text.strip()
        role = self.wm.my_role
        alive = self.wm.alive_players
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
            meta["shoot"] = text.lower() != "none"
            if meta["shoot"]:
                meta["name"] = text if text in alive else ""
        elif role == "werewolf":
            meta["reach_agreement"] = True
            meta["proposed_target"] = text if text in alive else ""

        return Msg(name=self.name, content=f"{role}: {text}",
                   role="assistant", metadata=meta)
