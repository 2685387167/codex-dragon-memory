"""从正式发布的 GitHub Release 检查并安装插件更新。"""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
import urllib.request
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


REPOSITORY = '2685387167/codex-dragon-memory'
MARKETPLACE = 'dragon-local'
PLUGIN_ID = f'dragon-memory@{MARKETPLACE}'
MANIFEST = Path('.codex-plugin/plugin.json')
CATALOG = Path('.agents/plugins/marketplace.json')
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-dev\.([1-9][0-9]*))?')
SNAPSHOT = re.compile(r'(.+)-(?:[0-9a-f]{40}|[0-9a-f]{64})')
INCOMING = re.compile(r'\.incoming-[a-z0-9_]{8}')
UNSAFE_PATH = re.compile(r'[\\:\x00-\x1f<>|"*?]|^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)', re.I)
MAX_ARCHIVE = 16 * 1024 * 1024


def version_key(version):
    """正式版高于同版本号的开发版，比较时按数字分段。"""
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError('无法识别插件版本号。')
    major, minor, patch, dev = match.groups()
    return int(major), int(minor), int(patch), dev is None, int(dev or 0)


def fetch(url, limit):
    """从固定的官方仓库读取发布信息或按提交固定的压缩包。"""
    request = urllib.request.Request(url, headers={
        'User-Agent': 'codex-dragon-memory-updater',
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(request, timeout=6) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('GitHub 返回的更新文件超过大小限制。')
    return data


def github_api(path):
    """读取公开发布信息，无需 GitHub 登录或访问令牌。"""
    return json.loads(fetch(f'https://api.github.com/repos/{REPOSITORY}/{path}', 2 * 1024 * 1024))


def unpack_package(data, root, version):
    """只解包插件和市场清单，拒绝越界、重复路径及特殊文件。"""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(entries) > 1000 or sum(entry.file_size for entry in entries) > MAX_ARCHIVE:
            raise ValueError('更新包体积超过限制。')
        for entry in entries:
            _, _, relative = entry.filename.partition('/')
            if relative != CATALOG.as_posix() and not relative.startswith('plugin/'):
                continue
            parts = relative.split('/')
            if any(not part or part.endswith(('.', ' ')) or UNSAFE_PATH.search(part) for part in parts):
                raise ValueError('更新包含无效的文件路径。')
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if mode not in (0, stat.S_IFREG):
                raise ValueError('更新包含不受支持的文件类型。')
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination)
    validate_package(root, version)


def validate_package(root, version):
    """核对解包或已保存的快照仍属于预期插件和版本。"""
    manifest = json.loads((root / 'plugin' / MANIFEST).read_text(encoding='utf-8'))
    catalog = json.loads((root / CATALOG).read_text(encoding='utf-8'))
    if (manifest.get('name') != 'dragon-memory' or manifest.get('version') != version
            or catalog.get('name') != MARKETPLACE
            or not any(entry.get('name') == 'dragon-memory'
                       and entry.get('source') == {'source': 'local', 'path': './plugin'}
                       for entry in catalog.get('plugins', []))
            or not (root / 'plugin/.mcp.json').is_file()):
        raise ValueError('更新包与已确认的发布版本不一致。')


class Updates:
    """隔离网络检查、已确认的本机安装与最近十次更新记录。"""

    def __init__(self, plugin_root, store):
        """读取当前运行版本；更新记录保存在独立数据库。"""
        self.version = json.loads((plugin_root / MANIFEST).read_text(encoding='utf-8'))['version']
        self.installed = self.version
        self.home = store.home
        self.db_path = store.data_dir / 'updates.sqlite3'
        self.marketplace_cache = store.data_dir / 'marketplace-snapshots'
        self.lock = threading.Lock()
        self.checked_at = 0
        self.checking = False
        self.update = None
        self.releases = []
        self.error = None

    def state(self, refresh=False):
        """后台检查只在加载或打开页面时发起，重复请求共享结果。"""
        with self.lock:
            if refresh and not self.checking and (not self.checked_at or time.monotonic() - self.checked_at >= 4):
                self.checking = True
                threading.Thread(target=self.check, daemon=True).start()
            return {'current': self.installed, 'running': self.version, 'checking': self.checking,
                    'update': self.update, 'error': self.error}

    def check(self):
        """比较正式版并准备跨版本发布说明，尚不下载或安装代码。"""
        error = None
        update = None
        published = None
        installed = self.installed
        try:
            installed = self.installed_version() or self.version
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        try:
            official = {}
            for release in github_api('releases?per_page=10'):
                tag = release.get('tag_name', '')
                version = tag.removeprefix('v')
                match = VERSION.fullmatch(version)
                if not release.get('draft') and not release.get('prerelease') and match and match[4] is None:
                    official.setdefault(version, {'version': version, 'tag': tag,
                                                  'published_at': release.get('published_at'),
                                                  'notes': (release.get('body') or '此版本没有附发布说明。')[:4500]})
            published = sorted(official.values(), key=lambda item: version_key(item['version']), reverse=True)
            newer = [item for item in published if version_key(item['version']) > version_key(installed)]
            if newer:
                latest = newer[0]
                commit = github_api(f"commits/{latest['tag']}")['sha']
                if not re.fullmatch(r'[0-9a-f]{40}', commit):
                    raise ValueError('GitHub 返回了无效的发布提交。')
                update = {'version': latest['version'], 'commit': commit, 'releases': newer}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            error = '暂时无法检查 GitHub 更新，请稍后重试。'
        with self.lock:
            self.installed, self.update, self.error = installed, update, error
            if published is not None:
                self.releases = published
            self.checked_at = time.monotonic()
            self.checking = False

    def codex_command(self, *args):
        """定位当前客户端的 Codex 命令。"""
        codex = shutil.which('codex')
        if not codex:
            raise ValueError('找不到 Codex CLI，无法安装更新。')
        return [codex, *args]

    def cli(self, *args):
        """使用 Codex 自带安装命令，沿用当前配置目录和插件标识。"""
        try:
            result = subprocess.run(
                self.codex_command('plugin', *args, '--json'), check=True, capture_output=True, timeout=14,
                encoding='utf-8', env={**os.environ, 'CODEX_HOME': str(self.home)},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError('Codex 未完成更新安装，请重试。') from error
        return json.loads(result.stdout)

    def installed_version(self):
        """读取当前实际启用的版本，避免其他窗口重复安装或降级。"""
        entries = self.cli('list', '--marketplace', MARKETPLACE).get('installed', [])
        return next((entry['version'] for entry in entries
                     if entry.get('pluginId') == PLUGIN_ID and entry.get('enabled')), None)

    def marketplace_source(self, *, allow_missing=False):
        """读取已登记的本地安装源，避免迁移其他插件或管理策略。"""
        path = self.home / 'config.toml'
        config = tomllib.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        entry = config.get('marketplaces', {}).get(MARKETPLACE, {})
        if not entry and allow_missing:
            return None
        source = entry.get('source')
        if entry.get('source_type') != 'local' or not isinstance(source, str) or not Path(source).is_absolute():
            raise ValueError('当前插件由非本地市场管理，请通过原市场更新。')
        catalog = Path(source) / CATALOG
        if catalog.is_file():
            plugins = json.loads(catalog.read_text(encoding='utf-8')).get('plugins', [])
            if [plugin.get('name') for plugin in plugins] != ['dragon-memory']:
                raise ValueError('当前市场同时管理其他插件，请通过原市场更新。')
        return source

    def set_marketplace_source(self, source):
        """通过 Codex 配置接口持久保存单个市场的路径。"""
        try:
            with subprocess.Popen(
                self.codex_command('app-server', '--listen', 'stdio://'), cwd=self.home,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding='utf-8', env={**os.environ, 'CODEX_HOME': str(self.home)},
            ) as process:
                deadline = threading.Timer(10, process.kill)
                deadline.start()

                def request(message, response_id=None):
                    """发送一条协议消息，并等待指定响应。"""
                    process.stdin.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
                    process.stdin.flush()
                    if response_id is None:
                        return
                    for line in process.stdout:
                        reply = json.loads(line)
                        if reply.get('id') == response_id:
                            if 'error' in reply:
                                break
                            return
                    raise ValueError('Codex 未确认已保存插件安装源。')

                try:
                    request({'id': 1, 'method': 'initialize', 'params': {
                        'clientInfo': {'name': 'dragon-memory-updater', 'version': self.version},
                    }}, 1)
                    request({'method': 'initialized'})
                    request({'id': 2, 'method': 'config/value/write', 'params': {
                        'keyPath': f'marketplaces.{MARKETPLACE}.source',
                        'value': source, 'mergeStrategy': 'replace',
                    }}, 2)
                finally:
                    deadline.cancel()
                    process.stdin.close()
                    if process.poll() is None:
                        process.terminate()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise ValueError('Codex 未能保存插件安装源。') from error
        if Path(self.marketplace_source()).resolve() != Path(source).resolve():
            raise ValueError('Codex 保存的插件安装源与安装包不一致。')

    def stage_snapshot(self, version, identity, populate):
        """在当前安装事务内生成新目录，验证完整后再启用。"""
        self.marketplace_cache.mkdir(parents=True, exist_ok=True)
        destination = self.marketplace_cache / f'{version}-{identity}'
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=self.marketplace_cache, prefix='.incoming-') as temp:
                staged = Path(temp) / 'snapshot'
                populate(staged)
                validate_package(staged, version)
                staged.rename(destination)
        validate_package(destination, version)
        return destination

    def save_snapshot(self, commit, version):
        """下载固定提交并把发布包保存为 Codex 的本地安装源。"""
        try:
            archive = fetch(f'https://codeload.github.com/{REPOSITORY}/zip/{commit}', MAX_ARCHIVE)
            return self.stage_snapshot(version, commit, lambda staged: unpack_package(archive, staged, version))
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise ValueError('无法读取或保存 GitHub 更新包。') from error

    def install_local(self, package_root):
        """首次安装以及从旧更新器迁移时，使用稳定的本地安装源。"""
        version = self.version
        validate_package(package_root, version)
        paths = [CATALOG, *sorted(file.relative_to(package_root)
                                 for file in (package_root / 'plugin').rglob('*')
                                 if file.is_file() and '__pycache__' not in file.parts
                                 and file.suffix not in ('.pyc', '.pyo'))]
        digest = hashlib.sha256()
        for relative in paths:
            digest.update(relative.as_posix().encode('utf-8') + b'\0')
            digest.update(hashlib.sha256((package_root / relative).read_bytes()).digest())

        def copy_package(staged):
            """复制发布包的清单和插件文件，旧版源码与数据留在原处。"""
            for relative in paths:
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(package_root / relative, target)

        self.home.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.installed_version()
            if previous and version_key(previous) > version_key(version):
                return previous
            destination = self.stage_snapshot(version, digest.hexdigest(), copy_package)
            if self.marketplace_source(allow_missing=True) is None:
                self.cli('marketplace', 'add', str(destination))
                self.cli('add', PLUGIN_ID)
                if self.installed_version() != version:
                    raise ValueError('Codex 尚未确认新版本已启用。')
            else:
                self.activate_marketplace(destination, version)
            self.cleanup_snapshots(destination)
        return version

    def cleanup_snapshots(self, active):
        """只清理本插件生成的旧快照和未完成的暂存目录。"""
        parent = self.marketplace_cache.resolve()
        active = active.resolve()
        try:
            if (parent != self.db_path.parent.resolve() / 'marketplace-snapshots'
                    or active.parent != parent or not Path(self.marketplace_source()).samefile(active)):
                return
            candidates = list(parent.iterdir())
        except (OSError, ValueError):
            return
        for item in candidates:
            match = SNAPSHOT.fullmatch(item.name)
            if not (INCOMING.fullmatch(item.name) or (match and VERSION.fullmatch(match[1]))):
                continue
            try:
                if item == active or not item.is_dir() or item.resolve() != item:
                    continue
                if match:
                    validate_package(item, match[1])
                shutil.rmtree(item)
            except (OSError, ValueError, TypeError, AttributeError):
                # Windows 或其他窗口尚在使用时，留待下次安装再清理。
                continue

    def activate_marketplace(self, destination, version):
        """切换持久安装源并核对安装，失败时恢复原来源。"""
        previous_source = self.marketplace_source()
        desired_source = str(destination.resolve())
        try:
            self.set_marketplace_source(desired_source)
            if self.installed_version() != version:
                self.cli('add', PLUGIN_ID)
            if self.installed_version() != version:
                raise ValueError('Codex 尚未确认新版本已启用。')
        except (OSError, ValueError, subprocess.SubprocessError):
            current = Path(self.marketplace_source()).resolve()
            if current == Path(desired_source) and current != Path(previous_source).resolve():
                self.set_marketplace_source(previous_source)
            raise

    def connect(self):
        """记录安装结果，更新时只锁更新记录库。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS updates (
                version TEXT PRIMARY KEY, previous TEXT NOT NULL,
                installed_at TEXT NOT NULL, notes TEXT NOT NULL
            )''')
        except BaseException:
            db.close()
            raise
        return db

    def history(self):
        """以最新优先顺序返回实际安装的版本记录。"""
        if not self.db_path.exists():
            return []
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM updates ORDER BY installed_at DESC LIMIT 10')
            return [{**row, 'notes': json.loads(row['notes'])} for row in rows]

    def release_history(self):
        """每次正式发布展示一条；离线时仍可回看随安装保存的说明。"""
        with self.lock:
            releases = {item['version']: dict(item) for item in self.releases}
        for update in self.history():
            for release in update['notes']:
                releases.setdefault(release['version'], dict(release))
            releases[update['version']]['installed_at'] = update['installed_at']
        versions = sorted(releases, key=version_key, reverse=True)
        return [releases[version] for version in versions[:10]]

    def install(self, commit):
        """确认后按已展示的提交安装，可从任一旧版直接升级。"""
        with self.lock:
            update = self.update
        if not update or commit != update['commit']:
            raise ValueError('更新已变化，请重新检查并确认。')
        version = update['version']
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = self.installed_version()
            if previous is None:
                raise ValueError('Codex 尚未启用此插件，请先从插件页启用。')
            if version_key(previous) < version_key(version):
                snapshot = self.save_snapshot(commit, version)
                self.activate_marketplace(snapshot, version)
                db.execute('INSERT OR REPLACE INTO updates VALUES (?, ?, ?, ?)',
                           (version, previous, datetime.now(UTC).isoformat(), json.dumps(update['releases'], ensure_ascii=False)))
                db.execute('DELETE FROM updates WHERE version NOT IN (SELECT version FROM updates ORDER BY installed_at DESC LIMIT 10)')
                self.cleanup_snapshots(snapshot)
            else:
                version = previous
        with self.lock:
            self.installed, self.update, self.error = version, None, None
        return {'current': version, 'running': self.version}
