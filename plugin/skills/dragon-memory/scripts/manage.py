"""为 Codex 技能提供本地人设与记忆读写命令。"""

import argparse
import json
import sqlite3
import sys

from store import Store


def main():
    """解析用户指定的操作，将结果以 UTF-8 输出给 Codex。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('persona')
    commands.add_parser('search').add_argument('--query', default='')
    recall = commands.add_parser('recall', help='读取全局记忆和指定项目存档')
    recall.add_argument('--query', default='')
    recall.add_argument('--project-root')
    record = commands.add_parser('record', help='按范围保存全局记忆或归档项目记录')
    record.add_argument('--scope', required=True, choices=('global', 'project'))
    record.add_argument('--project-root')
    record.add_argument('--automatic', action='store_true')
    add = commands.add_parser('add')
    edit = commands.add_parser('edit')
    delete = commands.add_parser('delete')
    for command in (edit, delete):
        command.add_argument('id', type=int)
        command.add_argument('revision', type=int)
    for command in (add, edit, record):
        command.add_argument('--title', required=True)
        command.add_argument('--text', required=True)
        command.add_argument('--tags', default='')

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    store = Store()
    try:
        match args.action:
            case 'persona':
                path, data = store.read_persona()
                result = {'path': str(path), 'content': data.decode('utf-8-sig')}
            case 'search':
                result = store.search(args.query)
            case 'recall':
                result = store.recall(args.query, args.project_root)
            case 'record':
                result = store.record(args.scope, args.title, args.text, args.tags,
                                      project_root=args.project_root, automatic=args.automatic)
            case 'add' | 'edit':
                result = store.save_memory(args.title, args.text, args.tags,
                                           key=getattr(args, 'id', None),
                                           revision=getattr(args, 'revision', None))
            case 'delete':
                store.delete_memory(args.id, args.revision)
                result = {'deleted': args.id}
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
