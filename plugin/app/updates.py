"""从正式发布的 GitHub Release 检查并安装插件更新。"""

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

    def cli(self, *args):
        """使用 Codex 自带安装命令，沿用当前配置目录和插件标识。"""
        codex = shutil.which('codex')
        if not codex:
            raise ValueError('找不到 Codex CLI，无法安装更新。')
        try:
            result = subprocess.run(
                [codex, 'plugin', *args, '--json'], check=True, capture_output=True, timeout=14,
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
                try:
                    archive = fetch(f'https://codeload.github.com/{REPOSITORY}/zip/{commit}', MAX_ARCHIVE)
                    with tempfile.TemporaryDirectory(prefix='dragon-memory-update-') as temp:
                        unpack_package(archive, Path(temp), version)
                        # 本次安装临时覆盖市场来源，保留用户原来的市场配置。
                        source = json.dumps(Path(temp).as_posix(), ensure_ascii=False)
                        self.cli('add', PLUGIN_ID, '-c', f'marketplaces.{MARKETPLACE}.source={source}')
                except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                    raise ValueError('无法读取或安装 GitHub 更新包。') from error
                if self.installed_version() != version:
                    raise ValueError('Codex 尚未确认新版本已启用。')
                db.execute('INSERT OR REPLACE INTO updates VALUES (?, ?, ?, ?)',
                           (version, previous, datetime.now(UTC).isoformat(), json.dumps(update['releases'], ensure_ascii=False)))
                db.execute('DELETE FROM updates WHERE version NOT IN (SELECT version FROM updates ORDER BY installed_at DESC LIMIT 10)')
            else:
                version = previous
        with self.lock:
            self.installed, self.update, self.error = version, None, None
        return {'current': version, 'running': self.version}
