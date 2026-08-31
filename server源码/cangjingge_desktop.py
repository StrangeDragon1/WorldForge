# -*- coding: utf-8 -*-
"""
藏经阁 —— 桌面窗口版
作者 / Author: StrangeDragon1（奇怪的龙龙 / 奇怪的龍龍）
许可 / License: CC BY-NC 4.0（署名-非商业性使用）—— 见根目录 LICENSE

思路：复用 cangjingge_server 的本地 HTTP 服务（不自动开浏览器），
再用 pywebview 弹出一个原生窗口来渲染界面。

前置依赖：Microsoft Edge WebView2 运行库（Windows 10/11 一般已内置）。
启动时会检测 WebView2；若缺失，会弹出一个系统提示框询问：
  - 是(Y)：跳转到微软官方安装页面
  - 否(N)：不安装，改用浏览器打开
  - 取消 ：退出

本文件为新增测试文件，不改动 cangjingge_server.py 与 藏经阁.html：
  - 端口：8734 起自动顺延（若真正藏经阁已在跑，会另开端口，互不影响）
  - 数据目录：复用 exe 旁边的 data（或按 CANGJINGGE_HOME / 数据目录.txt）
  - 桌面窗口关闭 = 退出程序；浏览器回退模式下，心跳消失约 3 分钟后自动退出
"""
import ctypes
import threading
import time
import webbrowser
import winreg

import webview

import cangjingge_server as cs


# ---- WebView2 运行库检测 ----
WEBVIEW2_CLIENT_ID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'


def has_webview2():
    """通过注册表检测 WebView2 运行库是否已安装。"""
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s' % WEBVIEW2_CLIENT_ID),
        (winreg.HKEY_LOCAL_MACHINE,
         r'SOFTWARE\Microsoft\EdgeUpdate\Clients\%s' % WEBVIEW2_CLIENT_ID),
        (winreg.HKEY_CURRENT_USER,
         r'Software\Microsoft\EdgeUpdate\Clients\%s' % WEBVIEW2_CLIENT_ID),
    ]
    for root, sub in reg_paths:
        try:
            k = winreg.OpenKey(root, sub)
            try:
                val, _ = winreg.QueryValueEx(k, 'pv')
                if val:
                    return True
            finally:
                winreg.CloseKey(k)
        except OSError:
            continue
    return False


def ask_prereq():
    """WebView2 缺失时弹窗询问。返回 'install' | 'browser' | 'exit'。"""
    MB_YESNOCANCEL = 0x00000003
    MB_ICONQUESTION = 0x00000020
    MB_DEFBUTTON3 = 0x00000200   # 默认高亮「取消」，避免手滑误点
    res = ctypes.windll.user32.MessageBoxW(
        0,
        '使用「藏经阁桌面版」需要 Microsoft Edge WebView2 运行库（前置依赖）。\n\n'
        '是否跳转到微软官方安装页面？\n\n'
        '是(Y) ＝ 跳转安装\n'
        '否(N) ＝ 不安装，改用浏览器打开\n'
        '取消   ＝ 退出',
        '藏经阁 · 缺少前置依赖',
        MB_YESNOCANCEL | MB_ICONQUESTION | MB_DEFBUTTON3,
    )
    if res == 6:      # IDYES
        return 'install'
    if res == 7:      # IDNO
        return 'browser'
    return 'exit'


def run_browser_fallback(url):
    """WebView2 不可用 / 用户选择浏览器时：打开浏览器，心跳看门狗保活。"""
    cs.log('回退：改用浏览器打开 %s' % url)
    webbrowser.open(url)

    def watchdog():
        started = time.time()
        while True:
            time.sleep(5)
            idle = time.time() - max(cs.last_ping[0], started)
            if time.time() - started > cs.START_GRACE_SEC and idle > cs.IDLE_SHUTDOWN_SEC:
                cs.log('页面心跳中断超过 %d 秒，自动退出' % cs.IDLE_SHUTDOWN_SEC)
                cs.hard_exit(0.1)

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    cs.setup_log()
    cs.log('=' * 46)
    cs.log('桌面窗口测试版启动')
    cs.log('程序目录 : %s' % cs.BASE)
    cs.log('数据目录 : %s' % cs.DATA_PATH)
    cs.log('网页来源 : %s' % cs.HTML_PATH)
    cs.ensure_dirs()
    cs.log('词条数量 : %d' % len(cs.load_all()))
    wv = has_webview2()
    cs.log('WebView2 运行库 : %s' % ('已安装' if wv else '缺失'))

    port = cs.pick_free_port()
    if port is None:
        cs.log('错误：%s-%s 端口全被占用，放弃启动' % (cs.PORT_BASE, cs.PORT_BASE + cs.PORT_RANGE - 1))
        return
    cs.PORT = port

    try:
        server = cs.ThreadingHTTPServer(('127.0.0.1', port), cs.Handler)
    except Exception as ex:
        cs.log('错误：无法监听 %d — %s（可能是防火墙拦截）' % (port, ex))
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = 'http://127.0.0.1:%d/' % port
    cs.log('服务地址 : %s' % url)

    try:
        if not wv:
            choice = ask_prereq()
            if choice == 'install':
                cs.log('用户选择跳转安装，打开安装页后退出')
                webbrowser.open(WEBVIEW2_URL)
                return
            if choice == 'browser':
                run_browser_fallback(url)
                return
            cs.log('用户取消，退出')
            return

        # WebView2 正常：弹出桌面窗口
        webview.create_window(
            '藏经阁 · 桌面版（测试）',
            url,
            width=1180, height=820,
            min_size=(900, 640),
        )
        webview.start()
        cs.log('窗口已关闭，退出')
    except Exception as ex:
        cs.log('pywebview 异常：%s' % ex)
        run_browser_fallback(url)
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
    cs.log('测试版已退出')


if __name__ == '__main__':
    main()
