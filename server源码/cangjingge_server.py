# -*- coding: utf-8 -*-
"""藏经阁本地服务
双击藏经阁.exe（由本文件打包）后在 127.0.0.1 上启动，
浏览器访问 http://127.0.0.1:8734/ 即可免授权直读直写 data 文件夹。

数据目录可按下列优先级指定（先命中者生效）：
  1. 把文件夹拖到 exe 上 / 命令行传参      藏经阁.exe "D:\\我的世界观"
  2. 环境变量 CANGJINGGE_HOME
  3. exe 旁边的「数据目录.txt」（写一行绝对路径）
  4. 默认：exe 所在目录下的 data 文件夹

网页文件优先用 exe 旁边的 藏经阁.html，找不到时回退到打包内嵌的那份。
"""
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT_BASE = 8734
PORT_RANGE = 10
IDLE_SHUTDOWN_SEC = 180   # 页面心跳消失超过 3 分钟自动退出
START_GRACE_SEC = 60      # 启动宽限期

APP_NAME = '藏经阁'
MARKER = '.cangjingge.json'
TYPES = {
    'role': '人物', 'faction': '势力', 'item': '物品',
    'place': '地点', 'event': '事件', 'lore': '设定',
}
DIR_TO_TYPE = {v: k for k, v in TYPES.items()}


# ============================ 路径解析 ============================

def app_dir():
    """程序所在目录（打包后就是 exe 所在目录）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # 源码运行时，本文件位于 <项目根>/server源码/ 下，上溯两级即项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel):
    """PyInstaller onefile 解包出来的内嵌资源目录"""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(app_dir(), rel)


BASE = app_dir()


def normalize_data_dir(path):
    """拖进来的可能就是 data 本身，也可能是它的父目录，这里统一成数据根目录"""
    p = os.path.abspath(path)
    if os.path.isfile(os.path.join(p, MARKER)):
        return p, '本身就是数据根（含 %s）' % MARKER
    sub = os.path.join(p, 'data')
    if os.path.isdir(sub):
        return sub, '使用其下的 data 子文件夹'
    if any(os.path.isdir(os.path.join(p, d)) for d in DIR_TO_TYPE):
        return p, '含类型子文件夹，视为数据根'
    return p, '未识别结构，原样使用（会按需创建类型文件夹）'


def resolve_data_dir():
    """按优先级确定数据目录，返回 (路径, 来源说明)"""
    for a in sys.argv[1:]:
        a = str(a).strip().strip('"')
        if a and os.path.isdir(a):
            path, why = normalize_data_dir(a)
            return path, '拖拽/命令行参数（%s）' % why
    env = os.environ.get('CANGJINGGE_HOME')
    if env and env.strip():
        env = env.strip().strip('"')
        if os.path.isdir(env):
            path, why = normalize_data_dir(env)
            return path, '环境变量 CANGJINGGE_HOME（%s）' % why
    cfg = os.path.join(BASE, '数据目录.txt')
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding='utf-8', errors='replace') as f:
                for line in f:
                    v = line.strip().strip('"')
                    if not v or v.startswith('#'):
                        continue
                    if os.path.isdir(v):
                        path, why = normalize_data_dir(v)
                        return path, '数据目录.txt（%s）' % why
                    break
        except OSError:
            pass
    return os.path.join(BASE, 'data'), '默认（exe 旁边的 data 文件夹）'


def find_html():
    """优先用外部 html（方便随时改），找不到才用打包内嵌的那份"""
    p = os.path.join(BASE, '藏经阁.html')
    if os.path.isfile(p):
        return p, '外部文件'
    p = resource_path('藏经阁.html')
    if os.path.isfile(p):
        return p, '内嵌（exe 自带）'
    return None, '缺失'


DATA_PATH, DATA_SOURCE = resolve_data_dir()
HTML_PATH, HTML_SOURCE = find_html()
PORT = PORT_BASE


# ============================ 启动日志 ============================

LOG_PATH = None


def setup_log():
    global LOG_PATH
    for d in (os.path.join(BASE, 'logs'), tempfile.gettempdir()):
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, '藏经阁启动日志.txt')
            with open(p, 'a', encoding='utf-8'):
                pass
            LOG_PATH = p
            return
        except OSError:
            continue


def log(msg):
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S') + '  ' + msg + '\n')
    except OSError:
        pass


# ============================ 词条版本快照 ============================

def version_dir(type_key, name, create=False):
    type_key = type_key if type_key in TYPES else 'lore'
    d = os.path.join(VERSIONS_PATH, TYPES[type_key], safe_name(name))
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None
    return d


def snapshot_entry(type_key, name):
    """保存前把当前内容留一份快照，每个词条只保留最近 MAX_VERSIONS 个"""
    type_key = type_key if type_key in TYPES else 'lore'
    safe = safe_name(name)
    src = os.path.join(DATA_PATH, TYPES[type_key], safe + '.md')
    if not os.path.isfile(src):
        return None
    d = version_dir(type_key, name, create=True)
    if not d:
        return None
    stamp = time.strftime('%Y%m%d-%H%M%S')
    p = os.path.join(d, stamp + '.md')
    i = 2
    while os.path.isfile(p):
        p = os.path.join(d, '%s(%d).md' % (stamp, i))
        i += 1
    try:
        with open(src, 'rb') as f:
            data = f.read()
        tmp = p + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, p)          # 原子落盘，避免快照写一半
    except OSError:
        return None
    try:
        files = sorted(x for x in os.listdir(d) if x.lower().endswith('.md'))
        for old in files[:-MAX_VERSIONS]:
            os.remove(os.path.join(d, old))
    except OSError:
        pass
    return os.path.basename(p)


def list_versions(type_key, name):
    type_key = type_key if type_key in TYPES else 'lore'
    d = version_dir(type_key, name)
    if not d or not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d), reverse=True):
        if not fn.lower().endswith('.md'):
            continue
        p = os.path.join(d, fn)
        try:
            st = os.stat(p)
            with open(p, encoding='utf-8', errors='replace') as f:
                preview = f.read(160)
        except OSError:
            continue
        preview = ' '.join(preview.split())
        out.append({'file': fn,
                    'time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime)),
                    'size': st.st_size, 'preview': preview})
    return out


def read_version(type_key, name, fname):
    type_key = type_key if type_key in TYPES else 'lore'
    p = os.path.join(VERSIONS_PATH, TYPES[type_key], safe_name(name),
                     os.path.basename(fname or ''))
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def list_map_files():
    """data/地图/ 下的图片文件，每张图就是一张可用地图"""
    out = []
    d = os.path.join(DATA_PATH, '地图')
    if os.path.isdir(d):
        try:
            for fn in sorted(os.listdir(d)):
                if os.path.splitext(fn)[1].lower() in IMAGE_TYPES:
                    out.append(fn)
        except OSError:
            pass
    return out


def load_maps():
    try:
        with open(MAPS_PATH, encoding='utf-8') as f:
            return json.load(f).get('maps') or []
    except Exception:
        return []


def save_maps(items):
    atomic_write(MAPS_PATH, json.dumps({'maps': items}, ensure_ascii=False, indent=2))


def restore_version(type_key, name, fname):
    """把某个历史版本恢复为当前内容；恢复前先给现在的内容留一份快照"""
    type_key = type_key if type_key in TYPES else 'lore'
    text = read_version(type_key, name, fname)
    if text is None:
        return False
    snapshot_entry(type_key, name)
    d = os.path.join(DATA_PATH, TYPES[type_key])
    os.makedirs(d, exist_ok=True)
    atomic_write(os.path.join(d, safe_name(name) + '.md'), text)
    write_history('恢复版本', TYPES[type_key] + '/' + name, os.path.basename(fname or ''))
    return True


# ============================ 回收站与操作日志 ============================

TRASH_PATH = os.path.join(DATA_PATH, '.trash')
HISTORY_PATH = os.path.join(DATA_PATH, '.history.log')
TEMPLATES_PATH = os.path.join(DATA_PATH, '.templates.json')
VERSIONS_PATH = os.path.join(DATA_PATH, '.versions')
MAPS_PATH = os.path.join(DATA_PATH, '.maps.json')
MAX_VERSIONS = 20
TRASH_INDEX = 'index.json'


def now_str():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def write_history(action, target, extra=''):
    try:
        with open(HISTORY_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] %s %s%s\n' % (now_str(), action, target,
                                        (' — ' + extra) if extra else ''))
    except OSError:
        pass


def read_history(limit=200):
    try:
        with open(HISTORY_PATH, encoding='utf-8') as f:
            lines = [l.rstrip('\n') for l in f if l.strip()]
    except OSError:
        return []
    return list(reversed(lines[-limit:]))


def load_trash():
    try:
        with open(os.path.join(TRASH_PATH, TRASH_INDEX), encoding='utf-8') as f:
            return json.load(f).get('items') or []
    except Exception:
        return []


def save_trash(items):
    os.makedirs(TRASH_PATH, exist_ok=True)
    atomic_write(os.path.join(TRASH_PATH, TRASH_INDEX),
                 json.dumps({'items': items}, ensure_ascii=False, indent=2))


def load_templates():
    """读词条模板配置；文件不存在或损坏时返回 None，由前端回退内置模板"""
    try:
        with open(TEMPLATES_PATH, encoding='utf-8') as f:
            d = json.load(f)
        items = d.get('templates')
        return items if isinstance(items, list) and items else None
    except Exception:
        return None


def save_templates(items):
    atomic_write(TEMPLATES_PATH,
                 json.dumps({'version': 1, 'templates': items}, ensure_ascii=False, indent=2))


def unique_fname(d, base):
    name = base + '.md'
    i = 2
    while os.path.exists(os.path.join(d, name)):
        name = '%s(%d).md' % (base, i)
        i += 1
    return name


def trash_path_of(item):
    return os.path.join(TRASH_PATH, item.get('dir') or '', item.get('file') or '')


def trash_entry(type_key, name):
    """把词条移入 data/.trash/<类型>/，返回是否成功"""
    type_key = type_key if type_key in TYPES else 'lore'
    dirname = TYPES[type_key]
    safe = safe_name(name)
    src = os.path.join(DATA_PATH, dirname, safe + '.md')
    if not os.path.isfile(src):
        return False
    with open(src, encoding='utf-8') as f:
        text = f.read()
    dst_dir = os.path.join(TRASH_PATH, dirname)
    os.makedirs(dst_dir, exist_ok=True)
    fname = unique_fname(dst_dir, safe)
    with open(os.path.join(dst_dir, fname), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.remove(src)
    items = load_trash()
    items.append({'name': name, 'type': type_key, 'dir': dirname,
                  'file': fname, 'deletedAt': now_str()})
    save_trash(items)
    write_history('删除', dirname + '/' + name, '已移入回收站')
    return True


def restore_entry(item):
    dirname = item.get('dir') or TYPES.get(item.get('type'), '设定')
    src = trash_path_of(item)
    if not os.path.isfile(src):
        return False
    with open(src, encoding='utf-8') as f:
        text = f.read()
    dst_dir = os.path.join(DATA_PATH, dirname)
    os.makedirs(dst_dir, exist_ok=True)
    base = safe_name(item.get('name'))
    fname = unique_fname(dst_dir, base)
    with open(os.path.join(dst_dir, fname), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.remove(src)
    items = [x for x in load_trash()
             if not (x.get('dir') == item.get('dir') and x.get('file') == item.get('file'))]
    save_trash(items)
    write_history('恢复', dirname + '/' + (item.get('name') or ''),
                  ('原名已存在，恢复为 ' + fname) if fname != base + '.md' else '')
    return True


def purge_entry(item):
    p = trash_path_of(item)
    if os.path.isfile(p):
        os.remove(p)
    items = [x for x in load_trash()
             if not (x.get('dir') == item.get('dir') and x.get('file') == item.get('file'))]
    save_trash(items)
    write_history('彻底删除', (item.get('dir') or '') + '/' + (item.get('name') or ''))
    return True


def empty_trash():
    n = 0
    for it in load_trash():
        p = trash_path_of(it)
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except OSError:
            pass
    save_trash([])
    for d in DIR_TO_TYPE:
        p = os.path.join(TRASH_PATH, d)
        try:
            if os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        except OSError:
            pass
    write_history('清空回收站', '%d 个词条' % n)
    return n


# ============================ 词条读写 ============================

def safe_name(n):
    s = re.sub(r'[\\/:*?"<>|]', '_', str(n or '').strip())
    s = re.sub(r'[. ]+$', '', s)
    return s or '未命名'


def parse_md(text, type_key, base):
    e = {'id': TYPES[type_key] + '/' + base, 'name': base, 'type': type_key,
         'aliases': [], 'year': '', 'fields': [], 'content': ''}
    text = (text or '').lstrip('\ufeff')
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            head = text[4:end]
            for line in head.split('\n'):
                i = line.find(':')
                if i < 1:
                    continue
                key = line[:i].strip()
                val = line[i + 1:].strip()
                if not key:
                    continue
                if key == '类型' and val in DIR_TO_TYPE:
                    continue
                elif key == '别名':
                    e['aliases'] = [s.strip() for s in re.split(r'[,，、]+', val) if s.strip()]
                elif key == '纪年':
                    e['year'] = val
                else:
                    e['fields'].append({'label': key, 'value': val})
            e['content'] = text[end + 4:].lstrip('\n').rstrip()
        else:
            e['content'] = text
    else:
        e['content'] = text
    return e


def serialize_md(e):
    head = ''
    if e.get('aliases'):
        head += '别名: ' + '，'.join(e['aliases']) + '\n'
    if e.get('year'):
        head += '纪年: ' + e['year'] + '\n'
    for f in e.get('fields') or []:
        label = (f.get('label') or '').strip()
        if label:
            head += label + ': ' + (f.get('value') or '') + '\n'
    return '---\n' + head + '---\n\n' + (e.get('content') or '') + '\n'


def atomic_write(path, text):
    """先写临时文件再原子替换：写入过程中若中断（崩溃 / 断电），原文件仍然完好"""
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def entries_signature():
    """六个目录里所有 md 的（路径 + 修改时间 + 大小）拼成的签名，用于判断缓存是否过期"""
    parts = []
    for d in DIR_TO_TYPE:
        p = os.path.join(DATA_PATH, d)
        if not os.path.isdir(p):
            continue
        try:
            for fn in sorted(os.listdir(p)):
                if not fn.lower().endswith('.md'):
                    continue
                st = os.stat(os.path.join(p, fn))
                parts.append('%s/%s|%d|%d' % (d, fn, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return '\n'.join(parts)


_entries_cache = {'sig': None, 'data': None}


def load_all_cached():
    """词条多时逐文件读盘很慢（1000 条约 130ms），用签名缓存住未变动的结果"""
    sig = entries_signature()
    if _entries_cache['sig'] == sig and _entries_cache['data'] is not None:
        return _entries_cache['data']
    data = load_all()
    _entries_cache['sig'] = sig
    _entries_cache['data'] = data
    return data


def count_entries():
    """只数文件不读内容，比 load_all() 快一个数量级"""
    n = 0
    for d in DIR_TO_TYPE:
        p = os.path.join(DATA_PATH, d)
        if not os.path.isdir(p):
            continue
        try:
            n += len([f for f in os.listdir(p) if f.lower().endswith('.md')])
        except OSError:
            pass
    return n


def ensure_dirs():
    try:
        os.makedirs(DATA_PATH, exist_ok=True)
        for d in DIR_TO_TYPE:
            os.makedirs(os.path.join(DATA_PATH, d), exist_ok=True)
        marker = os.path.join(DATA_PATH, MARKER)
        if not os.path.isfile(marker):
            with open(marker, 'w', encoding='utf-8') as f:
                json.dump({'app': APP_NAME, 'version': 2,
                           'note': '此文件夹是藏经阁的数据目录。每个 .md 文件是一个词条，'
                                   '文件所在文件夹决定词条类型。可以用任何文本编辑器直接修改。'},
                          f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def write_entry(e):
    type_key = e.get('type') if e.get('type') in TYPES else 'lore'
    d = os.path.join(DATA_PATH, TYPES[type_key])
    os.makedirs(d, exist_ok=True)
    base = safe_name(e.get('name'))
    p = os.path.join(d, base + '.md')
    existed = os.path.isfile(p)
    if existed:
        snapshot_entry(type_key, base)      # 改动前先留一份历史
    atomic_write(p, serialize_md(e))        # 原子替换，写一半中断也不会损坏原文件
    write_history('更新' if existed else '新建', TYPES[type_key] + '/' + base)
    return TYPES[type_key] + '/' + base


def delete_entry(type_key, name, hard=False):
    """hard=True 直接删除（仅改名时清理旧文件用）；否则请走 trash_entry"""
    type_key = type_key if type_key in TYPES else 'lore'
    d = os.path.join(DATA_PATH, TYPES[type_key])
    p = os.path.join(d, safe_name(name) + '.md')
    if os.path.isfile(p):
        os.remove(p)  # 异常直接抛出，由接口层统一报告
        if hard:
            write_history('改名清理旧文件', TYPES[type_key] + '/' + name)


def load_all():
    entries = []
    if not os.path.isdir(DATA_PATH):
        return entries
    for dirname, type_key in DIR_TO_TYPE.items():
        d = os.path.join(DATA_PATH, dirname)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith('.md'):
                continue
            try:
                with open(os.path.join(d, fn), encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            entries.append(parse_md(text, type_key, fn[:-3]))
    return entries


# ============================ HTTP ============================

last_ping = [0.0]
server = None

IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
               '.svg': 'image/svg+xml'}


def _q(q, key, default=''):
    v = q.get(key)
    return (v[0] if v and v[0] else default)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self._send(code, body, 'application/json; charset=utf-8')

    def _serve_file(self, q):
        """按 data 目录内的相对路径返回二进制文件（地图图片用）"""
        rel = _q(q, 'p')
        if not rel:
            self._json({'error': '缺少参数 p'}, 400)
            return
        parts = [x for x in rel.replace('\\', '/').split('/') if x not in ('', '.')]
        if not parts or '..' in parts:
            self._json({'error': '路径不合法'}, 400)
            return
        p = os.path.join(DATA_PATH, *parts)
        if not os.path.isfile(p):
            self._json({'error': '文件不存在'}, 404)
            return
        ctype = IMAGE_TYPES.get(os.path.splitext(p)[1].lower(), 'application/octet-stream')
        try:
            with open(p, 'rb') as f:
                body = f.read()
        except OSError:
            self._json({'error': '读取失败'}, 500)
            return
        self._send(200, body, ctype)

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/index'):
            if HTML_PATH:
                try:
                    with open(HTML_PATH, 'rb') as f:
                        body = f.read()
                    self._send(200, body, 'text/html; charset=utf-8')
                except OSError:
                    self._send(500, '读取 藏经阁.html 失败'.encode('utf-8'),
                               'text/plain; charset=utf-8')
            else:
                self._send(404, '找不到 藏经阁.html，且 exe 内也没有内嵌版本。'
                                '请把 藏经阁.html 放回 exe 旁边。'.encode('utf-8'),
                           'text/plain; charset=utf-8')
        elif self.path == '/api/entries':
            self._json({'entries': load_all_cached(), 'root': DATA_PATH, 'rootSource': DATA_SOURCE})
        elif self.path == '/api/info':
            self._json({'app': APP_NAME, 'root': DATA_PATH, 'rootSource': DATA_SOURCE,
                        'htmlSource': HTML_SOURCE, 'port': PORT, 'count': count_entries()})
        elif self.path == '/api/trash':
            self._json({'items': load_trash()})
        elif self.path == '/api/history':
            self._json({'lines': read_history()})
        elif self.path == '/api/templates':
            self._json({'templates': load_templates(),
                        'hasFile': os.path.isfile(TEMPLATES_PATH)})
        elif self.path.startswith('/api/versions'):
            q = parse_qs(urlparse(self.path).query)
            self._json({'items': list_versions(_q(q, 'type'), _q(q, 'name'))})
        elif self.path.startswith('/api/version'):
            q = parse_qs(urlparse(self.path).query)
            text = read_version(_q(q, 'type'), _q(q, 'name'), _q(q, 'file'))
            self._json({'text': text if text is not None else ''})
        elif self.path == '/api/mapfiles':
            self._json({'files': list_map_files()})
        elif self.path == '/api/maps':
            self._json({'maps': load_maps()})
        elif self.path.startswith('/api/file'):
            self._serve_file(parse_qs(urlparse(self.path).query))
        elif self.path == '/api/ping':
            last_ping[0] = time.time()
            self._json({'ok': True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            self._json({'error': 'bad json'}, 400)
            return
        if self.path == '/api/save':
            try:
                eid = write_entry(payload.get('entry') or {})
                self._json({'ok': True, 'id': eid})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/delete':
            try:
                delete_entry(payload.get('type'), payload.get('name'), hard=True)
                self._json({'ok': True})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/trash':
            try:
                self._json({'ok': trash_entry(payload.get('type'), payload.get('name'))})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/restore':
            try:
                self._json({'ok': restore_entry(payload.get('item') or {})})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/purge':
            try:
                self._json({'ok': purge_entry(payload.get('item') or {})})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/empty-trash':
            try:
                self._json({'ok': True, 'count': empty_trash()})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/templates':
            try:
                save_templates(payload.get('templates') or [])
                self._json({'ok': True})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/maps':
            try:
                save_maps(payload.get('maps') or [])
                self._json({'ok': True})
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
        elif self.path == '/api/restore-version':
            ok = restore_version(payload.get('type'), payload.get('name'),
                                 payload.get('file'))
            self._json({'ok': ok})
        elif self.path == '/api/shutdown':
            self._json({'ok': True})
            log('收到 /api/shutdown，正在退出')
            threading.Thread(target=hard_exit, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


def hard_exit(delay=0.4):
    """shutdown() 在部分环境下会卡住，这里给响应留出时间后强制退出进程"""
    time.sleep(delay)
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


def probe(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/info' % port, timeout=0.4) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return None


def same_path(a, b):
    try:
        return os.path.normcase(os.path.abspath(a or '')) == os.path.normcase(os.path.abspath(b or ''))
    except Exception:
        return False


def find_running():
    """并行探测所有候选端口，找出已经在跑的藏经阁实例，返回 (端口, info)"""
    found = {}

    def check(p):
        info = probe(p)
        if info and info.get('app') == APP_NAME:
            found[p] = info

    ths = [threading.Thread(target=check, args=(p,), daemon=True)
           for p in range(PORT_BASE, PORT_BASE + PORT_RANGE)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(1.0)
    if not found:
        return None, None
    p = min(found)
    return p, found[p]


def pick_free_port():
    for p in range(PORT_BASE, PORT_BASE + PORT_RANGE):
        with socket.socket() as s:
            try:
                s.bind(('127.0.0.1', p))
                return p
            except OSError:
                continue
    return None


def main():
    global server, PORT
    setup_log()
    log('=' * 46)
    log('%s 启动' % time.strftime('%Y-%m-%d'))
    log('程序目录   : %s' % BASE)
    log('数据目录   : %s' % DATA_PATH)
    log('目录来源   : %s' % DATA_SOURCE)
    log('网页来源   : %s (%s)' % (HTML_SOURCE, HTML_PATH or '无'))
    ensure_dirs()
    log('词条数量   : %d' % len(load_all()))

    run_port, run_info = find_running()
    if run_port:
        if same_path(run_info.get('root'), DATA_PATH):
            log('已有实例在 %d 且数据目录一致，直接开页面' % run_port)
            webbrowser.open('http://127.0.0.1:%d/' % run_port)
            return
        log('已有实例在 %d，但其数据目录是 %s（本次要 %s），改用新端口'
            % (run_port, run_info.get('root'), DATA_PATH))

    port = pick_free_port()
    if port is None:
        log('错误：%d-%d 端口全被占用，放弃启动' % (PORT_BASE, PORT_BASE + PORT_RANGE - 1))
        return
    PORT = port

    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except Exception as ex:
        log('错误：无法监听 %d 端口 — %s（可能是防火墙拦截）' % (port, ex))
        return

    url = 'http://127.0.0.1:%d/' % port
    log('服务地址   : %s' % url)
    if not os.environ.get('CANGJINGGE_NO_BROWSER'):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    def watchdog():
        started = time.time()
        while True:
            time.sleep(5)
            idle = time.time() - max(last_ping[0], started)
            if time.time() - started > START_GRACE_SEC and idle > IDLE_SHUTDOWN_SEC:
                log('页面心跳中断超过 %d 秒，自动退出' % IDLE_SHUTDOWN_SEC)
                hard_exit(0.1)

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log('服务已停止')
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        try:
            d = os.path.join(BASE, 'logs')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'server-error.log'), 'a', encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + traceback.format_exc() + '\n')
        except Exception:
            pass
        sys.exit(1)
