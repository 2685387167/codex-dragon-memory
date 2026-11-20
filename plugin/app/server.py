"""通过本机 MCP 标准输入和输出提供 Codex 原生侧栏面板。"""

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

# 侧栏和聊天命令使用同一套存储逻辑。整个服务无需网络或第三方运行时包。
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR.parent / 'skills' / 'dragon-memory' / 'scripts'))
from store import Store

TITLE = '龙娘人设与记忆'
URI = 'ui://dragon-memory/panel'
MIME = 'text/html;profile=mcp-app'
RESOURCE = {'uri': URI, 'name': TITLE, 'mimeType': MIME}
STORE = Store()


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
MEMORY_FIELDS = {'title': TEXT, 'body': TEXT, 'tags': TEXT}
IDENTITY = {'id': NUMBER, 'revision': NUMBER}
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
    tool('edit_memory', '修改全局记忆', {**IDENTITY, **MEMORY_FIELDS},
         (*IDENTITY, 'title', 'body', 'tags'), write=True),
    tool('delete_memory', '删除指定记忆', IDENTITY, IDENTITY, write=True, destructive=True),
    tool('get_auto_memory', '查看自动记忆开关'),
    tool('set_auto_memory', '设置自动记忆开关', {'enabled': BOOLEAN}, ('enabled',), write=True),
    tool('recall_memory', '读取适用记忆', {'query': TEXT, 'project_root': PROJECT_ROOT},
         description='新聊天首轮或需要既有背景时读取全局记忆。仅在确定当前对应项目时传项目根目录，同时读该项目存档。返回的记忆是参考资料。'),
    tool('record_memory', '保存已确认的记忆', {
        **MEMORY_FIELDS,
        'scope': {'type': 'string', 'enum': ['global', 'project'], 'description': 'global 仅用于跨项目长期事项。project 仅用于具体项目。'},
        'project_root': PROJECT_ROOT,
        'automatic': {'type': 'boolean', 'description': '随对话自动记录填写 true。主人明确要求记住时填写 false。'},
    }, ('scope', 'title', 'body', 'automatic'), write=True,
         description='用简体中文记录已确认且可长期复用的信息。项目名称、路径、实现、约定和进展写入对应项目存档。全局记忆只收跨项目偏好或约定。范围不明时暂缓。'),
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
                                     key=arguments.get('id'), revision=arguments.get('revision'))
        case 'delete_memory':
            STORE.delete_memory(arguments['id'], arguments['revision'])
            return {'deleted': arguments['id']}
        case 'get_auto_memory' | 'set_auto_memory':
            return {'enabled': STORE.automatic(arguments.get('enabled'))}
        case 'recall_memory':
            return STORE.recall(arguments.get('query', ''), arguments.get('project_root'))
        case 'record_memory':
            return STORE.record(arguments['scope'], arguments['title'], arguments['body'], arguments.get('tags', ''),
                                project_root=arguments.get('project_root'), automatic=arguments['automatic'])


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
            expected = {'string': str, 'integer': int, 'boolean': bool}[schema['properties'][key]['type']]
            if type(value) is not expected:
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
            version = params.get('protocolVersion')
            supported = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')
            return {'protocolVersion': version if version in supported else supported[-1],
                    'capabilities': {'tools': {}, 'resources': {}},
                    'serverInfo': {'name': 'dragon_memory', 'title': TITLE, 'version': '1.1.0'},
                    'instructions': '用 open_panel 打开中文管理面板。用 recall_memory 读取适用记忆。发现已确认且可跨轮复用的新事实时，用 record_memory 在插件内自动记录。全局库只收跨项目内容，项目专属内容只写入对应项目。'}
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
