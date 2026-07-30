# -*- coding: utf-8 -*-
"""Utility functions for the werewolf game."""
import asyncio
from collections import defaultdict
from copy import deepcopy
from typing import Any, Dict, List, Tuple

import numpy as np
from _vendor import AudioBlock, Msg, ReActAgent, AgentBase
from prompt import ChinesePrompts as Prompts

MAX_GAME_ROUND = 30
MAX_DISCUSSION_ROUND = 3

async def async_print(
        self,
        msg: Msg,
        last: bool = True,
        speech: AudioBlock | list[AudioBlock] | None = None,
    ) -> None:
        """The function to display the message.

        Args:
            msg (`Msg`):
                The message object to be printed.
            last (`bool`, defaults to `True`):
                Whether this is the last one in streaming messages. For
                non-streaming message, this should always be `True`.
            speech (`AudioBlock | list[AudioBlock] | None`, optional):
                The audio content block(s) to be played along with the
                message.
        """
        
        if self._disable_console_output:
            return
        
        # 处理纯文本内容（Msg 类的核心属性，所有版本都支持）
        if isinstance(msg.content, str) and msg.content.strip():
            try:
                print(f"[{msg.name}] {msg.content}")
            except UnicodeEncodeError:
                # Windows GBK console can't handle emojis — encode explicitly
                safe = msg.content.encode("gbk", errors="replace").decode("gbk")
                print(f"[{msg.name}] {safe}")
        
        # 兼容少量结构化场景：如果 content 是列表/字典，做解析
        elif isinstance(msg.content, (list, dict)):
            # 转为字符串打印，避免迭代报错
            print(f"[{msg.name}] {str(msg.content)}")
        
        if not self._disable_msg_queue:
            await self.msg_queue.put((deepcopy(msg), last, speech))

async def handle_tie_vote(
    tie_candidates: List[str],
    agents: Dict[str, Any],
    state: Dict[str, Any]
) -> Tuple[List[str], bool]:
    """
    平票处理：仅非平票玩家重投，且仅能投平票候选人
    :param tie_candidates: 平票的玩家列表
    :param agents: 所有智能体{玩家名: 实例}
    :param state: 游戏状态
    :return: (重投投票结果列表, 是否再次平票)
    """
    # 1. 通知平票对象发言
    print(f"\n=== 平票触发重投 ===")
    print(f"平票玩家：{tie_candidates}（需发言自证）")
    
    # 存储重投结果
    new_votes = []
    
    # 2. 筛选重投参与玩家：仅存活+非平票候选人
    re_vote_players = [
        name for name in state["current_alive"]
        if name not in tie_candidates and name in agents
    ]
    print(f"参与重投的玩家：{re_vote_players}（仅能投票给平票候选人）")

    # 3. 发起重投
    for player in re_vote_players:
        agent = agents[player]
        # 构造重投提示
        vote_prompt = Msg(
            name="Moderator",
            content=f"现在是重投阶段，请从平票候选人 {tie_candidates} 中选择一人投票淘汰。",
            role="system"
        )
        
        # 创建投票模型
        from pydantic import BaseModel, Field
        from typing import Literal
        
        class ReVoteModel(BaseModel):
            vote: Literal[tuple(tie_candidates)] = Field(
                description="你要投票淘汰的平票候选人"
            )
        
        # 获取投票
        vote_msg = await agent(
            vote_prompt,
            structured_model=ReVoteModel,
            strict_mode=True,
            retry=2
        )
        
        if vote_msg and vote_msg.metadata and vote_msg.metadata.get("vote"):
            vote_target = vote_msg.metadata["vote"]
            if vote_target in tie_candidates:
                new_votes.append(vote_target)
                print(f"[{player}] 投票给：{vote_target}")

    # 4. 统计重投结果
    from collections import Counter
    if not new_votes:
        print("重投无有效投票")
        return [], True
    
    vote_counts = Counter(new_votes)
    max_count = max(vote_counts.values())
    is_re_tie = sum(1 for cnt in vote_counts.values() if cnt == max_count) > 1
    
    print(f"重投结果：{dict(vote_counts)}，是否再次平票：{is_re_tie}")

    return new_votes, is_re_tie

def majority_vote(
    votes: List[str], 
    alive_players: List[str],
    tie_candidates: List[str] = None  # 平局重投时仅能投这些候选人
) -> Tuple[str, str, bool]:
    """
    统计投票：适配平票重投规则
    :param votes: 投票列表
    :param alive_players: 存活玩家列表
    :param tie_candidates: 重投时仅能投的平票候选人（None=正常投票）
    :return: (最终目标/平票标记/none, 票数统计, 是否平票)
    """
   # 1. 过滤无效投票（None/空字符串/非存活玩家），重投时仅保留平票候选人
    valid_votes = []
    for v in votes:
        # 优化：过滤 None + 空字符串/全空格 + 非存活玩家
        if v is None or not v.strip() or v not in alive_players:
            continue
        # 平局重投局面：仅保留平票候选人的投票
        if tie_candidates and v not in tie_candidates:
            continue
        valid_votes.append(v)

    if not valid_votes:
        # 无有效投票 视同平票，返回True
        return "none", "无有效投票", True

    # 2. 统计票数
    vote_counts = {}
    for v in valid_votes:
        vote_counts[v] = vote_counts.get(v, 0) + 1

    # 3. 检查是否平票
    max_count = max(vote_counts.values())
    tie_candidates_result = [v for v, cnt in vote_counts.items() if cnt == max_count]
    is_tie = len(tie_candidates_result) > 1

    # 4. 生成结果（区分正常投票/重投）
    if is_tie:
        # 平票：返回平票候选人+统计+平票标记
        tie_str = f"tie: {','.join(tie_candidates_result)}"
        vote_stats = ", ".join([f"{name}: {count}" for name, count in vote_counts.items()])
        return tie_str, vote_stats, True
    else:
        # 非平票：返回结果
        result = tie_candidates_result[0]
        vote_stats = ", ".join([f"{name}: {count}" for name, count in vote_counts.items()])
        return result, vote_stats, False

def names_to_str(agents: list[str] | list) -> str:
    """Return a string of agent names."""
    if not agents:
        return ""
    names = []

    if len(agents) == 1:
        agent = agents[0]
        if hasattr(agent, "name"):
            return str(agent.name)
        return str(agent)
    
    names = []
    for agent in agents:
        # 处理有name属性的对象（如PlayerAgent, ReActAgent等）
        if hasattr(agent, "name"):
            names.append(str(agent.name))
        # 处理字符串
        elif isinstance(agent, str):
            names.append(agent)
        # 其他类型转为字符串
        else:
            names.append(str(agent))
    return ", ".join([*names[:-1], "和 " + names[-1]])


class EchoAgent(AgentBase):
    """Echo agent that repeats the input message."""

    def __init__(self) -> None:
        super().__init__()
        self.name = "Moderator"

        # 1. 消息队列开关（控制是否存储消息到队列）
        self._disable_msg_queue: bool = False
        # 2. 异步消息队列（存储 (msg, last, speech) 元组）
        self.msg_queue: asyncio.Queue = asyncio.Queue()
        # 3. 控制台输出开关（控制是否打印到控制台）
        self._disable_console_output: bool = False
        # 4. 流式输出前缀缓存（适配流式消息清理逻辑）
        self._stream_prefix: Dict[str, Dict[str, Any]] = {}
        # 5. 临时存储：累计的打印内容（简化文本块拼接）
        self._thinking_and_text_cache: Dict[str, List[str]] = {}
    
    def _print_text_block(
        self,
        msg_id: str,
        name_prefix: str,
        text_content: str,
        thinking_and_text_to_print: List[str],
    ) -> None:
        """核心文本块打印：拼接文本并缓存"""
        if msg_id not in self._thinking_and_text_cache:
            self._thinking_and_text_cache[msg_id] = []
        text_line = f"[{name_prefix}] {text_content}"
        self._thinking_and_text_cache[msg_id].append(text_line)
        thinking_and_text_to_print.extend(self._thinking_and_text_cache[msg_id])

    async def reply(self, content: str) -> Msg:
        """Repeat the input content with its name and role."""
        msg = Msg(
            self.name,
            content,
            role="assistant",
        )
        await async_print(self=self,msg=msg)
        return msg

    async def handle_interrupt(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Msg:
        """Handle interrupt."""

    async def observe(self, msg: Msg | list[Msg] | None) -> None:
        """Observe the user's message."""


class Players:
    """Maintain the players' status."""

    def __init__(self) -> None:
        """Initialize the players."""
        # The mapping from player name to role
        self.name_to_role = {}
        self.role_to_names = defaultdict(list)
        self.name_to_agent = {}
        self.werewolves = []
        self.villagers = []
        self.seer = []
        self.hunter = []
        self.witch = []
        self.current_alive = []
        self.all_players = []

    def add_player(self, player: ReActAgent, role: str) -> None:
        """Add a player to the game.

        Args:
            player (`ReActAgent`):
                The player to be added.
            role (`str`):
                The role of the player.
        """
        self.name_to_role[player.name] = role
        self.name_to_agent[player.name] = player
        self.role_to_names[role].append(player.name)
        self.all_players.append(player)
        if role == "werewolf":
            self.werewolves.append(player)
        elif role == "villager":
            self.villagers.append(player)
        elif role == "seer":
            self.seer.append(player)
        elif role == "hunter":
            self.hunter.append(player)
        elif role == "witch":
            self.witch.append(player)
        else:
            raise ValueError(f"Unknown role: {role}")
        self.current_alive.append(player)

    def update_players(self, dead_players: list[ReActAgent]) -> None:
        """Update the current alive players.

        Args:
            dead_players (`list[ReActAgent]`):
                A list of dead players to be removed.
        """
        dead_names = [p.name if hasattr(p, 'name') else p for p in dead_players if p]

        self.werewolves = [
            _ for _ in self.werewolves if _.name not in dead_names
        ]
        self.villagers = [
            _ for _ in self.villagers if _.name not in dead_names
        ]
        self.seer = [_ for _ in self.seer if _.name not in dead_names]
        self.hunter = [_ for _ in self.hunter if _.name not in dead_names]
        self.witch = [_ for _ in self.witch if _.name not in dead_names]
        self.current_alive = [
            _ for _ in self.current_alive if _.name not in dead_names
        ]

    def print_roles(self) -> None:
        """Print the roles of all players."""
        print("Roles:")
        for name, role in self.name_to_role.items():
            print(f" - {name}: {role}")

    def check_winning(self) -> str | None:
        """Check if the game is over and return the winning message."""

        # Prepare true roles string in Chinese
        true_roles = (
            f'{names_to_str(self.role_to_names["werewolf"])} 是狼人，'
            f'{names_to_str(self.role_to_names["villager"])} 是村民，'
            f'{names_to_str(self.role_to_names["seer"])} 是预言家，'
            f'{names_to_str(self.role_to_names["hunter"])} 是猎人，'
            f'{names_to_str(self.role_to_names["witch"])} 是女巫。'
        )

        if len(self.werewolves) * 2 >= len(self.current_alive):
            return Prompts.to_all_wolf_win.format(
                n_alive=len(self.current_alive),
                n_werewolves=len(self.werewolves),
                true_roles=true_roles,
            )
        if self.current_alive and not self.werewolves:
            return Prompts.to_all_village_win.format(
                true_roles=true_roles,
            )
        return None
