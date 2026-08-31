# -*- coding: utf-8 -*-
"""
藏经阁 · 璇星大陆世界观 —— 本地服务
作者 / Author: StrangeDragon1（奇怪的龙龙 / 奇怪的龍龍）
许可 / License: 自定义许可（软件不可商用、产出内容可商用、修改需署名）—— 见根目录 LICENSE

本地服务：双击藏经阁.exe（由本文件打包）后在 127.0.0.1 上启动，
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
import shutil
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
# ---- 多世界观（存档容器） ----
WORLDS_FILE = '.worlds.json'      # 世界观存档容器里的注册表
WORKSPACE_NAME = '世界存档'       # 软件根目录下的默认存档容器名
LEGACY_WORLD_NAME = '璇星大陆'   # 首次运行时旧 data 迁移成的最初世界观名
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


def find_html():
    """优先用外部 html（方便随时改），找不到才用打包内嵌的那份"""
    p = os.path.join(BASE, '藏经阁.html')
    if os.path.isfile(p):
        return p, '外部文件'
    p = resource_path('藏经阁.html')
    if os.path.isfile(p):
        return p, '内嵌（exe 自带）'
    return None, '缺失'


def safe_name(n):
    s = re.sub(r'[\\/:*?"<>|]', '_', str(n or '').strip())
    s = re.sub(r'[. ]+$', '', s)
    return s or '未命名'


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


# ============================ 多世界观（存档容器） ============================

def is_workspace_dir(p):
    """是不是一个世界观存档容器（含 .worlds.json 注册表）"""
    return os.path.isfile(os.path.join(p, WORLDS_FILE))


def is_world_dir(p):
    """是不是一个世界观数据根（含 .cangjingge.json 标记）"""
    return os.path.isfile(os.path.join(p, MARKER))


def looks_like_world(p):
    """没有标记但已含六大类型文件夹，也按世界观数据根看待"""
    if is_world_dir(p):
        return True
    return any(os.path.isdir(os.path.join(p, d)) for d in DIR_TO_TYPE)


def resolve_startup():
    """确定运行模式与数据目录。
    返回 (mode, data_path, workspace_root, source)
    mode: 'workspace' 多世界观存档 | 'legacy' 单世界兼容（拖了某个世界观直接打开）
    """
    cands = []
    for a in sys.argv[1:]:
        a = str(a).strip().strip('"')
        if a and os.path.isdir(a):
            cands.append(a)
    env = os.environ.get('CANGJINGGE_HOME')
    if env and env.strip():
        e = env.strip().strip('"')
        if os.path.isdir(e):
            cands.append(e)
    cfg = os.path.join(BASE, '数据目录.txt')
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding='utf-8', errors='replace') as f:
                for line in f:
                    v = line.strip().strip('"')
                    if not v or v.startswith('#'):
                        continue
                    if os.path.isdir(v):
                        cands.append(v)
                    break
        except OSError:
            pass
    for p in cands:
        if is_workspace_dir(p):
            return ('workspace', None, p, '拖拽/设置（世界观存档容器）')
        if is_world_dir(p):
            return ('legacy', p, None, '拖拽/设置（单个世界观：%s）' % p)
        sub = os.path.join(p, 'data')
        if is_world_dir(sub):
            return ('legacy', sub, None, '拖拽/设置（使用其下的 data 子文件夹）')
        if looks_like_world(p):
            return ('legacy', p, None, '拖拽/设置（含类型子文件夹，视为数据根）')
    return ('workspace', None, os.path.join(BASE, WORKSPACE_NAME),
            '默认（软件根目录下的「%s」存档容器）' % WORKSPACE_NAME)


def unique_ws_dirname(ws, base):
    n = safe_name(base) or '未命名'
    i = 2
    while os.path.exists(os.path.join(ws, n)):
        n = '%s(%d)' % (safe_name(base), i)
        i += 1
    return n


def make_world_dir(ws, dirn):
    """在存档容器里建一个世界观数据根：六类文件夹 + 标记 + 默认纪元表。返回目录。"""
    d = os.path.join(ws, dirn)
    os.makedirs(d, exist_ok=True)
    for t in DIR_TO_TYPE:
        os.makedirs(os.path.join(d, t), exist_ok=True)
    marker = os.path.join(d, MARKER)
    if not os.path.isfile(marker):
        try:
            with open(marker, 'w', encoding='utf-8') as f:
                json.dump({'app': APP_NAME, 'version': 2,
                           'note': '此文件夹是一个世界观的数据目录（藏经阁）。'
                                   '每个 .md 文件是一个词条，所在文件夹决定其类型，'
                                   '可用任意文本编辑器直接修改。'},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    era_dir = os.path.join(d, '设定')
    os.makedirs(era_dir, exist_ok=True)
    era = os.path.join(era_dir, '纪元表.md')
    if not os.path.isfile(era):
        try:
            with open(era, 'w', encoding='utf-8', newline='\n') as f:
                f.write('---\n---\n\n公元\n')
        except OSError:
            pass
    return d


def load_worlds():
    """读存档容器里的世界观注册表。返回 (worlds 列表, 当前激活目录名)。"""
    if not WORKSPACE_ROOT:
        return [], None
    wf = os.path.join(WORKSPACE_ROOT, WORLDS_FILE)
    try:
        with open(wf, encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return [], None
    worlds = d.get('worlds')
    if not isinstance(worlds, list):
        worlds = []
    active = d.get('lastActive')
    dirs = [w.get('dir') for w in worlds if isinstance(w, dict)]
    if active not in dirs:
        active = dirs[0] if dirs else None
    return worlds, active


def _save_worlds(ws, worlds, active):
    atomic_write(os.path.join(ws, WORLDS_FILE),
                 json.dumps({'version': 1, 'worlds': worlds, 'lastActive': active},
                            ensure_ascii=False, indent=2))


def ensure_workspace(ws):
    """确保 ws 是世界观存档容器；首次使用则迁移旧 data 或建首个世界观。
    返回 (worlds, active_dirname, first_run)。"""
    os.makedirs(ws, exist_ok=True)
    wf = os.path.join(ws, WORLDS_FILE)
    if os.path.isfile(wf):
        try:
            with open(wf, encoding='utf-8') as f:
                d = json.load(f)
            worlds = d.get('worlds')
            if not isinstance(worlds, list):
                raise ValueError('worlds 不是列表')
            active = d.get('lastActive')
            dirs = [w.get('dir') for w in worlds if isinstance(w, dict)]
            if active not in dirs:
                active = dirs[0] if dirs else None
            return worlds, active, False
        except Exception:
            # 注册表损坏/缺失时，先备份原文件，避免静默丢信息
            try:
                if os.path.isfile(wf):
                    os.replace(wf, wf + '.bak')
            except OSError:
                pass
    # 首次：初始化
    worlds = []
    legacy = os.path.join(BASE, 'data')
    if os.path.isdir(legacy) and is_world_dir(legacy):
        dst = os.path.join(ws, LEGACY_WORLD_NAME)
        # 仅当存档容器基本为空时，把旧 data 整体迁移进来
        try:
            empty_ws = not os.listdir(ws)
        except OSError:
            empty_ws = False
        if empty_ws and not os.path.exists(dst):
            try:
                shutil.move(legacy, dst)
            except OSError:
                pass
        if os.path.isdir(dst) and is_world_dir(dst):
            worlds.append({'name': LEGACY_WORLD_NAME, 'dir': LEGACY_WORLD_NAME})
            active = LEGACY_WORLD_NAME
    if not worlds:
        dirn = unique_ws_dirname(ws, LEGACY_WORLD_NAME)
        make_world_dir(ws, dirn)
        worlds.append({'name': LEGACY_WORLD_NAME, 'dir': dirn})
        active = dirn
    _save_worlds(ws, worlds, active)
    return worlds, active, True


def activate_world(dirname):
    """切换到存档容器内的某个世界观，并让服务重新绑定该数据目录。"""
    global DATA_PATH, ACTIVE_WORLD, DATA_SOURCE
    if not WORKSPACE_ROOT:
        return False
    safe = safe_name(dirname)
    target = os.path.join(WORKSPACE_ROOT, safe)
    if not is_world_dir(target):
        return False
    ACTIVE_WORLD = safe
    DATA_PATH = target
    DATA_SOURCE = '世界观存档：%s（当前：%s）' % (WORKSPACE_ROOT, safe)
    worlds, _ = load_worlds()
    _save_worlds(WORKSPACE_ROOT, worlds, safe)
    _entries_cache['sig'] = None
    _entries_cache['data'] = None
    ensure_dirs()
    return True


def create_new_world(name):
    """在存档容器里新建一个世界观并激活。返回目录名，失败返回 None。"""
    global DATA_PATH, ACTIVE_WORLD, DATA_SOURCE
    if not WORKSPACE_ROOT:
        return None
    name = str(name or '').strip()
    if not name:
        return None
    dirn = unique_ws_dirname(WORKSPACE_ROOT, name)
    make_world_dir(WORKSPACE_ROOT, dirn)
    worlds, _ = load_worlds()
    worlds.append({'name': name, 'dir': dirn})
    ACTIVE_WORLD = dirn
    DATA_PATH = os.path.join(WORKSPACE_ROOT, dirn)
    DATA_SOURCE = '世界观存档：%s（当前：%s）' % (WORKSPACE_ROOT, dirn)
    _save_worlds(WORKSPACE_ROOT, worlds, dirn)
    _entries_cache['sig'] = None
    _entries_cache['data'] = None
    ensure_dirs()
    return dirn


# ---- 运行时（多世界观）状态 ----
MODE = 'legacy'          # 'workspace' 多世界观存档 | 'legacy' 单世界兼容
WORKSPACE_ROOT = None    # 世界观存档容器根目录（仅 workspace 模式）
WORLDS = None            # [{'name','dir'}, ...]（仅 workspace 模式）
ACTIVE_WORLD = None      # 当前激活的世界观子目录名（仅 workspace 模式）
FIRST_RUN = False        # 本次启动是否首次初始化存档


def init_runtime():
    """根据启动参数/环境确定运行模式与数据目录，并完成首次存档建库。"""
    global DATA_PATH, DATA_SOURCE, MODE, WORKSPACE_ROOT, WORLDS, ACTIVE_WORLD, FIRST_RUN
    mode, data_path, ws_root, source = resolve_startup()
    MODE = mode
    if mode == 'workspace':
        WORKSPACE_ROOT = ws_root
        WORLDS, ACTIVE_WORLD, FIRST_RUN = ensure_workspace(ws_root)
        DATA_PATH = os.path.join(ws_root, ACTIVE_WORLD) if ACTIVE_WORLD else ws_root
        DATA_SOURCE = '世界观存档：%s（当前：%s）%s' % (
            ws_root, ACTIVE_WORLD or '（空）',
            '，本次已初始化' if FIRST_RUN else '')
        WORLDS, ACTIVE_WORLD = load_worlds()
    else:
        DATA_PATH = data_path
        DATA_SOURCE = source


init_runtime()
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
        elif self.path == '/api/worlds':
            if MODE == 'workspace' and WORKSPACE_ROOT:
                worlds, active = load_worlds()
                self._json({'mode': 'workspace', 'root': WORKSPACE_ROOT,
                            'worlds': worlds, 'lastActive': active,
                            'active': ACTIVE_WORLD, 'data': DATA_PATH})
            else:
                self._json({'mode': 'legacy', 'root': None, 'worlds': None,
                            'lastActive': None, 'active': None, 'data': DATA_PATH})
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
        elif self.path == '/api/worlds':
            try:
                act = payload.get('action')
                if act == 'switch':
                    ok = activate_world(payload.get('dir'))
                    self._json({'ok': bool(ok), 'active': ACTIVE_WORLD if ok else None})
                elif act == 'create':
                    dirn = create_new_world(payload.get('name'))
                    self._json({'ok': bool(dirn), 'active': dirn, 'dir': dirn})
                else:
                    self._json({'error': '未知操作'}, 400)
            except Exception as ex:
                self._json({'error': str(ex)}, 500)
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
