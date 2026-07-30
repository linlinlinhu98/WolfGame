# -*- coding: utf-8 -*-
"""Standalone vendor layer — replaces agentscope framework imports.

This file provides minimal implementations of the agentscope APIs used by
the werewolf game, so the project can run independently without requiring
a specific development version of agentscope.

Usage:
    from _vendor import Msg, ReActAgentBase, OpenAIChatModel, ...
"""

import asyncio
import os
import uuid
import random
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Msg — simple message object
# ---------------------------------------------------------------------------

@dataclass
class Msg:
    """A message in the agent conversation. Drop-in replacement for agentscope.message.Msg."""
    name: str
    content: str
    role: str  # "system" | "user" | "assistant"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.id = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# ReActAgentBase — minimal agent base class
# ---------------------------------------------------------------------------

class ReActAgentBase:
    """Minimal base class for reactive agents.

    Subclasses must implement:
        __call__(self, msg, *, structured_model=None, **kw) -> Msg
        reply(self, msg) -> Msg
        observe(self, msg) -> None
        state_dict(self) -> dict
        load_state_dict(self, d: dict) -> None
    """

    def __init__(self):
        self._subscribers: Dict[str, List] = {}

    async def __call__(
        self, msg: Msg | None = None, *, structured_model: Any = None, **kw
    ) -> Msg:
        raise NotImplementedError

    async def reply(self, msg: Msg) -> Msg:
        return await self.__call__(msg)

    async def observe(self, msg: Msg | List[Msg] | None) -> None:
        """Default no-op. Subclasses override."""

    def state_dict(self) -> dict:
        return {}

    def load_state_dict(self, d: dict) -> None:
        pass


# ---------------------------------------------------------------------------
# OpenAIChatModel — thin wrapper around OpenAI/DeepSeek API
# ---------------------------------------------------------------------------

class OpenAIChatModel:
    """Calls an OpenAI-compatible chat API (DeepSeek, OpenAI, etc.)."""

    def __init__(
        self,
        model_name: str = "deepseek-chat",
        api_key: str = "",
        stream: bool = False,
        client_kwargs: dict | None = None,
    ):
        self.model_name = model_name
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.stream = stream
        self.client_kwargs = client_kwargs or {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise ImportError(
                    "openai package required. Install: pip install openai"
                )
            base_url = self.client_kwargs.get("base_url", "https://api.deepseek.com")
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=base_url,
            )
        return self._client

    async def __call__(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 500,
    ) -> Any:
        client = self._get_client()
        response = await client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        return response


# ---------------------------------------------------------------------------
# DeepSeekMultiAgentFormatter — formats messages for DeepSeek API
# ---------------------------------------------------------------------------

class DeepSeekMultiAgentFormatter:
    """Formats AgentScope-style Msg objects into OpenAI-compatible dicts."""

    async def format(self, messages: List[Msg]) -> List[dict]:
        """Convert Msg objects to dict format for API call."""
        result = []
        for m in messages:
            if isinstance(m, dict):
                result.append(m)
                continue
            role = m.role if hasattr(m, 'role') else "user"
            content = m.content if hasattr(m, 'content') else str(m)
            result.append({"role": role, "content": content})
        return result


# ---------------------------------------------------------------------------
# MsgHub — message broadcasting hub (replaces agentscope.pipeline._msghub)
# ---------------------------------------------------------------------------

class MsgHub:
    """Async context manager that broadcasts messages to participant agents.

    Usage:
        async with MsgHub(participants=agents) as hub:
            await hub.broadcast(msg)
    """

    def __init__(
        self,
        participants: List[Any],
        announcement: Any = None,
        enable_auto_broadcast: bool = True,
        name: str = "",
    ):
        self.participants = list(participants)
        self._auto_broadcast = enable_auto_broadcast
        self.announcement = announcement
        self.name = name
        self._entered = False

    async def __aenter__(self):
        self._entered = True
        if self.announcement:
            await self.broadcast(self.announcement)
        return self

    async def __aexit__(self, *args):
        self._entered = False

    def set_auto_broadcast(self, enabled: bool):
        self._auto_broadcast = enabled

    async def broadcast(self, msg_or_msgs: Any):
        """Send message(s) to all participants via their observe() method."""
        if msg_or_msgs is None:
            return
        items = msg_or_msgs if isinstance(msg_or_msgs, list) else [msg_or_msgs]
        for agent in self.participants:
            for m in items:
                if m and hasattr(agent, 'observe'):
                    await agent.observe(m)


# ---------------------------------------------------------------------------
# fanout_pipeline — parallel agent execution (replaces agentscope)
# ---------------------------------------------------------------------------

async def fanout_pipeline(
    agents: List[Any],
    msg: Any,
    structured_model: Any = None,
    enable_gather: bool = True,
    strict_mode: bool = True,
    retry: int = 3,
    require_metadata: bool = True,
) -> List[Any]:
    """Run agents in parallel with the same message, collect results.

    Replacement for agentscope.pipeline._functional.fanout_pipeline.
    """
    async def _run_one(agent):
        for attempt in range(retry + 1):
            try:
                result = await agent(msg, structured_model=structured_model)
                if result is not None:
                    return result
            except Exception as e:
                if attempt == retry:
                    return None
                await asyncio.sleep(0.5)
        return None

    tasks = [_run_one(a) for a in agents]
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Misc stubs used by utils.py and structured_model.py
# ---------------------------------------------------------------------------

class AgentBase:
    """Minimal stub for agentscope.agent.AgentBase."""
    name: str = ""

    async def __call__(self, content: str) -> "Msg":
        """Convert a string into a Msg — used by EchoAgent (moderator)."""
        return Msg(name=getattr(self, "name", "System"),
                   content=str(content), role="assistant")


class ReActAgent(AgentBase):
    """Minimal stub for agentscope.agent.ReActAgent."""
    pass


class AudioBlock:
    """Stub for agentscope.message._message_block.AudioBlock."""
    pass
