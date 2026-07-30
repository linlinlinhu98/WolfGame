"""A werewolf game implemented by agentscope."""
import asyncio
from copy import deepcopy
import time
from datetime import datetime
import numpy as np
import shortuuid
import json
from pathlib import Path

from agent import PlayerAgent
from agentscope.pipeline._msghub import MsgHub
from agentscope.pipeline._functional import fanout_pipeline

from utils import (
    handle_tie_vote,
    majority_vote,
    names_to_str,
    EchoAgent,
    MAX_GAME_ROUND,
    MAX_DISCUSSION_ROUND,
    Players,
)
from structured_model import (
    DiscussionModel,
    get_vote_model,
    get_poison_model,
    WitchResurrectModel,
    get_seer_model,
    get_hunter_model,
)
from prompt import ChinesePrompts as Prompts

# Optional web UI integration
try:
    from web_ui.server import emit_event as _emit_web_event
except ImportError:
    _emit_web_event = lambda t, d: None  # no-op if web_ui not available

moderator = EchoAgent()

# 创建数据存储目录
data_dir = Path("game_data")
data_dir.mkdir(exist_ok=True)

# 数据收集模块
def collect_game_data(agents, players, game_result, game_rounds):
    """
    收集游戏数据，记录游戏的完整过程
    
    Args:
        agents: 所有玩家智能体
        players: 游戏玩家对象
        game_result: 游戏结果
        game_rounds: 游戏轮次信息
    """
    # 生成游戏ID
    game_id = str(shortuuid.uuid())
    timestamp = datetime.now().isoformat()
    
    # 收集玩家信息
    player_data = []
    for agent in agents:
        if hasattr(agent, 'name') and hasattr(agent, 'state'):
            player_data.append({
                "name": agent.name,
                "role": agent.state.get("role", "unknown"),
                "camp": agent.state.get("camp", "unknown"),
                "speeches": agent.state.get("round_discussions", []),
                "votes": agent.state.get("round_votes", []),
                "actions": agent.state.get("actions", []),
                "is_alive": agent.state.get("is_alive", False)
            })
    
    # 构建游戏数据
    game_data = {
        "id": game_id,
        "timestamp": timestamp,
        "players": player_data,
        "rounds": game_rounds,
        "result": game_result,
        "winner": "werewolf" if "werewolf" in game_result.lower() else "villager"
    }
    
    # 保存数据到文件
    file_path = data_dir / f"{game_id}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(game_data, f, ensure_ascii=False, indent=2)
    
    print(f"游戏数据已保存到: {file_path}")
    return file_path

def check_player_ready(player) -> bool:
    """校验单个玩家是否初始化完成"""
    if player is None:
        print("玩家对象为空，初始化失败")
        return False
        
    required_attrs = ["name", "state", "model"]
    
    # 1. 检查必备属性是否存在
    for attr in required_attrs:
        if not hasattr(player, attr):
            print(f"玩家{getattr(player, 'name', '未知')}缺失属性：{attr}")
            return False
    
    # 2. 检查state是否为字典
    state = player.state
    if not isinstance(state, dict):
        print(f"玩家{player.name}的state不是字典类型：{type(state)}")
        return False
    
    # 3. 检查state中核心字段是否存在（role/camp/current_alive）
    missing_keys = []
    for key in ["role", "camp", "current_alive"]:
        if key not in state:
            missing_keys.append(key)
            # 自动补全current_alive空值（兼容未初始化场景）
            if key == "current_alive":
                player.state["current_alive"] = []
    if missing_keys:
        print(f"玩家{player.name}的state缺失核心字段：{missing_keys}")
        return False
    
    # 4. 核心：检查角色和阵营是否非空（不能是None/空字符串）
    role = state["role"]
    camp = state["camp"]
    if role is None or str(role).strip() == "":
        print(f"玩家{player.name}未分配角色（role=None/空）")
        return False
    if camp is None or str(camp).strip() == "":
        print(f"玩家{player.name}未分配阵营（camp=None/空）")
        return False
    
    # 5. 检查模型是否加载完成
    if player.model is None:
        print(f"玩家{player.name}的推理模型未加载（model=None）")
        return False
    
    # 所有校验通过
    return True

def sync_alive_status(players: Players):
    alive_names = [p.name for p in players.current_alive]
    dead_names = [p.name for p in players.all_players if p.name not in alive_names]
    for agent in players.all_players:
        agent.state["current_alive"] = alive_names.copy()
        agent.state["dead_players"] = dead_names.copy()
        agent.state["is_alive"] = agent.name in alive_names
        # Also sync WorkingMemory so LLM decisions use fresh data
        if hasattr(agent, 'wm'):
            agent.wm.alive_players = alive_names.copy()
            agent.wm.dead_players = dead_names.copy()

async def wait_all_players_ready(players: Players, max_wait_time: int = 180, check_interval: int = 2) -> bool:
    """全局等待：直到所有玩家初始化完成"""
    start_time = time.time()
    n_players = 9
    player_list = players.all_players if hasattr(players, 'all_players') else []

    if not player_list:
        print("玩家列表为空，无法初始化")
        return False

    while time.time() - start_time < max_wait_time:
        # 统计已就绪玩家数量
        ready_count = sum(1 for p in player_list if check_player_ready(p))

        # 所有玩家就绪：打印完整信息
        if ready_count == n_players:
            print(f"\n所有玩家初始化完成！（{ready_count}/{n_players}）")
            print("\n所有玩家角色 & 阵营分配结果：")
            print("-" * 50)
            # 按阵营分组打印
            camp_groups = {}
            for p in player_list:
                camp = p.state["camp"]
                role = p.state["role"]
                if camp not in camp_groups:
                    camp_groups[camp] = []
                camp_groups[camp].append({"name": p.name, "role": role})
            # 遍历分组，格式化打印，每个阵营内按玩家序号排序
            for camp, player_info_list in camp_groups.items():
                # 按玩家序号排序（提取数字部分）
                sorted_players = sorted(player_info_list, key=lambda x: int(''.join(filter(str.isdigit, x['name'])))
)
                print(f"【{camp.upper()} 阵营】")
                for info in sorted_players:
                    print(f"  └─ {info['name']} → {info['role']}")
            print("-" * 50)
            return True
        
        print(f"初始化中... 当前就绪：{ready_count}/{n_players} 位玩家，{check_interval}秒后重试")
        await asyncio.sleep(check_interval)  # 替换time.sleep，兼容异步上下文

    # 如果到时间还没初始化完，抛出明确错误
    raise TimeoutError(f"\n玩家初始化超时({max_wait_time}秒)！请检查角色分配/模型加载逻辑")

async def hunter_stage(
    hunter_agent: PlayerAgent,
    players: Players,
) -> str | None:
    """Because the hunter's stage may happen in two places: killed at night
    or voted during the day, we define a function here to avoid duplication."""
    global moderator
    # 防御性检查：猎人对象为空直接返回
    if hunter_agent is None or not check_player_ready(hunter_agent):
        return None
    
    msg_hunter = await hunter_agent(
        await moderator(Prompts.to_hunter.format(name=hunter_agent.name)),
        structured_model=get_hunter_model(players.current_alive),
    )
    # 严格校验shoot和name字段
    if msg_hunter.metadata.get("shoot") and msg_hunter.metadata.get("name"):
        target_name = msg_hunter.metadata.get("name")
        # 校验目标是否存活
        if target_name in [p.name for p in players.current_alive]:
            return target_name
    return None

MAX_GAME_ROUND = 10
MAX_DISCUSSION_ROUND = 5  # 最大讨论轮次

async def werewolves_game(agents: list[PlayerAgent]) -> None:
    """The main entry of the werewolf game

    Args:
        agents (`list[ReActAgent]`):
            A list of 9 agents.
    """
    assert len(agents) == 9, "The werewolf game needs exactly 9 players."

    # Init the players' status
    players = Players()

    # If the witch has healing and poison potion
    healing, poison = True, True

    # If it's the first day, the dead can leave a message
    first_day = True
    
    # 记录游戏轮次信息
    game_rounds = []

    # Broadcast the game begin message
    async with MsgHub(participants=agents) as greeting_hub:
        await greeting_hub.broadcast(
            await moderator(
                Prompts.to_all_new_game.format(names_to_str(agents)),
            ),
        )

    # Assign roles to the agents
    roles = ["werewolf"] * 3 + ["villager"] * 3 + ["seer", "witch", "hunter"]
    np.random.shuffle(agents)
    np.random.shuffle(roles)
    name_to_role = {}
    all_player_names = [agent.name for agent in agents]

    for agent, role in zip(agents, roles):
        # 初始化玩家state（确保非空）
        if not hasattr(agent, 'state') or agent.state is None:
            agent.state = {}
        
        # Tell the agent its role
        await agent.observe(
            await moderator(
                f"[{agent.name} ONLY] {agent.name}, your role is {role}.",
            ),
        )
        players.add_player(agent, role)
        # 记录映射关系
        name_to_role[agent.name] = role

        # 同步映射表和存活状态到所有玩家的 state 中
        agent.state["name_to_role"] = deepcopy(name_to_role)
        agent.state["current_alive"] = all_player_names.copy()
        # 补充阵营字段（根据角色自动分配）
        if role == "werewolf":
            agent.state["camp"] = "werewolf"
        else:
            agent.state["camp"] = "villager"

    async with MsgHub(participants=agents) as init_hub:
        alive_str = names_to_str(all_player_names)
        await init_hub.broadcast(
            await moderator(
                f"Current alive players are: {alive_str}",  # 匹配agent.py的解析关键词
            ),
        )

    # Printing the roles
    players.print_roles()

    try:
        await wait_all_players_ready(players)
    except TimeoutError as e:
        print(e)
        return
    
    # 4. 所有玩家分配完成，进入正式游戏
    print("\n开始正式狼人杀游戏！")

    # GAME BEGIN!
    _emit_web_event("init", {
        "players": [{"name": a.name, "role": a.state.get("role", "?"),
                      "camp": a.state.get("camp", "?")}
                     for a in agents]
    })
    for round_idx in range(MAX_GAME_ROUND):
        print(f"\n游戏第 {round_idx + 1} 轮")
        _emit_web_event("phase_change", {"phase": "night", "round": round_idx + 1})
        _emit_web_event("phase_log", {"panel": "night", "text": Prompts.to_all_night})

        # Night phase
        async with MsgHub(
            participants=players.current_alive,
            enable_auto_broadcast=False,  # manual broadcast only
            name=f"alive_players_round_{round_idx}",
        ) as alive_players_hub:

            await alive_players_hub.broadcast(
                await moderator(Prompts.to_all_night),
            )
            killed_player, poisoned_player, shot_player = None, None, None

            # Werewolves discuss - each wolf proposes a target via agent reasoning
            if players.werewolves and len(players.werewolves) > 0:
                wolf_discuss_text = Prompts.to_wolves_discussion.format(
                    names_to_str(players.werewolves),
                    names_to_str(players.current_alive),
                )
                _emit_web_event("phase_log", {"panel": "night", "text": wolf_discuss_text})
                werewolves_hub = MsgHub(
                    players.werewolves,
                    enable_auto_broadcast=True,
                    announcement=await moderator(wolf_discuss_text),
                    name=f"werewolves_round_{round_idx}",
                )
                try:
                    await werewolves_hub.__aenter__()

                    wolf_proposals = {}
                    non_wolves = [
                        p.name for p in players.current_alive
                        if p not in players.werewolves
                    ]

                    # Each wolf decides sequentially, seeing prior proposals
                    for wolf in players.werewolves:
                        if not check_player_ready(wolf):
                            continue

                        # Let wolves see what previous wolves chose
                        vote_prompt = Prompts.to_wolves_vote
                        if wolf_proposals:
                            prior = "；".join(
                                f"{w}选了{t}" for w, t in wolf_proposals.items()
                            )
                            vote_prompt = (
                                f"{Prompts.to_wolves_vote}"
                                f"（已有狼人选: {prior}。建议统一目标）"
                            )

                        night_msg = await wolf(
                            await moderator(vote_prompt),
                            structured_model=DiscussionModel,
                        )

                        target = night_msg.metadata.get("proposed_target") if night_msg.metadata else None
                        if not target:
                            # Extract from content if not in metadata
                            content = night_msg.content if isinstance(night_msg.content, str) else ""
                            for name in non_wolves:
                                if name in content:
                                    target = name
                                    break

                        if target and target in non_wolves:
                            wolf_proposals[wolf.name] = target
                            _emit_web_event("wolf_proposal", {"player": wolf.name, "target": target})
                        elif non_wolves:
                            import random
                            if target:
                                print(f"[{wolf.name}] 选了狼队友{target}，改为随机选择")
                            target = random.choice(non_wolves)
                            wolf_proposals[wolf.name] = target
                            _emit_web_event("wolf_proposal", {"player": wolf.name, "target": target, "random": True})

                    # Count votes for each target — handle ties properly
                    unified_target = None
                    if wolf_proposals:
                        from collections import Counter
                        import random
                        target_counts = Counter(wolf_proposals.values())
                        # Get all targets with max votes
                        max_votes = max(target_counts.values())
                        top_targets = [t for t, c in target_counts.items() if c == max_votes]
                        if len(top_targets) == 1:
                            unified_target = top_targets[0]
                            print(f"狼人达成共识，击杀目标：{unified_target}（{max_votes}票）")
                        else:
                            # Tie — random among top targets
                            unified_target = random.choice(top_targets)
                            print(f"狼人意见分歧（平局{top_targets}），随机选择：{unified_target}")

                    if not unified_target and non_wolves:
                        import random
                        unified_target = random.choice(non_wolves)
                        print(f"狼人未达成统一，随机击杀：{unified_target}")

                    if unified_target:
                        killed_player = unified_target
                        discussion_result = await moderator(
                            Prompts.to_wolves_res.format("unanimous agreement", killed_player),
                        )
                        await werewolves_hub.broadcast(discussion_result)

                finally:
                    await werewolves_hub.__aexit__(None, None, None)
                    del werewolves_hub

            # Witch's turn
            if players.witch and len(players.witch) > 0:
                _emit_web_event("phase_log", {"panel": "night", "text": Prompts.to_all_witch_turn})
                await alive_players_hub.broadcast(
                    await moderator(Prompts.to_all_witch_turn),
                )
                msg_witch_poison = None
                for agent in players.witch:
                    if not check_player_ready(agent):
                        continue
                    if not agent.state.get("is_alive", True):
                        continue  # Dead witch can't act
                        
                    # Cannot heal witch herself
                    msg_witch_resurrect = None
                    if healing and killed_player and killed_player != agent.name:
                        msg_witch_resurrect = await agent(
                            await moderator(
                                Prompts.to_witch_resurrect.format(
                                    witch_name=agent.name, dead_name=killed_player
                                ),
                            ),
                            structured_model=WitchResurrectModel,
                            strict_mode=True,
                            retry=2
                        )
                        if msg_witch_resurrect and msg_witch_resurrect.metadata.get("resurrect"):
                            killed_player = None
                            healing = False
                    elif healing and killed_player and killed_player == agent.name:
                        await agent.observe(
                            await moderator(
                                f"[仅女巫可见] {agent.name}，你是女巫，今晚你被狼人袭击了。你不能用解药救自己，但毒药仍然可用。",
                            ),
                        )
                    elif healing:
                        # 没有人被击杀，但女巫还有解药
                        pass

                    # Has poison potion
                    if poison:
                        msg_witch_poison = await agent(
                            await moderator(
                                Prompts.to_witch_poison.format(witch_name=agent.name),
                            ),
                            structured_model=get_poison_model(players.current_alive),
                            strict_mode=True,
                            retry=2
                        )
                        if msg_witch_poison and msg_witch_poison.metadata.get("poison", False):
                            poisoned_player = msg_witch_poison.metadata.get("name")
                            # 校验毒杀目标存活
                            if poisoned_player not in [p.name for p in players.current_alive]:
                                poisoned_player = None
                            else:
                                poison = False

            # Seer's turn
            if players.seer and len(players.seer) > 0:
                _emit_web_event("phase_log", {"panel": "night", "text": Prompts.to_all_seer_turn})
                await alive_players_hub.broadcast(
                    await moderator(Prompts.to_all_seer_turn),
                )
                for agent in players.seer:
                    if not check_player_ready(agent):
                        continue
                    if not agent.state.get("is_alive", True):
                        continue  # Dead seer can't check
                        
                    msg_seer = await agent(
                        await moderator(
                            Prompts.to_seer.format(
                                agent.name,
                                names_to_str(players.current_alive),
                            ),
                        ),
                        structured_model=get_seer_model(players.current_alive),
                        strict_mode=True,
                        retry=2
                    )
                    if msg_seer and msg_seer.metadata.get("name"):
                        player_name = msg_seer.metadata["name"]
                        # 校验查验目标存活
                        if player_name in [p.name for p in players.current_alive]:
                            checked_role = players.name_to_role.get(player_name, 'unknown')
                            # 只有预言家自己知道查验结果
                            await agent.observe(
                                await moderator(
                                    f"[{agent.name} ONLY] 你查验了{player_name}，他的身份是{checked_role}。"
                                ),
                            )
                            # Emit seer result for god mode UI
                            _emit_web_event("seer_result", {"seer": agent.name, "target": player_name, "role": checked_role})
                            # 更新预言家的查验记录
                            if hasattr(agent, 'state') and isinstance(agent.state, dict):
                                if 'seer_checked' not in agent.state:
                                    agent.state['seer_checked'] = {}
                                agent.state['seer_checked'][player_name] = checked_role

            # Hunter's turn (night)
            if players.hunter and len(players.hunter) > 0:
                for agent in players.hunter:
                    if not check_player_ready(agent):
                        continue
                    # If killed and not by witch's poison
                    if (
                        killed_player == agent.name
                        and poisoned_player != agent.name
                    ):
                        shot_player = await hunter_stage(agent, players)

            # Update alive players
            dead_tonight = [killed_player, poisoned_player, shot_player]
            dead_tonight = [p for p in dead_tonight if p and p in [x.name for x in players.current_alive]]
            if dead_tonight:
                print(f"夜间死亡玩家：{dead_tonight}")
                _emit_web_event("death", {"players": dead_tonight, "phase": "night"})
                players.update_players(dead_tonight)
                sync_alive_status(players)

            # Day phase
            alive_player_names = [p.name for p in players.current_alive if check_player_ready(p)]
            _emit_web_event("phase_change", {"phase": "day", "round": round_idx + 1})
            if dead_tonight:
                day_text = Prompts.to_all_day.format(names_to_str(dead_tonight))
                _emit_web_event("phase_log", {"panel": "day", "text": day_text, "label": "天亮"})
                await alive_players_hub.broadcast(
                    await moderator(day_text),
                )

                # The killed player leave a last message in first night
                if killed_player and first_day:
                    killed_agent = players.name_to_agent.get(killed_player)
                    if killed_agent and check_player_ready(killed_agent):
                        last_words_prompt = Prompts.to_dead_player.format(killed_player)
                        _emit_web_event("phase_log", {"panel": "day", "text": last_words_prompt, "label": "遗言"})
                        msg_moderator = await moderator(last_words_prompt)
                        await alive_players_hub.broadcast(msg_moderator)
                        # Leave a message
                        last_msg = await killed_agent(msg_moderator)
                        await alive_players_hub.broadcast(last_msg)

            else:
                _emit_web_event("phase_log", {"panel": "day", "text": Prompts.to_all_peace})
                await alive_players_hub.broadcast(
                    await moderator(Prompts.to_all_peace),
                )

            # Check winning
            res = players.check_winning()
            if res:
                await moderator(res)
                _emit_web_event("game_over", {"result": res})
                print()
                # Reflection disabled
                collect_game_data(agents, players, res, game_rounds)
                return

            # Discussion
            discuss_text = Prompts.to_all_discuss.format(names=names_to_str(players.current_alive))
            _emit_web_event("phase_log", {"panel": "day", "text": discuss_text, "label": "发言环节"})
            await alive_players_hub.broadcast(
                await moderator(discuss_text),
            )
            # Open the auto broadcast to enable discussion
            alive_players_hub.set_auto_broadcast(True)

            for agent in players.current_alive:
                if not check_player_ready(agent):
                    continue
                
                # 强制发言：让智能体自由发言
                speak_prompt = await moderator(
                    f"[轮到你发言] {agent.name}，存活玩家：{names_to_str(players.current_alive)}，请发表你的分析和推理。",
                )
                await agent(speak_prompt)

            # Disable auto broadcast to avoid leaking info
            alive_players_hub.set_auto_broadcast(False)

            # 白天全员投票（保留平票处理）
            if not players.current_alive:
                continue

            vote_prompt_text = Prompts.to_all_vote.format(names_to_str(alive_player_names))
            _emit_web_event("phase_log", {"panel": "day", "text": vote_prompt_text, "label": "投票环节"})

            msgs_vote = await fanout_pipeline(
                agents=players.current_alive,
                msg=await moderator(vote_prompt_text),
                structured_model=get_vote_model(players.current_alive),
                enable_gather=True,
                strict_mode=True,       # 强制校验：不满足Literal枚举则不通过
                retry=3,                # 重试3次：校验失败后让AI重新生成，直到合法
                require_metadata=True,  # 强制要求返回metadata，必须包含vote字段
            )
            # 提取投票目标列表
            day_votes = []
            valid_vote_tuple = tuple(_.name for _ in players.current_alive if check_player_ready(_))
            for vote_msg in msgs_vote:
                if not vote_msg or not vote_msg.metadata:
                    continue
                vote_target = vote_msg.metadata.get("vote")
                if vote_target and vote_target in valid_vote_tuple:
                    day_votes.append(vote_target)
            
            # 首轮统计投票
            if not day_votes:
                print("本轮无有效投票，跳过白天淘汰")
                voted_player = None
                votes_stats = "无有效投票"
            else:
                vote_result, votes_stats, is_tie = majority_vote(
                    votes=day_votes,
                    alive_players=alive_player_names
                )
                voted_player = None

                if is_tie:
                    # vote_result is like "tie: Player3,Player8" — strip prefix
                    raw = vote_result.replace("tie:", "").strip() if isinstance(vote_result, str) else ""
                    tie_candidates = [c.strip() for c in raw.split(",") if c.strip() in alive_player_names]
                    if not tie_candidates:
                        voted_player = None
                        votes_stats = "平票但无有效候选人，本轮无淘汰"
                    else:
                        # 平票处理：平票候选人发言+非平票玩家重投
                        new_votes, is_re_tie = await handle_tie_vote(
                            tie_candidates=tie_candidates,
                            agents={agent.name: agent for agent in players.current_alive if check_player_ready(agent)},
                            state={
                                "current_alive": alive_player_names,
                                "_recent_msgs": [],
                                "game_phase": "voting"
                            }
                        )
                        if is_re_tie:
                            # 重投仍平票：白天无淘汰，直接进入天黑
                            voted_player = None
                            votes_stats = f"白天投票平票（{','.join(tie_candidates)}），重投仍平票，本轮无玩家淘汰，直接进入天黑"
                            await alive_players_hub.broadcast(
                                await moderator(votes_stats),
                            )
                        else:
                            # 重投非平票：确定淘汰目标
                            voted_player, re_vote_stats, _ = majority_vote(
                                votes=new_votes,
                                alive_players=alive_player_names,
                                tie_candidates=tie_candidates
                            )
                            votes_stats = f"首轮平票（{','.join(tie_candidates)}），重投结果：{re_vote_stats}，最终淘汰：{voted_player}"
                else:
                    # 非平票：直接确定淘汰目标
                    voted_player = vote_result
                    votes_stats = f"投票结果：{votes_stats}，最终淘汰：{voted_player}"

                # Broadcast the voting messages together
                res_text = Prompts.to_all_res.format(votes_stats, voted_player or "无")
                _emit_web_event("phase_log", {"panel": "day", "text": res_text, "label": "投票结果"})
                voting_msgs = [
                    *msgs_vote,
                    await moderator(res_text),
                ]

                # Leave a message if voted
                if voted_player and voted_player in players.name_to_agent:
                    dead_agent = players.name_to_agent[voted_player]
                    if check_player_ready(dead_agent):
                        lw_text = Prompts.to_dead_player.format(voted_player)
                        _emit_web_event("phase_log", {"panel": "day", "text": lw_text, "label": "遗言"})
                        prompt_msg = await moderator(lw_text)
                        last_msg = await dead_agent(prompt_msg)
                        voting_msgs.extend([prompt_msg, last_msg])

                await alive_players_hub.broadcast(voting_msgs)
                
                # If the voted player is the hunter, he can shoot someone
                shot_player_day = None
                if voted_player and players.hunter and len(players.hunter) > 0:
                    for agent in players.hunter:
                        if check_player_ready(agent) and voted_player == agent.name:
                            shot_player_day = await hunter_stage(agent, players)
                            if shot_player_day:
                                await alive_players_hub.broadcast(
                                    await moderator(
                                        Prompts.to_all_hunter_shoot.format(
                                            shot_player_day,
                                        ),
                                    ),
                                )

                # Update alive players (day)
                dead_today = [voted_player, shot_player_day]
                dead_today = [p for p in dead_today if p and p in alive_player_names]
                if dead_today:
                    print(f"白天死亡玩家：{dead_today}")
                    _emit_web_event("death", {"players": dead_today, "phase": "day"})
                    players.update_players(dead_today)
                    sync_alive_status(players)
                # Check winning
                res = players.check_winning()
                if res:
                    async with MsgHub(players.all_players) as all_players_hub:
                        res_msg = await moderator(res)
                        await all_players_hub.broadcast(res_msg)
                    _emit_web_event("game_over", {"result": res})
                    print()
                    # Reflection disabled
                    collect_game_data(agents, players, res, game_rounds)
                    return

        # 记录轮次信息
        round_data = {
            "round_number": round_idx + 1,
            "night_actions": {
                "killed_player": killed_player,
                "poisoned_player": poisoned_player,
                "shot_player": shot_player
            },
            "day_discussion": "",
            "voting": {
                "voted_player": voted_player,
                "votes_stats": votes_stats
            }
        }
        game_rounds.append(round_data)

        # The day ends
        first_day = False

    # Game over, each player reflects
    game_result = "游戏达到最大轮次，平局结束！"
    print(game_result)
    # 收集游戏数据
    collect_game_data(agents, players, game_result, game_rounds)
    # Reflection disabled