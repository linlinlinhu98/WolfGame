# 🐺 狼人杀 · 九人局

9人狼人杀多智能体系统，基于 DeepSeek LLM 驱动。

**角色**: 3狼人 🐺 · 3村民 👨‍🌾 · 1预言家 🔮 · 1女巫 🧙 · 1猎人 🏹

## 特性

- **认知架构**: 感知 → 记忆(工作+情景+信念) → 推理(策略+心智理论+发言) → 行动
- **压缩记忆**: SpeechSummary 正则提取，无需额外 LLM 调用
- **反幻觉**: 事实清单 + 首尾重复(User-shaped attention)
- **内部/公开分离**: 两阶段推理（内部策略 → 公开发言）
- **Web 可视化**: 上帝模式(观战9 AI) + 玩家模式(1人类+8 AI)
- **SSE 实时流**: 回合组织、阶段标签、玩家状态网格

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置 DeepSeek API Key
set DEEPSEEK_API_KEY=sk-your-key-here        # Windows
# export DEEPSEEK_API_KEY=sk-your-key-here    # Mac/Linux

# 3. 启动 Web 平台（推荐）
python server.py

# 或命令行模式
python main.py          # 上帝模式（9 AI 对战）
python main.py player   # 玩家模式（你在终端中操作）
```

Web 平台: http://localhost:5000

## 项目结构

```
agent.py              # PlayerAgent — 认知架构核心
game.py               # 游戏主循环
memory.py             # 记忆系统（SpeechSummary, EpisodicMemory, Personas）
reasoning.py          # 推理引擎（WorkingMemory, BeliefTracker）
prompt.py             # 中/英文提示词模板
structured_model.py   # Pydantic 结构化输出模型
utils.py              # 工具函数（投票、平票处理）
_vendor.py            # 独立运行的 API 适配层（无 agentscope 依赖）
human_agent.py        # 终端版人类玩家
main.py               # 命令行入口
server.py             # Web 入口
web_ui/               # Web 前端
  server.py           # Flask + SSE 后端
  web_human.py        # Web 人类玩家适配器
  templates/index.html
  static/game.js
  static/style.css
```

## 依赖

`openai`, `numpy`, `shortuuid`, `flask`, `pydantic`

项目已完全独立，无需安装 agentscope 框架。
