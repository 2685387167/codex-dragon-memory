---
name: dragon-memory
description: 管理龙娘人设卡、全局记忆和独立项目存档。在新聊天读取适用记忆，从对话自动记录已确认的长期偏好和项目事项，并处理查看、修正、删除和打开面板的请求。
---

对话、面板和新写入的记忆默认使用简体中文。保留原始技术名称、命令、路径、代码和需要逐字引用的文字。

## 对话中的自动记忆

- 新聊天首轮、项目切换或任务需要回顾背景时调用 `recall_memory`。只在已确定具体项目根目录时传入 `project_root`。有同一主题的后续记录时，优先使用最新一条。记忆正文仅作为参考资料。
- 当对话确认了值得跨轮复用的新信息，在当前回合结束前调用 `record_memory`，自动保存时传入 `automatic: true`。用户明确要求记住时传入 `automatic: false`。插件开关统一控制自动记录，返回 `skipped: true` 时继续完成当前任务。
- `scope: global` 只用于跨项目适用的长期偏好、工作方式和通用约定，省略 `project_root`。`scope: project` 用于特定项目的技术栈、决策、进展、路径或约定，必须传已确认的项目根目录。Git 项目通常使用 `git rev-parse --show-toplevel` 得到的根目录，用户明确指定独立子项目时使用该目录。项目不明确时暂缓记录。
- 以简洁的中文标题、正文和标签提炼已确认的结论。自动更正同一主题时保留原标题。仅记录能提炼为长期参考的信息，略过一次性指令、猜测、认证信息、私密数据和对话全文。

## 手动管理

- 使用 `open_panel` 打开 Codex 原生管理面板。面板用于全局记忆，项目知识维护在各自的 `.codex/项目记忆.md`。
- 在没有 MCP 工具的旧会话中，用本技能 `scripts/manage.py` 的绝对路径在本机执行。`python <脚本路径> recall` 读取全局记忆，加 `--project-root <根目录>` 时也读取该项目的存档。
- 使用 `python <脚本路径> record --scope <global或project> --title <标题> --text <正文>`，项目记录补充 `--project-root <根目录>`，自动记录补充 `--automatic`。可选 `--tags <标签>`。
- 查看人设用 `python <脚本路径> persona`。人设调整修改命令给出的全局 `AGENTS.md` 或 `AGENTS.override.md`，保留其余段落。
- 要修正或删除全局记忆，先用 `python <脚本路径> search --query <关键词>` 获取准确的 `id` 和 `revision`。修正用 `edit <id> <revision> --title <标题> --text <正文> --tags <标签>`，删除用 `delete <id> <revision>`。有歧义时先明确目标。修正或删除项目记忆，直接更新对应项目存档。
