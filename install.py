"""从仓库根目录安装插件，并验证 Codex 已启用它。"""

import shutil
import sqlite3
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
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(root / 'plugin/app'), str(root / 'plugin/skills/dragon-memory/scripts')]
    from store import Store
    from updates import Updates

    try:
        version = Updates(root / 'plugin', Store()).install_local(root)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"安装未完成。{error}", file=sys.stderr)
        return 1

    print(f"「龙娘人设与记忆」{version} 已安装并启用，安装源已保存。")
    print("请打开新的 Codex 聊天，输入「打开龙娘人设与记忆管理面板」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

