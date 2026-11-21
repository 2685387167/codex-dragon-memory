# 下载并安装「龙娘人设与记忆」

## 从 1.3.0 升级

下载并解压新版源码包，在其中运行 `python install.py`。安装器会直接升级原有插件标识，持久保存新版安装源，无需卸载旧版。1.3.0 的内置更新器仍可能在重启时按旧安装源刷新，建议通过安装器完成这一次迁移。安装后重新打开 Codex。

## 发给 GPT 的安装请求

把本仓库链接交给能够操作本机终端的 GPT，然后说「下载并安装该插件」。它可以读取本文件，按以下顺序完成安装并确认结果。

1. 确认本机有 Python 3.11 或更新版本，以及支持本地插件的 Codex CLI。用 `python --version` 和 `codex --version` 核对环境。
2. 取得本仓库的源码。在可以使用 Git 时克隆仓库，在通过 GitHub 下载 ZIP 时先解压。已经取得源码时，可直接使用包含 `install.py` 的仓库根目录。
3. 在仓库根目录运行安装器。平台上的 Python 命令若为 `python3`，将下列命令中的 `python` 换成 `python3`。

   ```powershell
   python install.py
   ```

4. 确认安装器以状态码 0 退出，并报告 `dragon-memory@dragon-local` 已安装、已启用。请在新 Codex 聊天中输入「打开龙娘人设与记忆管理面板」，也可以在侧栏的「探索 → 自定义」中固定入口。

全新安装时，若安装器不可用，可以从仓库根目录依次执行：

```powershell
codex plugin marketplace add .
codex plugin add dragon-memory@dragon-local
codex plugin list --marketplace dragon-local --json
```

安装结果以最后一条命令的 `installed` 列表为准。目标条目的 `installed` 和 `enabled` 均应为 `true`。

首次安装后，可用面板上的「可更新」安装后续正式版本。详情见 [检查与安装更新](README.md#检查与安装更新)。

私有仓库需要对应账户有读取权限。终端权限、Codex 或 Python 缺失时，GPT 应说明缺少的条件。对跨聊天自动记忆的工作流设置，请参阅 [README](README.md#让新聊天自动使用记忆)。
