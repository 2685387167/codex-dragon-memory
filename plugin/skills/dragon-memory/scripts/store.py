"""龙娘面板和 Codex 技能共用的人设与长期记忆存储。"""

import hashlib
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


# 全局库拒绝明显依赖当前项目的记录，范围判断仍以对话中的实际语义为准。
PROJECT_REFERENCE = re.compile(r'(?:本|当前|这个|该)(?:项目|仓库|代码库)|\b(?:this|current)\s+(?:project|repo(?:sitory)?)\b', re.I)


def codex_home():
    """与 Codex 使用同一个本地主目录。"""
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def replace_bytes(path, content):
    """先完整写出临时文件，再在同一文件系统上替换目标。"""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        staging = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            staging.unlink(missing_ok=True)
            raise
    try:
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def memory_fields(title, body, tags):
    """校验并规范化记忆标题、正文和标签，保留原文的技术术语。"""
    title = ' '.join(title.split())
    body = body.strip().replace('\r\n', '\n').replace('\r', '\n')
    tags = '、'.join(dict.fromkeys(
        ' '.join(tag.split()) for tag in re.split(r'[,，、]', tags) if tag.strip()
    ))
    if not title or not body:
        raise ValueError('请填写记忆的标题和正文。')
    if len(title) > 120 or len(body) > 20000 or len(tags) > 400:
        raise ValueError('标题最多 120 字、正文最多 20,000 字、标签最多 400 字。')
    return title, body, tags


class Store:
    """人设使用现有全局文件，记忆采用可并发读写的本地数据库。"""

    def __init__(self, home=None):
        self.home = Path(home) if home is not None else codex_home()
        self.data_dir = self.home / 'dragon-memory'
        self.database = self.data_dir / 'memory.sqlite3'

    @property
    def persona_path(self):
        """Codex 优先读取同目录中非空的人设覆盖文件。"""
        override = self.home / 'AGENTS.override.md'
        if override.is_file() and override.read_text(encoding='utf-8-sig').strip():
            return override
        return self.home / 'AGENTS.md'

    def read_persona(self):
        """保留原始字节作为版本标记，以便检测外部修改和保留换行符。"""
        path = self.persona_path
        data = path.read_bytes() if path.is_file() else b''
        return path, data

    def save_persona(self, expected_path, expected, text):
        """更新当前生效的人设文件，备份上一版本并保留其编码和换行。"""
        path, current = self.read_persona()
        if (path, current) != (expected_path, expected):
            raise ValueError('人设卡已在别处更新。请先复制你的修改，重新加载后再保存。')
        if not text.strip():
            raise ValueError('请填写人设卡内容。')
        raw = text.replace('\r\n', '\n').replace('\r', '\n')
        if b'\r\n' in current:
            raw = raw.replace('\n', '\r\n')
        encoding = 'utf-8-sig' if current.startswith(b'\xef\xbb\xbf') else 'utf-8'
        data = raw.encode(encoding)
        if current == data:
            return path, data
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if current:
            replace_bytes(self.data_dir / f'{path.name}.bak', current)
        replace_bytes(path, data)
        return path, data

    def connect(self):
        """创建新库或迁移旧库，原有记忆继续保留为手动记录。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA user_version').fetchone()[0] < 1:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('''
                        CREATE TABLE IF NOT EXISTS memory (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            title TEXT NOT NULL,
                            body TEXT NOT NULL,
                            tags TEXT NOT NULL,
                            updated TEXT NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 1,
                            origin TEXT NOT NULL DEFAULT 'manual'
                        )
                    ''')
                    if 'origin' not in {column['name'] for column in db.execute('PRAGMA table_info(memory)')}:
                        db.execute("ALTER TABLE memory ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
                    db.execute('''
                        CREATE TABLE IF NOT EXISTS settings (
                            name TEXT PRIMARY KEY,
                            enabled INTEGER NOT NULL
                        )
                    ''')
                    db.execute('PRAGMA user_version = 1')
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def auto_enabled(db):
        """读取同一数据库事务中的自动记忆开关，默认开启。"""
        row = db.execute("SELECT enabled FROM settings WHERE name = 'auto_memory'").fetchone()
        return row is None or bool(row['enabled'])

    def automatic(self, enabled=None):
        """读取或持久化自动采集开关。手动记录始终可以保存。"""
        with closing(self.connect()) as db, db:
            if enabled is None:
                return self.auto_enabled(db)
            db.execute("INSERT OR REPLACE INTO settings VALUES ('auto_memory', ?)", (enabled,))
            return enabled

    def search(self, query=''):
        """标题、正文和标签均可检索，多个关键词必须同时命中。"""
        terms = query.casefold().split()
        with closing(self.connect()) as db:
            rows = db.execute('SELECT * FROM memory ORDER BY updated DESC, id DESC')
            return [dict(row) for row in rows if all(
                term in ' '.join((row['title'], row['body'], row['tags'])).casefold()
                for term in terms
            )]

    def save_memory(self, title, body, tags='', *, key=None, revision=None, automatic=False):
        """存入全局记忆。自动记录按主题去重，并保留用户手工编辑的版本。"""
        title, body, tags = memory_fields(title, body, tags)
        if any(PROJECT_REFERENCE.search(value) for value in (title, body, tags)):
            raise ValueError('这条内容涉及特定项目，请存档到对应项目。')
        updated = datetime.now(UTC).isoformat(timespec='microseconds')
        origin = 'automatic' if automatic else 'manual'
        with closing(self.connect()) as db, db:
            if automatic:
                db.execute('BEGIN IMMEDIATE')
                if not self.auto_enabled(db):
                    return None
                previous = db.execute('''
                    SELECT * FROM memory WHERE title = ? COLLATE NOCASE
                    ORDER BY origin = 'manual' DESC, id DESC LIMIT 1
                ''', (title,)).fetchone()
                if previous:
                    if previous['origin'] == 'manual' or previous['body'] == body:
                        return dict(previous)
                    key, revision = previous['id'], previous['revision']
            values = (title, body, tags, updated, origin)
            if key is None:
                result = db.execute(
                    'INSERT INTO memory (title, body, tags, updated, origin) VALUES (?, ?, ?, ?, ?)', values)
                key = result.lastrowid
            else:
                result = db.execute('''
                    UPDATE memory SET title = ?, body = ?, tags = ?, updated = ?, origin = ?, revision = revision + 1
                    WHERE id = ? AND revision = ?
                ''', (*values, key, revision))
                if not result.rowcount:
                    raise ValueError('这条记忆已被修改或删除。请重新加载后再保存。')
            return dict(db.execute('SELECT * FROM memory WHERE id = ?', (key,)).fetchone())

    def delete_memory(self, key, revision):
        """仅删除已核对过的那一版记忆。"""
        with closing(self.connect()) as db, db:
            result = db.execute('DELETE FROM memory WHERE id = ? AND revision = ?', (key, revision))
            if not result.rowcount:
                raise ValueError('这条记忆已被修改或删除。请重新加载后再操作。')

    def recall(self, query='', project_root=None):
        """向聊天提供有界的全局记忆和当前项目存档。"""
        notes = self.search(query)[:8]
        for note in notes:
            if len(note['body']) > 800:
                note['body'] = note['body'][:800] + '…'
        return {'automatic': self.automatic(), 'global': notes,
                'project': self.project_context(project_root) if project_root else None}

    def record(self, scope, title, body, tags='', *, project_root=None, automatic=False):
        """要求调用方明确范围。项目根目录不明时保留在本轮对话中。"""
        if scope == 'global' and project_root is None:
            item = self.save_memory(title, body, tags, automatic=automatic)
            return {'scope': scope, 'item': item, 'skipped': item is None}
        if scope == 'project' and project_root:
            path = self.archive_project(project_root, title, body, tags, automatic=automatic)
            return {'scope': scope, 'path': str(path) if path else None, 'skipped': path is None}
        raise ValueError('请明确选择全局记忆或项目存档。项目存档需要已确认的根目录。')

    def project_archive(self, project_root):
        """验证已确认的项目根目录，防止项目文件通过链接落到其他目录。"""
        root = Path(project_root)
        if not root.is_absolute():
            raise ValueError('请提供已确认的项目根目录绝对路径。')
        root = root.resolve(strict=True)
        home = self.home.resolve()
        if not root.is_dir() or root in (root.parent, Path.home().resolve(), home) or root.is_relative_to(home):
            raise ValueError('请使用实际项目根目录。')
        archive = root / '.codex' / '项目记忆.md'
        if archive.resolve() != archive:
            raise ValueError('项目记忆存档路径指向了其他位置。')
        return archive

    def archive_project(self, project_root, title, body, tags='', *, automatic=False):
        """将项目记录直接追加到该项目的 Markdown 存档，并幂等处理重试。"""
        path = self.project_archive(project_root)
        title, body, tags = memory_fields(title, body, tags)
        digest = hashlib.sha256(f'{title}\0{body}'.encode('utf-8')).hexdigest()
        marker = f'<!-- dragon-memory:{digest} -->'
        # 与本机其他插件会话串行提交，事务内不会写入任何项目内容到全局库。
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if automatic and not self.auto_enabled(db):
                return None
            previous = path.read_bytes() if path.is_file() else '# 项目记忆\n'.encode('utf-8')
            heading = f'## {title}'
            latest = previous.rfind(f'\n{heading}\n'.encode('utf-8'))
            same_as_latest = latest >= 0 and previous[:latest].rsplit(b'\n', 1)[-1] == marker.encode('ascii')
            if not same_as_latest:
                entry = [marker, heading, '', *[f'> {line}' for line in body.split('\n')]]
                if tags:
                    entry.extend(['', f'标签：{tags}'])
                path.parent.mkdir(parents=True, exist_ok=True)
                replace_bytes(path, previous.rstrip(b'\r\n') + ('\n\n' + '\n'.join(entry) + '\n').encode('utf-8'))
        return path

    def project_context(self, project_root):
        """只读取指定项目的存档，优先提供靠后的记录。"""
        path = self.project_archive(project_root)
        if not path.is_file():
            return None
        with path.open('rb') as stream:
            stream.seek(max(path.stat().st_size - 12000, 0))
            content = stream.read(12000).decode('utf-8', errors='ignore')
        return {'path': str(path), 'text': '\n'.join(
            line for line in content.splitlines() if not line.startswith('<!-- dragon-memory:')
        ).strip()}
