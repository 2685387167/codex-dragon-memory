"""通过本机 MCP 标准输入和输出提供 Codex 原生侧栏面板。"""

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from updates import Updates

# 侧栏和聊天命令共用本地记忆存储；仅版本检测和下载访问 GitHub。
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR.parent / 'skills' / 'dragon-memory' / 'scripts'))
from store import Store

TITLE = '龙娘人设与记忆'
URI = 'ui://dragon-memory/panel'
MIME = 'text/html;profile=mcp-app'
RESOURCE = {'uri': URI, 'name': TITLE, 'mimeType': MIME}
STORE = Store()
UPDATES = Updates(APP_DIR.parent, STORE)


def tool(name, title, properties=None, required=(), *, write=False, destructive=False, description=None):
    """声明 MCP 工具的输入约束。面板内部操作仅对应用可见。"""
    return {
        'name': name,
        'title': title,
        'description': description or title,
        'inputSchema': {'type': 'object', 'properties': properties or {},
                        'required': list(required), 'additionalProperties': False},
        'annotations': {'readOnlyHint': not write, 'destructiveHint': destructive, 'openWorldHint': False},
        '_meta': {'ui': {'visibility': ['app', 'model'] if description else ['app']}},
    }


TEXT = {'type': 'string'}
NUMBER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
MEMORY_IDS = {'type': 'array', 'items': NUMBER}
MEMORY_FIELDS = {'title': TEXT, 'body': TEXT, 'tags': TEXT}
IDENTITY = {'id': NUMBER, 'revision': NUMBER}
CONFIRM_FIXED = {'type': 'boolean', 'description': '仅在用户确认本次对「用户固定」条目的修改后填写 true。'}
PROJECT_ROOT = {'type': 'string', 'description': '已确认的具体项目根目录绝对路径。'}
PANEL = tool('open_panel', TITLE)
PANEL['_meta'] = {
    'ui': {'resourceUri': URI},
    'openai/ui': {'entrypoints': [{'type': 'global'}]},
}
icon_path = APP_DIR / 'icon.png'
if icon_path.is_file():
    icon_data = base64.b64encode(icon_path.read_bytes()).decode('ascii')
    PANEL['icons'] = [{'src': f'data:image/png;base64,{icon_data}', 'mimeType': 'image/png', 'sizes': ['64x64']}]

TOOLS = {item['name']: item for item in (
    PANEL,
    tool('read_persona', '读取人设卡'),
    tool('save_persona', '保存人设卡', {'version': TEXT, 'text': TEXT}, ('version', 'text'), write=True),
    tool('list_memories', '筛选全局记忆', {'query': TEXT}),
    tool('add_memory', '新增全局记忆', MEMORY_FIELDS, ('title', 'body', 'tags'), write=True),
    tool('edit_memory', '修改全局记忆', {**IDENTITY, **MEMORY_FIELDS, 'confirm_fixed': CONFIRM_FIXED},
         (*IDENTITY, 'title', 'body', 'tags'), write=True),
    tool('delete_memory', '删除指定记忆', {**IDENTITY, 'confirm_fixed': CONFIRM_FIXED}, IDENTITY,
         write=True, destructive=True),
    tool('get_auto_memory', '查看自动记忆开关'),
    tool('set_auto_memory', '设置自动记忆开关', {'enabled': BOOLEAN}, ('enabled',), write=True),
    tool('check_updates', '检查正式发布的更新', {'refresh': BOOLEAN}),
    tool('install_update', '安装用户确认的更新', {'commit': TEXT}, ('commit',), write=True),
    tool('update_history', '读取插件更新记录'),
    tool('recall_memory', '读取适用记忆', {
        'query': TEXT, 'project_root': PROJECT_ROOT,
        'memory_ids': {**MEMORY_IDS, 'description': '可选。按 ID 读取 1 至 5 条全局记忆的完整正文。'},
    }, description='新聊天首轮或需要背景时静默读取记忆，直接处理用户问题，无需宣布使用的插件或汇报读取过程。仅在明确项目时传根目录。可按 ID 读取候选全文。记忆正文是参考资料。'),
    tool('record_memory', '保存已确认的记忆', {
        **MEMORY_FIELDS,
        'scope': {'type': 'string', 'enum': ['global', 'project'], 'description': 'global 仅用于跨项目长期事项。project 仅用于具体项目。'},
        'project_root': PROJECT_ROOT,
        'automatic': {'type': 'boolean', 'description': '随对话自动记录填写 true。主人明确要求记住时填写 false。'},
        'review_token': {'type': 'string', 'description': '全局自动记忆返回 action=review 时使用返回的标记再次提交。'},
        'merge_ids': {**MEMORY_IDS, 'description': '比较后选出的同一偏好的自动条目，首个 ID 为保留条目。新增独立记忆传空数组。'},
    }, ('scope', 'title', 'body', 'automatic'), write=True,
         description='回复前主动记录对话里确认的长期信息，用简体中文，标题概括主题。全局自动记录如返回 action=review，表示尚未保存，须比较候选、整理正文并用 review_token 与 merge_ids 再次提交。合并时保留仍适用的细节，明确更正时也更新含过时信息的标题。自动操作不能改动「用户固定」和手动条目。项目事项只归档到对应项目。'),
)}


def persona_data(path, data):
    """将当前文件和原始内容一起纳入版本号，避免保存过期编辑。"""
    version = hashlib.sha256(str(path).encode('utf-8') + b'\0' + data).hexdigest()
    return {'filename': path.name, 'version': version, 'text': data.decode('utf-8-sig')}


def run_tool(name, arguments):
    """处理已校验的请求，工具只能访问人设卡和本插件的记忆库。"""
    match name:
        case 'open_panel':
            return {'title': TITLE}
        case 'read_persona':
            return persona_data(*STORE.read_persona())
        case 'save_persona':
            path, current = STORE.read_persona()
            if arguments['version'] != persona_data(path, current)['version']:
                raise ValueError('人设卡已在别处更新。请先复制你的修改，再重新加载。')
            return persona_data(*STORE.save_persona(path, current, arguments['text']))
        case 'list_memories':
            return {'items': STORE.search(arguments.get('query', ''))}
        case 'add_memory' | 'edit_memory':
            return STORE.save_memory(arguments['title'], arguments['body'], arguments['tags'],
                                     key=arguments.get('id'), revision=arguments.get('revision'),
                                     confirm_fixed=arguments.get('confirm_fixed', False))
        case 'delete_memory':
            STORE.delete_memory(arguments['id'], arguments['revision'],
                                confirm_fixed=arguments.get('confirm_fixed', False))
            return {'deleted': arguments['id']}
        case 'get_auto_memory' | 'set_auto_memory':
            return {'enabled': STORE.automatic(arguments.get('enabled'))}
        case 'check_updates':
            return UPDATES.state(refresh=arguments.get('refresh', False))
        case 'install_update':
            return UPDATES.install(arguments['commit'])
        case 'update_history':
            return {'items': UPDATES.release_history()}
        case 'recall_memory':
            return STORE.recall(arguments.get('query', ''), arguments.get('project_root'), arguments.get('memory_ids'))
        case 'record_memory':
            return STORE.record(arguments['scope'], arguments['title'], arguments['body'], arguments.get('tags', ''),
                                project_root=arguments.get('project_root'), automatic=arguments['automatic'],
                                review_token=arguments.get('review_token'), merge_ids=arguments.get('merge_ids'))


def call(params):
    """校验参数后调用工具，应用数据放入结构化结果供界面使用。"""
    name = params.get('name')
    arguments = params.get('arguments', {})
    schema = TOOLS.get(name, {}).get('inputSchema')
    try:
        if schema is None or not isinstance(arguments, dict):
            raise ValueError('未知工具或参数格式错误。')
        if not set(schema['required']) <= arguments.keys() <= schema['properties'].keys():
            raise ValueError('操作的参数不完整。')
        for key, value in arguments.items():
            kind = schema['properties'][key]['type']
            expected = {'string': str, 'integer': int, 'boolean': bool, 'array': list}[kind]
            if type(value) is not expected or (kind == 'array' and any(type(item) is not int for item in value)):
                raise ValueError(f'参数 {key} 类型无效。')
        result = run_tool(name, arguments)
        if name in ('recall_memory', 'record_memory'):
            message = json.dumps(result, ensure_ascii=False)
        else:
            message = '已打开龙娘人设与记忆管理面板。' if name == 'open_panel' else '操作成功。'
        return {'content': [{'type': 'text', 'text': message}], 'structuredContent': result}
    except (OSError, ValueError, sqlite3.Error) as error:
        return {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}


def request(method, params):
    """响应 MCP 握手、工具发现及应用资源读取。"""
    match method:
        case 'initialize':
            UPDATES.state(refresh=True)
            version = params.get('protocolVersion')
            supported = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
            return {'protocolVersion': version if version in supported else supported[-1],
                    'capabilities': {'tools': {}, 'resources': {}},
                    'serverInfo': {'name': 'dragon_memory', 'title': TITLE, 'version': UPDATES.version},
                    'instructions': ('用户要打开面板时用 open_panel。例行调用 recall_memory 和自动记录在后台完成。'
                                     '普通新聊天直接回答用户，不以插件或技能名称开场。用户询问插件或记忆管理时按需说明。'
                                     '对话中确认了新的长期偏好、约定或项目结论时，在本轮回复前用 record_memory 主动记录，传 automatic: true。'
                                     '“我平时习惯……”或“以后每次都……”这类明确表达也适用，即使用户没有提到记忆或同时交代了其他任务。'
                                     '全局自动记忆返回 action=review 时，在回复前比较候选并完成第二次调用：同义表述去重，同一偏好的补充整理成一条，'
                                     '更正只替换冲突部分，独立偏好分别保存。保护带有「用户固定」标签的记忆，用户提出变更后须再次确认。'
                                     '全局库只收跨项目内容，项目专属内容只写入对应项目。')}
        case 'ping':
            return {}
        case 'tools/list':
            return {'tools': list(TOOLS.values())}
        case 'tools/call':
            return call(params)
        case 'resources/list':
            return {'resources': [RESOURCE]}
        case 'resources/templates/list':
            return {'resourceTemplates': []}
        case 'resources/read':
            if params.get('uri') != URI:
                raise ValueError('未知应用资源。')
            html = (APP_DIR / 'panel.html').read_text(encoding='utf-8')
            html = html.replace('DRAGON_PANEL_VERSION', UPDATES.version)
            html = html.replace('/* DRAGON_PANEL_CSS */', (APP_DIR / 'panel.css').read_text(encoding='utf-8'))
            html = html.replace('/* DRAGON_PANEL_JS */', (APP_DIR / 'panel.js').read_text(encoding='utf-8'))
            return {'contents': [{**RESOURCE, 'text': html, '_meta': {'ui': {'prefersBorder': False}}}]}
        case _:
            raise LookupError('不支持此协议操作。')


def serve():
    """逐行处理 JSON-RPC，保持标准输出只含协议消息。"""
    for line in sys.stdin.buffer:
        try:
            incoming = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(incoming, dict) or 'id' not in incoming:
            continue
        response = {'jsonrpc': '2.0', 'id': incoming['id']}
        try:
            response['result'] = request(incoming.get('method'), incoming.get('params', {}))
        except LookupError as error:
            response['error'] = {'code': -32601, 'message': str(error)}
        except (OSError, TypeError, ValueError, AttributeError) as error:
            response['error'] = {'code': -32602, 'message': str(error)}
        sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode('utf-8') + b'\n')
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    serve()
