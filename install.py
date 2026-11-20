"""从仓库根目录安装插件，并验证 Codex 已启用它。"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    """使用本机 Codex CLI 完成市场注册、安装和状态核对。"""
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更新版本。", file=sys.stderr)
        return 1

    codex = shutil.which("codex")
    if codex is None:
        print("找不到 Codex CLI，请安装后确认终端可以运行 codex。", file=sys.stderr)
        return 1

    root = Path(__file__).resolve().parent
    marketplace_name = "dragon-local"
    plugin_id = "dragon-memory@dragon-local"

    try:
        # 从当前检出的副本安装，避免依赖固定盘符或账户目录。
        subprocess.run([codex, "plugin", "marketplace", "add", str(root)], cwd=root, check=True)
        subprocess.run([codex, "plugin", "add", plugin_id], cwd=root, check=True)
        result = subprocess.run(
            [codex, "plugin", "list", "--marketplace", marketplace_name, "--json"],
            cwd=root, check=True, capture_output=True, encoding="utf-8",
        )
        installed = json.loads(result.stdout).get("installed", [])
        plugin = next((entry for entry in installed
                       if entry.get("pluginId") == plugin_id and entry.get("enabled") is True), None)
        if plugin is None:
            print("安装命令已运行，Codex 尚未报告插件已启用。", file=sys.stderr)
            return 1
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"安装未完成。{error}", file=sys.stderr)
        return 1

    print(f"「龙娘人设与记忆」{plugin.get('version', '')} 已安装并启用。")
    print("请打开新的 Codex 聊天，输入「打开龙娘人设与记忆管理面板」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

