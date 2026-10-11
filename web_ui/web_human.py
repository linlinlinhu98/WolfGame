# -*- coding: utf-8 -*-
"""WebHumanAgent — human player controlled via web UI.

Uses asyncio.Future to wait for input from the web interface.
The server sets the future result when the user submits their action.
"""
import asyncio
from typing import Any

from _vendor import Msg
from agent import PlayerAgent
import config

try:
    from web_ui.server import emit_event as _emit_web
except ImportError:  # web_ui not importable (e.g. console-only entry)
    _emit_web = lambda t, d: None


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
        self._event_loop = asyncio.get_running_loop()
        result = await self._pending_future
        self._pending_future = None
        return result

    def submit_input(self, text: str) -> None:
        """Called by Flask server when user submits (from another thread!)."""
        if self._pending_future and not self._pending_future.done():
            # Must use call_soon_threadsafe — Future.set_result is NOT thread-safe
            loop = getattr(self, '_event_loop', None)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(self._pending_future.set_result, text)
            else:
                self._pending_future.set_result(text)  # Fallback for same-thread

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
        # Witch stage is no longer sniffed from prompt text — the frontend
        # builds a two-step (heal -> poison) decision from the potion flags
        # (healing_used / poison_used / night_killed) and sends the exact
        # structured string ("resurrect" / "poison:X" / "resurrect+poison:X").
        # Add teammate info for werewolves (from the private TEAM_INFO channel)
        if wm.my_role == "werewolf":
            info["teammates"] = [m for m in wm.my_teammates
                if m in wm.alive_players and m != self.name]
            info["dead_teammates"] = [m for m in wm.my_teammates
                if m in wm.dead_players]
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
            _emit_web("speech", {"player": self.name, "content": speech})
            return Msg(name=self.name, content=speech, role="assistant",
                       metadata={"human": True})
        if wm.phase == "night":
            action = await self._wait_for_input()
            return self._build_action_msg("night", action)
        if wm.phase == "discussion":
            speech = await self._wait_for_input()
            _emit_web("speech", {"player": self.name, "content": speech})
            return Msg(name=self.name, content=speech, role="assistant",
                       metadata={"human": True})
        if wm.phase == "voting":
            alive = wm.alive_players
            # Strict vote loop: invalid input re-prompts instead of silently
            # coercing to another player. Empty input (what _stop_old_game
            # feeds the future to unblock an aborted game) still falls back
            # to a legal target so a reset game never hangs.
            while True:
                vote = await self._wait_for_input()
                v = vote.strip()
                if v.lower() == "abstain" and wm.round_num <= 1 and config.ALLOW_R1_ABSTAIN:
                    _emit_web("vote", {"voter": self.name, "target": "abstain"})
                    return Msg(name=self.name, content="abstain", role="assistant",
                               metadata={"vote": "abstain", "human": True})
                if v == "":
                    others = [p for p in alive if p != self.name]
                    v = others[0] if others else ""
                    break
                if v in alive and v != self.name:
                    break
                _emit_web("phase_log", {"panel": "day",
                          "text": f"⚠️ {self.name} 的投票「{v[:30]}」无效，请从存活玩家中重新选择。"})
            _emit_web("vote", {"voter": self.name, "target": v})
            return Msg(name=self.name, content=v, role="assistant",
                       metadata={"vote": v, "human": True})

        return Msg(name=self.name, content="", role="assistant")

    def _build_action_msg(self, mode: str, text: str) -> Msg:
        text = text.strip()
        role = self.wm.my_role
        alive = self.wm.alive_players
        meta = {"human": True}

        if role == "seer":
            meta["name"] = text if text in alive else (alive[0] if alive else "")
        elif role == "witch":
            # Structured protocol (no keyword sniffing): the UI sends exactly
            # "resurrect" | "poison:<名>" | "resurrect+poison:<名>" | "none".
            t = text.lower()
            if t.startswith("resurrect"):
                meta["resurrect"] = True
                self.wm.healing_used = True
                if "+poison:" in t:
                    target = text.split("+poison:", 1)[1].strip()
                    if target in alive:
                        meta["poison"] = True
                        meta["name"] = target
                        self.wm.poison_used = True
                    else:
                        meta["poison"] = False
            elif t.startswith("poison:"):
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
