"""
微信 iLink Bot 插件 - 主入口
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict

import aiohttp
import yaml
import qrcode
from PIL import Image

from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page, register_route
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, '微信Bot')

PLUGIN_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.yaml')
WECHAT_TOKEN_FILE = os.path.join(DATA_DIR, '.weixin-token.json')
FONT_PATH = os.path.join(PLUGIN_DIR, 'Microsoft YaHei.ttf')

DEFAULT_CONFIG = {
    'wechat_base_url': 'https://ilinkai.weixin.qq.com',
    'base_url': 'http://127.0.0.1:5200',
    'use_image': True,
    'use_background': False,
    'auto_login': False,
}

TOKEN_EXPIRE_DAYS = 7
_config: dict = {}
_wechat_task: Optional[asyncio.Task] = None
_wechat_running = False
_wechat_session: Optional[Dict] = None
_wechat_initialized = False
_login_abort = False
_login_in_progress = False
_wechat_lock = threading.Lock()


__plugin_meta__ = {
    'name': 'wechat-bot',
    'author': '乄杺',
    'description': '利用微信ClawBot实现管理框架机器人以及查看机器人DAU数据',
    'version': '1.0.0',
    'github': 'https://github.com/linxi-root/wechat-bot',
}


# ══════════════════════════════════════════════════════════════════════════
# 依赖检查
# ══════════════════════════════════════════════════════════════════════════

def _check_and_install_deps():
    req_file = os.path.join(PLUGIN_DIR, 'requirements.txt')
    if not os.path.exists(req_file):
        return
    with open(req_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    required = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'): continue
        pkg = line.split('>=')[0].split('==')[0].split('~=')[0].split('!=')[0].strip()
        if pkg: required.append(pkg)
    if not required: return
    try:
        result = subprocess.check_output(
            [sys.executable, '-m', 'pip', 'list', '--format=columns'],
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        log.error("[微信Bot] pip list 失败"); return
    installed = set()
    for line in result.split('\n')[2:]:
        parts = line.split()
        if parts: installed.add(parts[0])
    missing = [p for p in required if p not in installed and p.lower() not in {i.lower() for i in installed}]
    if not missing: log.info("[微信Bot] 依赖已安装"); return
    log.info(f"[微信Bot] 安装: {missing}")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-q'] + missing,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("[微信Bot] 安装完成")
    except subprocess.CalledProcessError:
        log.error(f"[微信Bot] 安装失败: pip install {' '.join(missing)}")


# ══════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    global _config
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(DEFAULT_CONFIG, f, allow_unicode=True, default_flow_style=False)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            _config = {**DEFAULT_CONFIG, **(yaml.safe_load(f) or {})}
        return _config
    except Exception:
        _config = dict(DEFAULT_CONFIG); return _config

def _read_config_direct() -> dict:
    if not os.path.exists(CONFIG_PATH): return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return {**DEFAULT_CONFIG, **(yaml.safe_load(f) or {})}
    except Exception: return dict(DEFAULT_CONFIG)

def update_config(updates: dict) -> bool:
    global _config
    cfg = _read_config_direct()
    cfg.update(updates)
    _config.update(updates)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    return True


# ══════════════════════════════════════════════════════════════════════════
# Token
# ══════════════════════════════════════════════════════════════════════════

def load_token() -> Optional[Dict]:
    if not os.path.exists(WECHAT_TOKEN_FILE): return None
    try:
        with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
        saved_at = data.get('savedAt', '')
        if saved_at:
            try:
                if (datetime.now() - datetime.fromisoformat(saved_at)).days > TOKEN_EXPIRE_DAYS:
                    os.remove(WECHAT_TOKEN_FILE); return None
            except Exception: pass
        return data if data.get('token') and data.get('baseUrl') else None
    except Exception: return None

def save_token(data: Dict):
    data['savedAt'] = datetime.now().isoformat()
    with open(WECHAT_TOKEN_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(WECHAT_TOKEN_FILE, 0o600)

def clear_token():
    if os.path.exists(WECHAT_TOKEN_FILE): os.remove(WECHAT_TOKEN_FILE)

def _save_context_token(user_id: str, context_token: str):
    try:
        if os.path.exists(WECHAT_TOKEN_FILE):
            with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
        else: data = {}
        data['context_token'] = context_token
        data['last_user_id'] = user_id
        data['last_update'] = datetime.now().isoformat()
        with open(WECHAT_TOKEN_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e: log.error(f"[微信Bot] 保存 context_token 失败: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 二维码
# ══════════════════════════════════════════════════════════════════════════

def generate_qr_image(url: str, size: int = 360) -> bytes:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=2)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").resize((size, size), Image.LANCZOS)
    buf = BytesIO(); img.save(buf, format='PNG'); return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════
# 微信 Bot 核心
# ══════════════════════════════════════════════════════════════════════════

async def wechat_login(event=None) -> Dict:
    global _login_abort, _login_in_progress
    _login_abort = False; _login_in_progress = True
    cfg = _read_config_direct()
    base_url = cfg.get('wechat_base_url', 'https://ilinkai.weixin.qq.com')
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3") as resp:
            text = await resp.text()
            qr_data = json.loads(text)
        qrcode_str = qr_data.get("qrcode", "")
        if not qrcode_str: _login_in_progress = False; raise Exception(f"获取二维码失败: {qr_data}")
        qr_link = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qrcode_str}&bot_type=3"
        print(f"\n[微信Bot] 📱 {qr_link}\n")
        if event:
            try:
                await event.reply_image(generate_qr_image(qr_link), "📱 请扫描上方二维码")
                await event.reply(f"## 📱 微信扫码登录\n---\n```\n{qr_link}\n```\n---\n> 发送 `微信终止` 取消", msg_type=2)
            except Exception: pass
        deadline = time.time() + 5 * 60; refresh_count = 0; current_qr = qrcode_str
        while time.time() < deadline:
            if _login_abort: _login_in_progress = False; raise Exception("登录已被用户终止")
            try:
                async with session.get(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={current_qr}") as resp:
                    text = await resp.text()
                    status_data = json.loads(text)
            except Exception as e:
                log.error(f"[微信Bot] 查询二维码状态出错: {e}")
                await asyncio.sleep(2); continue
            status = status_data.get("status", "wait")
            if status == "wait": print(".", end="", flush=True)
            elif status == "scaned": print("\n[微信Bot] 👀 已扫码")
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3: _login_in_progress = False; raise Exception("二维码多次过期")
                try:
                    async with session.get(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3") as resp:
                        new_text = await resp.text()
                        new_qr = json.loads(new_text)
                    current_qr = new_qr.get("qrcode", "")
                    qr_link = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={current_qr}&bot_type=3"
                    if event:
                        await event.reply_image(generate_qr_image(qr_link), f"📱 已刷新 ({refresh_count}/3)")
                        await event.reply(f"## 新链接\n```\n{qr_link}\n```\n> 发送 `微信终止` 取消", msg_type=2)
                except Exception: pass
            elif status == "confirmed":
                print("\n[微信Bot] ✅ 登录成功\n")
                token_data = {"token": status_data["bot_token"], "baseUrl": status_data.get("baseurl", base_url),
                              "accountId": status_data.get("ilink_bot_id", ""), "userId": status_data.get("ilink_user_id", ""),
                              "savedAt": datetime.now().isoformat()}
                save_token(token_data); _login_in_progress = False; return token_data
            await asyncio.sleep(1)
    _login_in_progress = False; raise Exception("登录超时")


async def wechat_main_loop(session_data: Dict):
    global _wechat_running
    from .app.weixin_api import WeixinApiClient, WeixinMessageSender
    from .app.commands import WechatCommand
    client = WeixinApiClient(session_data["baseUrl"], session_data["token"])
    sender = WeixinMessageSender(client)
    buf = ""
    log.info("[微信Bot] 消息循环启动")
    while _wechat_running:
        try:
            resp = await client.get_updates(buf, timeout_ms=38_000)
            if resp.get_updates_buf: buf = resp.get_updates_buf
            if not resp.msgs: continue
            for msg in resp.msgs:
                if msg.message_type != 1 or not msg.from_user_id: continue
                text = ""; context_token = msg.context_token or ""
                if msg.item_list:
                    for item in msg.item_list:
                        if item.type == 1 and item.text_item and item.text_item.text: text = item.text_item.text; break
                if not text: continue
                if context_token: _save_context_token(msg.from_user_id, context_token)
                log.info(f"[微信消息] 收到: {text[:50]}")
                handler, args = WechatCommand.match(text)
                if handler: await handler(sender, msg.from_user_id, context_token, args)
                else: await sender.send_text(msg.from_user_id, f"收到消息: {text}\n发送「帮助」查看可用指令", context_token)
        except asyncio.CancelledError: break
        except Exception as e:
            if "session timeout" in str(e).lower() or "-14" in str(e):
                log.error("[微信Bot] Session 过期"); _wechat_running = False; break
            log.error(f"[微信Bot] 轮询出错: {e}"); await asyncio.sleep(3)
    await sender.close()
    log.info("[微信Bot] 消息循环停止")


async def start_wechat(event=None):
    global _wechat_session, _wechat_task, _wechat_running
    with _wechat_lock:
        if _wechat_running:
            return False, "已在运行中"
        try:
            existing = load_token()
            if existing:
                _wechat_session = existing
            else:
                _wechat_session = await wechat_login(event)
            _wechat_running = True
            _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
            return True, "启动成功"
        except Exception as e:
            _wechat_running = False
            msg = str(e)
            return False, "登录已被用户终止" if "登录已被用户终止" in msg else f"启动失败: {e}"


async def stop_wechat():
    global _wechat_running, _wechat_task, _wechat_session, _login_abort
    with _wechat_lock:
        if not _wechat_running and not _login_in_progress:
            return False, "未在运行"
        _login_abort = True
        _wechat_running = False
        if _wechat_task:
            _wechat_task.cancel()
            try:
                await _wechat_task
            except asyncio.CancelledError:
                pass
        _wechat_session = None
        _login_abort = False
        return True, "已停止"


# ══════════════════════════════════════════════════════════════════════════
# Web API
# ══════════════════════════════════════════════════════════════════════════

@register_route('GET', '/api/ext/wechat/state', auth=False)
async def api_get_state(request):
    from aiohttp import web
    cfg = _read_config_direct()
    has_token = os.path.exists(WECHAT_TOKEN_FILE)
    session_info = None
    if has_token:
        try:
            with open(WECHAT_TOKEN_FILE, 'r') as f: d = json.load(f)
            session_info = {'accountId': d.get('accountId', ''), 'savedAt': d.get('savedAt', '')}
        except Exception: pass
    if _wechat_running: bot_status = 'running'
    elif _login_in_progress: bot_status = 'logging_in'
    elif has_token: bot_status = 'stopped'
    else: bot_status = 'no_token'
    return web.json_response({'ok': True, 'data': {
        'config': cfg, 'bot_status': bot_status, 'running': _wechat_running,
        'initialized': _wechat_initialized, 'login_in_progress': _login_in_progress, 'session': session_info}})

@register_route('POST', '/api/ext/wechat/config', auth=False)
async def api_save_config(request):
    from aiohttp import web
    try:
        body = await request.json()
        keys = ['base_url', 'use_image', 'use_background', 'auto_login']
        updates = {k: v for k, v in body.items() if k in keys}
        if not updates:
            return web.json_response({'ok': False, 'message': '无有效配置'}, status=400)
        update_config(updates)
        return web.json_response({'ok': True, 'message': '已保存', 'data': updates})
    except Exception as e:
        return web.json_response({'ok': False, 'message': str(e)}, status=400)

@register_route('POST', '/api/ext/wechat/restart', auth=False)
async def api_restart(request):
    from aiohttp import web
    if _wechat_running:
        await stop_wechat()
        await asyncio.sleep(2)
    clear_token()
    success, msg = await start_wechat()
    return web.json_response({'ok': success, 'message': msg, 'cleared': True})

@register_route('POST', '/api/ext/wechat/stop', auth=False)
async def api_stop(request):
    from aiohttp import web
    success, msg = await stop_wechat()
    return web.json_response({'ok': success, 'message': msg})

@register_route('POST', '/api/ext/wechat/start', auth=False)
async def api_start(request):
    from aiohttp import web
    if _wechat_running:
        return web.json_response({'ok': False, 'message': '已在运行中'})
    success, msg = await start_wechat()
    return web.json_response({'ok': success, 'message': msg})


# ══════════════════════════════════════════════════════════════════════════
# Web 页面
# ══════════════════════════════════════════════════════════════════════════

CONFIG_PAGE_HTML = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>微信Bot 配置</title>
<style>:root{--bg:#0f0f23;--card-bg:#1a1a2e;--border:#2a2a3e;--text:#e0e0e0;--text-secondary:#aaa;--accent:#7c8aff;--accent-hover:#9b9fff;--danger:#ff6b6b;--success:#51cf66;--input-bg:#16213e}*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:24px;line-height:1.6}.container{max-width:800px;margin:0 auto}h1{font-size:24px;color:var(--accent);margin-bottom:8px}.subtitle{color:var(--text-secondary);font-size:14px;margin-bottom:24px}.card{background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px}.card-title{font-size:18px;font-weight:600;margin-bottom:20px;color:var(--accent)}.form-group{margin-bottom:18px}.form-group:last-child{margin-bottom:0}.form-group label{display:block;font-size:14px;color:var(--text-secondary);margin-bottom:6px}.form-group input[type="url"],.form-group input[type="text"]{width:100%;padding:10px 14px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;transition:border-color 0.2s}.form-group input:focus{outline:none;border-color:var(--accent)}.api-url{font-size:14px;font-family:monospace;color:var(--accent);word-break:break-all;line-height:1.8}.hint{font-size:12px;color:var(--text-secondary);margin-top:4px}.divider{border:none;border-top:1px solid var(--border);margin:20px 0}.section-title{font-size:14px;font-weight:600;color:var(--text);margin-bottom:14px;padding-left:10px;border-left:3px solid var(--accent)}.toggle-group{display:flex;align-items:center;justify-content:space-between;padding:10px 0}.toggle-label{font-size:14px}.toggle-desc{font-size:12px;color:var(--text-secondary);margin-top:2px}.toggle{position:relative;width:48px;height:26px;flex-shrink:0}.toggle input{display:none}.toggle .slider{position:absolute;top:0;left:0;right:0;bottom:0;background:#3a3a5c;border-radius:26px;cursor:pointer;transition:background 0.3s}.toggle .slider::before{content:'';position:absolute;width:20px;height:20px;left:3px;bottom:3px;background:white;border-radius:50%;transition:transform 0.3s}.toggle input:checked+.slider{background:var(--accent)}.toggle input:checked+.slider::before{transform:translateX(22px)}.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 20px;border:none;border-radius:8px;font-size:14px;cursor:pointer;transition:opacity 0.2s}.btn-primary{background:var(--accent);color:white}.btn-primary:hover{background:var(--accent-hover)}.btn-danger{background:transparent;color:var(--danger);border:1px solid var(--danger)}.btn-danger:hover{background:rgba(255,107,107,0.1)}.btn-group{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}.btn:disabled{opacity:0.5;cursor:not-allowed}.status-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:13px;font-weight:500}.status-running{background:rgba(81,207,102,0.15);color:var(--success)}.status-stopped{background:rgba(255,107,107,0.15);color:var(--danger)}.status-loading{background:rgba(124,138,255,0.15);color:var(--accent)}.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block}.status-running .status-dot{background:var(--success)}.status-stopped .status-dot{background:var(--danger)}.status-loading .status-dot{background:var(--accent);animation:pulse 1.5s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;font-size:14px;z-index:1000;display:none}.toast.show{display:block;animation:slideIn 0.3s ease}@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}.toast-success{background:var(--success);color:#0f0f23}.toast-error{background:var(--danger);color:white}.session-info{background:var(--input-bg);border-radius:8px;padding:16px;margin-top:14px;font-size:13px}.session-info .row{display:flex;justify-content:space-between;padding:4px 0}.session-info .label{color:var(--text-secondary)}.session-info .value{font-family:monospace}.loading-spinner{display:inline-block;width:16px;height:16px;border:2px solid transparent;border-top-color:currentColor;border-radius:50%;animation:spin 0.8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:999;display:none;align-items:center;justify-content:center}.modal-overlay.show{display:flex}.modal{background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:420px;width:90%}.modal h3{color:var(--accent);margin-bottom:12px;font-size:18px}.modal p{color:var(--text-secondary);font-size:14px;margin-bottom:16px;line-height:1.6}.modal .btn-group{justify-content:flex-end;margin-top:0}</style></head><body><div class="container"><h1>🤖 微信Bot 配置</h1><p class="subtitle">管理微信 Bot 和系统监控</p>

<!-- 状态卡片 -->
<div class="card"><div class="card-title">📡 运行状态</div>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><span id="statusBadge" class="status-badge status-stopped"><span class="status-dot"></span><span id="statusText">加载中...</span></span></div>
<div class="session-info" id="sessionInfo" style="display:none"><div class="row"><span class="label">Bot ID</span><span class="value" id="sessBotId">-</span></div><div class="row"><span class="label">登录时间</span><span class="value" id="sessTime">-</span></div></div>
<div class="btn-group"><button class="btn btn-primary" onclick="startW()" id="btnStart">▶️ 启动</button><button class="btn btn-danger" onclick="stopW()" id="btnStop">⏹️ 停止</button><button class="btn btn-primary" onclick="restartW()" id="btnRestart">🔄 重启</button></div></div>

<!-- 配置卡片 -->
<div class="card"><div class="card-title">⚙️ 配置</div>

<div class="section-title">🔗 接口地址</div>
<div class="form-group"><label>微信 API 地址</label><div class="api-url" id="wechatBaseUrl">https://ilinkai.weixin.qq.com</div><div class="hint">固定地址，不可修改</div></div>
<div class="form-group"><label>公网地址</label><input type="url" id="baseUrl" placeholder="http://127.0.0.1:5200"><div class="hint">用于 QQ 端图片生成，需包含端口号</div></div>

<hr class="divider">

<div class="section-title">📊 系统监控</div>
<div class="toggle-group"><div><div class="toggle-label">图片输出</div><div class="toggle-desc">以图片形式发送系统状态</div></div><label class="toggle"><input type="checkbox" id="useImage"><span class="slider"></span></label></div>
<div class="toggle-group"><div><div class="toggle-label">背景图</div><div class="toggle-desc">使用 data/background.png 作为背景</div></div><label class="toggle"><input type="checkbox" id="useBackground"><span class="slider"></span></label></div>

<hr class="divider">

<div class="section-title">🤖 自动登录</div>
<div class="toggle-group"><div><div class="toggle-label">启动时自动登录微信</div><div class="toggle-desc">插件加载后自动启动微信 Bot</div></div><label class="toggle"><input type="checkbox" id="autoLogin"><span class="slider"></span></label></div>

<div class="btn-group"><button class="btn btn-primary" onclick="saveCfg()">💾 保存配置</button><button class="btn btn-danger" onclick="resetCfg()">🔄 恢复默认</button></div></div></div>

<div class="toast" id="toast"></div>
<div class="modal-overlay" id="restartModal"><div class="modal"><h3>⚠️ 确认重启</h3><p>重启将<span style="color:#ff6b6b">清除已登录信息</span>，需要重新扫码登录。<br><br>确定要继续吗？</p><div class="btn-group"><button class="btn btn-primary" onclick="confirmRestart()">✅ 确认重启</button><button class="btn btn-danger" onclick="closeModal()">❌ 取消</button></div></div></div>

<script>
var A = {
    state: '/api/ext/wechat/state',
    config: '/api/ext/wechat/config',
    start: '/api/ext/wechat/start',
    stop: '/api/ext/wechat/stop',
    restart: '/api/ext/wechat/restart'
};
var currentStatus = 'no_token';

function T(m, t) {
    var o = document.getElementById('toast');
    o.textContent = m;
    o.className = 'toast toast-' + t + ' show';
    setTimeout(function() { o.classList.remove('show'); }, 3000);
}

function setBtnLoading(b, t) {
    b.disabled = true;
    b.dataset.origText = b.textContent;
    b.innerHTML = '<span class="loading-spinner"></span> ' + t;
}

function resetBtn(b) {
    b.innerHTML = b.dataset.origText || b.textContent;
    b.disabled = false;
}

function U(d) {
    var c = d.config || {};
    document.getElementById('wechatBaseUrl').textContent = c.wechat_base_url || 'https://ilinkai.weixin.qq.com';
    document.getElementById('baseUrl').value = c.base_url || 'http://127.0.0.1:5200';
    document.getElementById('useImage').checked = c.use_image !== false;
    document.getElementById('useBackground').checked = c.use_background === true;
    document.getElementById('autoLogin').checked = c.auto_login === true;

    var b = document.getElementById('statusBadge'),
        s = document.getElementById('statusText'),
        i = document.getElementById('sessionInfo'),
        st = document.getElementById('btnStart'),
        sp = document.getElementById('btnStop'),
        sr = document.getElementById('btnRestart'),
        bs = d.bot_status || 'no_token';

    currentStatus = bs;

    if (bs === 'logging_in') {
        b.className = 'status-badge status-loading';
        s.textContent = '登录中...';
        i.style.display = 'none';
        st.disabled = true;
        sp.disabled = false;
        sr.disabled = true;
    } else if (bs === 'running') {
        b.className = 'status-badge status-running';
        s.textContent = '运行中';
        if (d.session) {
            i.style.display = 'block';
            document.getElementById('sessBotId').textContent = d.session.accountId || '-';
            document.getElementById('sessTime').textContent = d.session.savedAt || '-';
        } else {
            i.style.display = 'none';
        }
        st.disabled = true;
        sp.disabled = false;
        sr.disabled = false;
    } else {
        b.className = 'status-badge status-stopped';
        s.textContent = bs === 'stopped' ? '已停止' : '未运行';
        i.style.display = 'none';
        st.disabled = false;
        sp.disabled = true;
        sr.disabled = false;
    }
}

function L() {
    fetch(A.state)
        .then(function(r) { return r.json(); })
        .then(function(r) { if (r.ok) U(r.data); })
        .catch(function(e) { console.error(e); });
}

function saveCfg() {
    var d = {
        base_url: document.getElementById('baseUrl').value,
        use_image: document.getElementById('useImage').checked,
        use_background: document.getElementById('useBackground').checked,
        auto_login: document.getElementById('autoLogin').checked
    };
    fetch(A.config, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(d)
    })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) {
                T('✅ 配置已保存', 'success');
                L();
            } else {
                T('❌ ' + r.message, 'error');
            }
        })
        .catch(function(e) {
            T('保存失败: ' + e.message, 'error');
        });
}

function startW() {
    if (currentStatus === 'running') {
        T('⚠️ 已在运行中', 'error');
        return;
    }
    var b = document.getElementById('btnStart');
    setBtnLoading(b, '启动中...');
    fetch(A.start, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            T(r.ok ? '✅ ' + r.message : '❌ ' + r.message, r.ok ? 'success' : 'error');
        })
        .catch(function(e) {
            T('启动失败: ' + e.message, 'error');
        })
        .finally(function() {
            resetBtn(b);
            setTimeout(L, 1500);
        });
}

function stopW() {
    if (currentStatus !== 'running' && currentStatus !== 'logging_in') {
        T('⚠️ 当前未在运行', 'error');
        return;
    }
    if (!confirm('确定停止微信Bot吗？')) return;
    var b = document.getElementById('btnStop');
    setBtnLoading(b, '停止中...');
    fetch(A.stop, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            T(r.ok ? '✅ ' + r.message : '❌ ' + r.message, r.ok ? 'success' : 'error');
        })
        .catch(function(e) {
            T('停止失败: ' + e.message, 'error');
        })
        .finally(function() {
            resetBtn(b);
            setTimeout(L, 1500);
        });
}

function restartW() {
    if (currentStatus !== 'running' && currentStatus !== 'stopped') {
        T('⚠️ 当前状态无法重启', 'error');
        return;
    }
    document.getElementById('restartModal').classList.add('show');
}

function confirmRestart() {
    document.getElementById('restartModal').classList.remove('show');
    var b = document.getElementById('btnRestart');
    setBtnLoading(b, '重启中...');
    fetch(A.restart, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            T(r.ok ? '✅ ' + r.message : '❌ ' + r.message, r.ok ? 'success' : 'error');
        })
        .catch(function(e) {
            T('重启失败: ' + e.message, 'error');
        })
        .finally(function() {
            resetBtn(b);
            setTimeout(L, 1500);
        });
}

function closeModal() {
    document.getElementById('restartModal').classList.remove('show');
}

function resetCfg() {
    if (!confirm('恢复默认配置？此操作会立即保存。')) return;
    document.getElementById('baseUrl').value = 'http://127.0.0.1:5200';
    document.getElementById('useImage').checked = true;
    document.getElementById('useBackground').checked = false;
    document.getElementById('autoLogin').checked = false;
    saveCfg();
}

L();
setInterval(L, 15000);
</script>
</body></html>'''


register_page(key='wechat-bot-config', label='微信Bot 配置', source='plugin', source_name='wechat_bot', html=CONFIG_PAGE_HTML, icon='robot')


# ══════════════════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════════════════

@on_load
async def init():
    global _wechat_initialized
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _check_and_install_deps)
    load_config()
    _wechat_initialized = True
    auto_login = _config.get('auto_login', False)
    if auto_login:
        log.info("[微信Bot] 自动启动...")
        success, msg = await start_wechat()
        log.info(f"[微信Bot] 自动启动: {msg}")
    else:
        log.info("[微信Bot] 已加载（静默模式）")

@on_unload
async def cleanup():
    global _wechat_initialized
    if _wechat_running: await stop_wechat()
    unregister_page('wechat-bot-config'); _wechat_initialized = False
    log.info("[微信Bot] 已卸载")


# ══════════════════════════════════════════════════════════════════════════
# QQ 控制指令
# ══════════════════════════════════════════════════════════════════════════

@handler(r'^微信状态$', name='微信状态', desc='查看微信Bot运行状态', owner_only=True)
async def cmd_wechat_status(event, match):
    if _wechat_running and _wechat_session: s = f"## 🤖 微信 Bot\n---\n✅ 运行中\nBot: `{_wechat_session.get('accountId','?')}`\n登录: {_wechat_session.get('savedAt','?')}"
    elif _login_in_progress: s = "## 🤖 微信 Bot\n---\n🔄 登录中..."
    else: s = "## 🤖 微信 Bot\n---\n❌ 未运行\n发送 `微信登录` 启动"
    await event.reply(s, msg_type=2)

@handler(r'^微信登录$', name='微信登录', desc='启动微信Bot', owner_only=True)
async def cmd_wechat_login(event, match):
    if _wechat_running: await event.reply("## ⚠️ 已在运行中", msg_type=2); return
    existing = load_token()
    if existing:
        success, msg = await start_wechat()
        if success: await event.reply(f"## ✅ 已启动\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2); return
        clear_token()
    await event.reply("## 🔐 正在生成二维码...\n> 发送 `微信终止` 取消", msg_type=2)
    success, msg = await start_wechat(event)
    if success: await event.reply(f"## ✅ 登录成功\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2)
    else: await event.reply(f"## {'🛑 已终止' if '终止' in msg else '❌ '+msg}", msg_type=2)

@handler(r'^微信终止$', name='微信终止', desc='终止登录', owner_only=True)
async def cmd_wechat_abort(event, match):
    if _wechat_running: await event.reply("## ⚠️ 已运行中，请用 `微信登出`", msg_type=2)
    elif not _login_in_progress: await event.reply("## ℹ️ 无登录进程", msg_type=2)
    else: _login_abort = True; await event.reply("## 🛑 已终止", msg_type=2)

@handler(r'^微信登出$', name='微信登出', desc='停止微信Bot', owner_only=True)
async def cmd_wechat_logout(event, match):
    success, msg = await stop_wechat()
    await event.reply(f"## {'👋 已登出' if success else '⚠️ '+msg}", msg_type=2)

@handler(r'^微信重启$', name='微信重启', desc='重启微信Bot', owner_only=True)
async def cmd_wechat_restart(event, match):
    await event.reply("## 🔄 重启中...（将清除登录信息，需重新扫码）", msg_type=2)
    if _wechat_running: await stop_wechat()
    await asyncio.sleep(2); clear_token()
    await event.reply("## 🔐 正在生成二维码...\n> 发送 `微信终止` 取消", msg_type=2)
    success, msg = await start_wechat(event)
    if success: await event.reply(f"## ✅ 重启成功\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2)
    else: await event.reply(f"## {'🛑 已终止' if '终止' in msg else '❌ '+msg}", msg_type=2)

@handler(r'^微信帮助$', name='微信帮助', desc='查看帮助', owner_only=True)
async def cmd_wechat_help(event, match):
    await event.reply("## 🤖 微信 Bot 帮助\n---\n**QQ命令**\n`微信登录` `微信终止` `微信登出` `微信重启` `微信状态` `微信帮助` `系统状态`\n\n**微信端**\n`系统状态` / `帮助` / `机器人列表` / `启动` / `关闭` / `dau` / `重启`\n\n**Web面板**\n侧边栏「微信Bot 配置」", msg_type=2)

from .app.system_status import cmd_system_status  # noqa: F401, E402