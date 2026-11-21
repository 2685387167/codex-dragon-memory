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
MEMORY_WORDS = re.compile(r'[\u3400-\u9fff]|[^\W_]+')


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


def global_memory_fields(title, body, tags):
    """规范化记忆内容，拒绝明显属于特定项目的全局条目。"""
    fields = memory_fields(title, body, tags)
    if any(PROJECT_REFERENCE.search(value) for value in fields):
        raise ValueError('这条内容涉及特定项目，请存档到对应项目。')
    return fields


def memory_terms(text):
    """提取词和相邻字组，为中文及混合语言的候选条目排序。"""
    words = MEMORY_WORDS.findall(text.casefold())
    return {word for word in words if len(word) > 1} | {
        left + right for left, right in zip(words, words[1:])
    }


def fixed_memory(row):
    """只按完整标签识别用户固定的记忆。"""
    return '用户固定' in {tag.strip() for tag in re.split(r'[,，、]', row['tags'])}


def review_memories(rows, title, body, tags, review_token):
    """提供候选条目，由当前对话判断语义关系并整理最终正文。"""
    terms = memory_terms(' '.join((title, body, tags)))
    ranked = sorted(rows, key=lambda row: (
        row['title'].casefold() == title.casefold(),
        len(terms & memory_terms(row['title'])) * 2
        + len(terms & memory_terms(' '.join((row['body'][:1500], row['tags'])))),
    ), reverse=True)
    candidates = [
        {**row, 'body': row['body'][:600], 'body_truncated': len(row['body']) > 600, 'fixed': fixed_memory(row)}
        for row in ranked[:16]
    ]
    return {'scope': 'global', 'action': 'review', 'review_token': review_token, 'candidates': candidates,
            'instruction': ('尚未写入。先比较候选的适用场景和具体偏好。'
                            '同一偏好保留仍适用的旧细节并写入新细节；明确更正只替换对应部分。'
                            '原标题仍准确时沿用，标题中含被更正的细节时同步改成准确的主题标题。'
                            '仅将确属同一条的自动记录 ID 按保留顺序填入 merge_ids，正文被截断时先按 ID 读全文。'
                            '独立事项使用不同标题和空数组。手动或用户固定条目不能自动更改，内容已涵盖时无需写入。'
                            '用户固定条目与新信息冲突时，不另存一条来覆盖它；由用户要求修改并确认后手动更新。')}


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

    def save_memory(self, title, body, tags='', *, key=None, revision=None, confirm_fixed=False):
        """从管理面板或手动命令保存全局记忆，编辑时校验条目版本。"""
        fields = global_memory_fields(title, body, tags)
        with closing(self.connect()) as db, db:
            if key is not None:
                db.execute('BEGIN IMMEDIATE')
                self.check_fixed(db, key, revision, confirm_fixed)
            return self.write_memory(db, fields, 'manual', key=key, revision=revision)

    @staticmethod
    def check_fixed(db, key, revision, confirmed):
        """校验将要编辑或删除的版本，防止确认后误改其他版本。"""
        row = db.execute('SELECT tags FROM memory WHERE id = ? AND revision = ?', (key, revision)).fetchone()
        if row and fixed_memory(row) and not confirmed:
            raise ValueError('这条记忆带有「用户固定」标签，请先让用户确认本次修改。')

    @staticmethod
    def write_memory(db, fields, origin, *, key=None, revision=None):
        """在调用方事务内统一写入条目，并返回实际保存的版本。"""
        updated = datetime.now(UTC).isoformat(timespec='microseconds')
        values = (*fields, updated, origin)
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

    def record_global(self, title, body, tags, *, review_token=None, merge_ids=None):
        """有旧条目时先返回候选；提交时核对版本，并在一个事务内合并。"""
        fields = global_memory_fields(title, body, tags)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not self.auto_enabled(db):
                return {'scope': 'global', 'item': None, 'skipped': True}
            rows = [dict(row) for row in db.execute('SELECT * FROM memory ORDER BY updated DESC, id DESC')]
            # 任一条目发生新增、编辑或删除，旧的比较结果都需要重新核对。
            versions = ','.join(f"{row['id']}:{row['revision']}" for row in rows)
            current = hashlib.sha256(versions.encode('ascii')).hexdigest()
            if (rows or review_token is not None) and (review_token != current or merge_ids is None):
                return review_memories(rows, *fields, current)
            merge_ids = list(dict.fromkeys(merge_ids or ()))
            by_id = {row['id']: row for row in rows}
            if any(key not in by_id for key in merge_ids):
                raise ValueError('所选记忆已不存在，请重新获取候选条目。')
            related = [by_id[key] for key in merge_ids]
            if any(fixed_memory(row) for row in related):
                raise ValueError('「用户固定」记忆不能自动修改或合并。')
            if any(row['origin'] == 'manual' for row in related):
                raise ValueError('手动条目须经手动编辑。已涵盖的内容可跳过，补充事项请使用独立标题。')
            # 合并沿用首条的 ID；标题可随更正更新，避免与新正文冲突。
            kept = related[0] if related else None
            final_title = fields[0]
            if any(row['title'].casefold() == final_title.casefold() and row['id'] not in merge_ids for row in rows):
                raise ValueError('存在未纳入比较的同标题条目，请核对候选或为独立事项换一个标题。')
            if kept:
                all_tags = '、'.join(row['tags'] for row in related if row['tags'])
                merged_tags = memory_fields(final_title, fields[1], '、'.join((all_tags, fields[2])))[2]
                updated = (final_title, fields[1], merged_tags)
                item = kept if (kept['title'], kept['body'], kept['tags']) == updated else self.write_memory(
                    db, updated, 'automatic', key=kept['id'], revision=kept['revision'])
                db.executemany('DELETE FROM memory WHERE id = ?', ((key,) for key in merge_ids[1:]))
            else:
                item = self.write_memory(db, fields, 'automatic')
            return {'scope': 'global', 'item': item, 'skipped': False}

    def delete_memory(self, key, revision, *, confirm_fixed=False):
        """仅删除已核对过的那一版记忆。"""
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.check_fixed(db, key, revision, confirm_fixed)
            result = db.execute('DELETE FROM memory WHERE id = ? AND revision = ?', (key, revision))
            if not result.rowcount:
                raise ValueError('这条记忆已被修改或删除。请重新加载后再操作。')

    def recall(self, query='', project_root=None, memory_ids=None):
        """提供适用记忆；指定 ID 时读取候选条目的完整正文。"""
        if memory_ids is not None:
            if not 1 <= len(memory_ids) <= 5:
                raise ValueError('一次可按 ID 读取 1 到 5 条全局记忆。')
            wanted = set(memory_ids)
            notes = [note for note in self.search() if note['id'] in wanted]
        else:
            notes = self.search(query)[:8]
            for note in notes:
                if len(note['body']) > 800:
                    note['body'] = note['body'][:800] + '…'
        return {'automatic': self.automatic(), 'global': notes,
                'project': self.project_context(project_root) if project_root else None}

    def record(self, scope, title, body, tags='', *, project_root=None, automatic=False,
               review_token=None, merge_ids=None):
        """要求调用方明确范围。项目根目录不明时保留在本轮对话中。"""
        if (review_token is not None or merge_ids is not None) and (scope != 'global' or not automatic):
            raise ValueError('候选条目只用于全局自动记忆。')
        if scope == 'global' and project_root is None:
            if automatic:
                return self.record_global(title, body, tags, review_token=review_token, merge_ids=merge_ids)
            return {'scope': scope, 'item': self.save_memory(title, body, tags), 'skipped': False}
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
