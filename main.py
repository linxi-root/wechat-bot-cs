"""
微信 iLink Bot 插件 - 主入口
"""

import asyncio
import base64
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
_current_qr_code: str = ""
_current_qr_link: str = ""
_current_qr_image_base64: str = ""
_login_future: Optional[asyncio.Task] = None
_qr_generated_at: float = 0
_login_source: str = ""


__plugin_meta__ = {
    'name': 'wechat-bot',
    'author': '乄杺',
    'description': '利用微信ClawBot实现管理框架机器人以及查看机器人DAU数据',
    'version': '1.0.2',
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


# ══════════════════════════════════════════════════════════════════════════# Token
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

def generate_qr_base64(url: str, size: int = 280) -> str:
    """生成二维码的 base64 data URI"""
    img_bytes = generate_qr_image(url, size)
    return "data:image/png;base64," + base64.b64encode(img_bytes).decode()


async def _fetch_qr_code() -> tuple:
    """获取二维码，返回 (qr_code_str, qr_link)"""
    cfg = _read_config_direct()
    base_url = cfg.get('wechat_base_url', 'https://ilinkai.weixin.qq.com')
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3") as resp:
            text = await resp.text()
            qr_data = json.loads(text)
    qrcode_str = qr_data.get("qrcode", "")
    if not qrcode_str:
        raise Exception(f"获取二维码失败: {qr_data}")
    qr_link = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={qrcode_str}&bot_type=3"
    return qrcode_str, qr_link


# ══════════════════════════════════════════════════════════════════════════
# 微信 Bot 核心
# ══════════════════════════════════════════════════════════════════════════

async def wechat_login(event=None) -> Dict:
    buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    global _login_abort, _login_in_progress, _current_qr_code, _current_qr_link, _current_qr_image_base64, _qr_generated_at
    _login_abort = False; _login_in_progress = True
    cfg = _read_config_direct()
    base_url = cfg.get('wechat_base_url', 'https://ilinkai.weixin.qq.com')
    async with aiohttp.ClientSession() as session:
        qrcode_str, qr_link = await _fetch_qr_code()
        _current_qr_code = qrcode_str
        _current_qr_link = qr_link
        _current_qr_image_base64 = generate_qr_base64(qr_link)
        _qr_generated_at = time.time()
        log.info(f"[微信Bot] 📱 {qr_link}")
        if event:
            try:
                await event.reply_image(generate_qr_image(qr_link), "📱 请扫描上方二维码")
                await event.reply(f"## 📱 微信扫码登录\n---\n```\n{qr_link}\n```\n---\n> 发送 `微信终止` 取消", msg_type=2, buttons=buttons)
            except Exception: pass
        deadline = time.time() + 5 * 60; refresh_count = 0; current_qr = qrcode_str
        while time.time() < deadline:
            if _login_abort:
                # cmd_wechat_abort 已经把 _login_in_progress 设为 False 了，这里直接抛异常
                raise Exception("登录已被用户终止")
            try:
                async with session.get(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={current_qr}") as resp:
                    text = await resp.text()
                    status_data = json.loads(text)
            except Exception as e:
                log.error(f"[微信Bot] 查询二维码状态出错: {e}")
                await asyncio.sleep(2); continue
            status = status_data.get("status", "wait")
            if status == "wait": print(".", end="", flush=True)
            elif status == "scaned": 
                log.info("[微信Bot] 👀 已扫码")
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 1: _login_in_progress = False; raise Exception("二维码多次过期")
                try:
                    new_qrcode_str, new_qr_link = await _fetch_qr_code()
                    current_qr = new_qrcode_str
                    _current_qr_code = new_qrcode_str
                    _current_qr_link = new_qr_link
                    _current_qr_image_base64 = generate_qr_base64(new_qr_link)
                    _qr_generated_at = time.time()
                    qr_link = new_qr_link
                    if event:
                        await event.reply_image(generate_qr_image(qr_link), f"📱 已刷新")
                        await event.reply(f"## 新链接\n```\n{qr_link}\n```\n> 发送 `微信终止` 取消", msg_type=2, buttons=buttons)
                except Exception: pass
            elif status == "confirmed":
                log.info("[微信Bot] ✅ 登录成功")
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
    global _wechat_session, _wechat_task, _wechat_running, _login_source
    with _wechat_lock:
        if _wechat_running:
            return False, "已在运行中"
        existing = load_token()
        if existing:
            _wechat_session = existing
            _wechat_running = True
            _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
            _login_source = ""
            return True, "启动成功（使用已保存的登录信息）"
        try:
            _login_source = "qq"  # QQ端触发
            _wechat_session = await wechat_login(event)
            _wechat_running = True
            _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
            _login_source = ""
            return True, "启动成功"
        except Exception as e:
            _wechat_running = False
            _login_source = ""
            msg = str(e)
            return False, "登录已被用户终止" if "登录已被用户终止" in msg else f"启动失败: {e}"

async def start_wechat_async():
    global _login_in_progress, _current_qr_code, _current_qr_link, _current_qr_image_base64, _qr_generated_at, _login_future, _login_source
    with _wechat_lock:
        if _wechat_running:
            return False, "已在运行中", None, None, None
        try:
            qrcode_str, qr_link = await _fetch_qr_code()
        except Exception as e:
            return False, f"获取二维码失败: {e}", None, None, None
        _current_qr_code = qrcode_str
        _current_qr_link = qr_link
        _current_qr_image_base64 = generate_qr_base64(qr_link)
        _qr_generated_at = time.time()
        _login_in_progress = True
        _login_abort = False
        _login_source = "web"  # Web端触发
        log.info(f"📱 {qr_link}")
        _login_future = asyncio.create_task(_do_login_background(qrcode_str))
        return True, "已生成二维码", qrcode_str, qr_link, _current_qr_image_base64


async def _do_login_background(initial_qr_code: str):
    """后台执行登录轮询，不阻塞"""
    global _wechat_session, _wechat_task, _wechat_running, _login_in_progress, _current_qr_code, _current_qr_link, _current_qr_image_base64, _qr_generated_at
    cfg = _read_config_direct()
    base_url = cfg.get('wechat_base_url', 'https://ilinkai.weixin.qq.com')
    async with aiohttp.ClientSession() as session:
        deadline = time.time() + 5 * 60
        refresh_count = 0
        current_qr = initial_qr_code
        while time.time() < deadline:
            if _login_abort:
                _login_in_progress = False
                _login_source = ""
                log.info("登录已被用户终止")
                return
            try:
                async with session.get(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={current_qr}") as resp:
                    text = await resp.text()
                    status_data = json.loads(text)
            except asyncio.CancelledError:
                _login_in_progress = False
                _login_source = ""
                log.info("登录任务被取消")
                return
            except Exception as e:
                if _login_abort:
                    _login_in_progress = False
                    return
                log.error(f"查询二维码状态出错: {e}")
                await asyncio.sleep(2)
                continue
            
            # 每次处理状态前再检查一次
            if _login_abort:
                _login_in_progress = False
                return
                
            status = status_data.get("status", "wait")
            if status == "wait":
                pass
            elif status == "scaned":
                log.info("👀 已扫码")
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 1:
                    _login_in_progress = False
                    _login_source = ""
                    log.error("二维码多次过期")
                    return
                try:
                    if _login_abort:
                        _login_in_progress = False
                        return
                    new_qrcode_str, new_qr_link = await _fetch_qr_code()
                    current_qr = new_qrcode_str
                    _current_qr_code = new_qrcode_str
                    _current_qr_link = new_qr_link
                    _current_qr_image_base64 = generate_qr_base64(new_qr_link)
                    _qr_generated_at = time.time()
                except asyncio.CancelledError:
                    _login_in_progress = False
                    return
                except Exception:
                    pass
            elif status == "confirmed":
                log.info("✅ 登录成功")
                token_data = {
                    "token": status_data["bot_token"],
                    "baseUrl": status_data.get("baseurl", base_url),
                    "accountId": status_data.get("ilink_bot_id", ""),
                    "userId": status_data.get("ilink_user_id", ""),
                    "savedAt": datetime.now().isoformat()
                }
                _login_source = ""
                save_token(token_data)
                _wechat_session = token_data
                _wechat_running = True
                _login_in_progress = False
                _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
                return
            await asyncio.sleep(1)
    _login_in_progress = False
    _login_source = ""
    log.error("登录超时")


async def stop_wechat():
    global _wechat_running, _wechat_task, _wechat_session, _login_abort, _login_in_progress, _login_future
    with _wechat_lock:
        if not _wechat_running and not _login_in_progress:
            return False, "未在运行"
        _login_abort = True
        _login_in_progress = False
        _wechat_running = False
        
        # 先取消登录任务
        if _login_future and not _login_future.done():
            _login_future.cancel()
            try:
                await _login_future
            except asyncio.CancelledError:
                pass
        
        # 再取消消息循环
        if _wechat_task:
            _wechat_task.cancel()
            try:
                await _wechat_task
            except asyncio.CancelledError:
                pass
                
        _wechat_session = None
        _login_abort = False
        _login_future = None
        return True, "已停止"

# ══════════════════════════════════════════════════════════════════════════
# Web API
# ══════════════════════════════════════════════════════════════════════════

@register_route('GET', '/api/ext/wechat/qr-image', auth=False)
async def api_get_qr_image(request):
    """返回当前二维码的 base64 图片"""
    from aiohttp import web
    qr_url = request.query.get('url', _current_qr_link)
    if not qr_url:
        return web.json_response({'ok': False, 'message': '无二维码'}, status=400)
    try:
        img_b64 = generate_qr_base64(qr_url)
        return web.json_response({'ok': True, 'data': {'image': img_b64}})
    except Exception as e:
        return web.json_response({'ok': False, 'message': str(e)}, status=500)


@register_route('GET', '/api/ext/wechat/login-qr', auth=False)
async def api_get_login_qr(request):
    """获取当前登录二维码信息"""
    from aiohttp import web
    return web.json_response({
        'ok': True,
        'data': {
            'qr_code': _current_qr_code,
            'qr_url': _current_qr_link,
            'qr_image': _current_qr_image_base64,
            'generated_at': _qr_generated_at
        }
    })


@register_route('GET', '/api/ext/wechat/login-status', auth=False)
async def api_get_login_status(request):
    """查询二维码状态"""
    from aiohttp import web
    qrcode_str = request.query.get('qrcode', '')
    if not qrcode_str:
        return web.json_response({'ok': False, 'message': '缺少qrcode参数'}, status=400)
    cfg = _read_config_direct()
    base_url = cfg.get('wechat_base_url', 'https://ilinkai.weixin.qq.com')
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode_str}") as resp:
                text = await resp.text()
                status_data = json.loads(text)
        status = status_data.get('status', 'wait')
        return web.json_response({
            'ok': True,
            'data': {
                'status': status
            }
        })
    except Exception as e:
        return web.json_response({'ok': False, 'message': str(e)}, status=500)


@register_route('POST', '/api/ext/wechat/abort-login', auth=False)
async def api_abort_login(request):
    """终止登录"""
    from aiohttp import web
    global _login_abort, _login_in_progress, _login_source
    _login_abort = True
    _login_in_progress = False
    _login_source = ""
    return web.json_response({'ok': True, 'message': '已终止'})


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
    if _wechat_running:
        bot_status = 'running'
    elif _login_in_progress:
        bot_status = 'logging_in'
    elif has_token:
        bot_status = 'stopped'
    else:
        bot_status = 'no_token'
    return web.json_response({'ok': True, 'data': {
        'config': cfg, 'bot_status': bot_status, 'running': _wechat_running,
        'initialized': _wechat_initialized, 'login_in_progress': _login_in_progress,
        'session': session_info,
        'qr_code': _current_qr_code,
        'qr_url': _current_qr_link,
        'qr_image': _current_qr_image_base64,
        'qr_generated_at': _qr_generated_at,
        'login_source': _login_source
    }})

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
        log.info(f"[微信Bot] 配置已更新: {updates}")
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
    success, msg, qr_code, qr_url, qr_image = await start_wechat_async()
    return web.json_response({
        'ok': success, 'message': msg, 'cleared': True,
        'need_login': success and not _wechat_running,
        'qr_code': qr_code, 'qr_url': qr_url, 'qr_image': qr_image
    })


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
    existing = load_token()
    if existing:
        success, msg = await start_wechat()
        return web.json_response({'ok': success, 'message': msg, 'need_login': False})
    success, msg, qr_code, qr_url, qr_image = await start_wechat_async()
    return web.json_response({
        'ok': success, 'message': msg,
        'need_login': True,
        'qr_code': qr_code, 'qr_url': qr_url, 'qr_image': qr_image
    })


# ══════════════════════════════════════════════════════════════════════════
# Web 页面
# ══════════════════════════════════════════════════════════════════════════

CONFIG_PAGE_HTML = r'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>微信Bot 配置</title>
<style>:root{--bg:#0f0f23;--card-bg:#1a1a2e;--border:#2a2a3e;--text:#e0e0e0;--text-secondary:#aaa;--accent:#7c8aff;--accent-hover:#9b9fff;--danger:#ff6b6b;--success:#51cf66;--input-bg:#16213e;--placeholder-color:#666}*{margin:0;padding:0;box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);padding:24px;line-height:1.6}.container{max-width:800px;margin:0 auto}h1{font-size:24px;color:var(--accent);margin-bottom:8px}.subtitle{color:var(--text-secondary);font-size:14px;margin-bottom:24px}.card{background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px}.card-title{font-size:18px;font-weight:600;margin-bottom:20px;color:var(--accent)}.form-group{margin-bottom:18px}.form-group:last-child{margin-bottom:0}.form-group label{display:block;font-size:14px;color:var(--text-secondary);margin-bottom:6px}.form-group input[type="text"]{width:100%;padding:10px 14px;background:var(--input-bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;transition:border-color 0.2s}.form-group input::placeholder{color:var(--placeholder-color)}.form-group input:focus{outline:none;border-color:var(--accent)}.api-url{font-size:14px;font-family:monospace;color:var(--accent);word-break:break-all;line-height:1.8}.hint{font-size:12px;color:var(--text-secondary);margin-top:4px}.current-value{font-size:12px;color:var(--accent);margin-top:4px;font-family:monospace}.divider{border:none;border-top:1px solid var(--border);margin:20px 0}.section-title{font-size:14px;font-weight:600;color:var(--text);margin-bottom:14px;padding-left:10px;border-left:3px solid var(--accent)}.toggle-group{display:flex;align-items:center;justify-content:space-between;padding:10px 0}.toggle-label{font-size:14px}.toggle-desc{font-size:12px;color:var(--text-secondary);margin-top:2px}.toggle{position:relative;width:48px;height:26px;flex-shrink:0}.toggle input{display:none}.toggle .slider{position:absolute;top:0;left:0;right:0;bottom:0;background:#3a3a5c;border-radius:26px;cursor:pointer;transition:background 0.3s}.toggle .slider::before{content:'';position:absolute;width:20px;height:20px;left:3px;bottom:3px;background:white;border-radius:50%;transition:transform 0.3s}.toggle input:checked+.slider{background:var(--accent)}.toggle input:checked+.slider::before{transform:translateX(22px)}.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 20px;border:none;border-radius:8px;font-size:14px;cursor:pointer;transition:opacity 0.2s}.btn-primary{background:var(--accent);color:white}.btn-primary:hover{background:var(--accent-hover)}.btn-danger{background:transparent;color:var(--danger);border:1px solid var(--danger)}.btn-danger:hover{background:rgba(255,107,107,0.1)}.btn-sm{padding:6px 14px;font-size:13px}.btn-group{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}.btn:disabled{opacity:0.5;cursor:not-allowed}.status-badge{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;border-radius:20px;font-size:13px;font-weight:500}.status-running{background:rgba(81,207,102,0.15);color:var(--success)}.status-stopped{background:rgba(255,107,107,0.15);color:var(--danger)}.status-loading{background:rgba(124,138,255,0.15);color:var(--accent)}.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block}.status-running .status-dot{background:var(--success)}.status-stopped .status-dot{background:var(--danger)}.status-loading .status-dot{background:var(--accent);animation:pulse 1.5s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}.toast{position:fixed;top:20px;right:20px;padding:12px 20px;border-radius:8px;font-size:14px;z-index:1000;display:none}.toast.show{display:block;animation:slideIn 0.3s ease}@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}.toast-success{background:var(--success);color:#0f0f23}.toast-error{background:var(--danger);color:white}.session-info{background:var(--input-bg);border-radius:8px;padding:16px;margin-top:14px;font-size:13px}.session-info .row{display:flex;justify-content:space-between;padding:4px 0}.session-info .label{color:var(--text-secondary)}.session-info .value{font-family:monospace}.login-area{display:none;margin-top:16px;padding:20px;background:var(--input-bg);border-radius:12px;border:1px solid var(--border)}.login-area.show{display:block}.login-area .qr-wrapper{text-align:center;margin-bottom:16px;min-height:60px}.login-area .qr-wrapper img{max-width:240px;border-radius:8px;border:1px solid var(--border);image-rendering:pixelated}.login-area .link-box{background:rgba(0,0,0,0.3);border-radius:8px;padding:10px 14px;font-size:12px;font-family:monospace;word-break:break-all;color:var(--text-secondary);margin-bottom:12px;user-select:all;max-height:40px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.login-area .status-text{text-align:center;font-size:13px;color:var(--accent);margin-bottom:12px}.login-area .btn-row{display:flex;gap:8px;justify-content:center}.loading-spinner{display:inline-block;width:16px;height:16px;border:2px solid transparent;border-top-color:currentColor;border-radius:50%;animation:spin 0.8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:999;display:none;align-items:center;justify-content:center}.modal-overlay.show{display:flex}.modal{background:var(--card-bg);border:1px solid var(--border);border-radius:12px;padding:24px;max-width:420px;width:90%}.modal h3{color:var(--accent);margin-bottom:12px;font-size:18px}.modal p{color:var(--text-secondary);font-size:14px;margin-bottom:16px;line-height:1.6}.modal .btn-group{justify-content:flex-end;margin-top:0}.radio-group{display:inline-flex;gap:16px;align-items:center;margin-top:8px}.radio-group label{display:inline-flex;align-items:center;gap:4px;cursor:pointer;font-size:14px;color:var(--text)}.radio-group input[type="radio"]{accent-color:var(--accent)}.url-input-row{display:flex;align-items:center;gap:0}.url-input-row .protocol-prefix{background:var(--input-bg);border:1px solid var(--border);border-right:none;border-radius:8px 0 0 8px;padding:10px 12px;font-size:14px;font-family:monospace;color:var(--text-secondary);white-space:nowrap}.url-input-row input{flex:1;padding:10px 14px;background:var(--input-bg);border:1px solid var(--border);border-radius:0 8px 8px 0;color:var(--text);font-size:14px}.url-input-row input:focus{outline:none;border-color:var(--accent)}</style></head><body><div class="container"><h1>🤖 微信Bot 配置</h1><p class="subtitle">管理微信 Bot 和系统监控</p>

<!-- 状态卡片 -->
<div class="card"><div class="card-title">📡 运行状态</div>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px"><span id="statusBadge" class="status-badge status-stopped"><span class="status-dot"></span><span id="statusText">加载中...</span></span></div>
<div class="session-info" id="sessionInfo" style="display:none"><div class="row"><span class="label">Bot ID</span><span class="value" id="sessBotId">-</span></div><div class="row"><span class="label">登录时间</span><span class="value" id="sessTime">-</span></div></div>
<div class="btn-group"><button class="btn btn-primary" onclick="startW()" id="btnStart">▶️ 启动</button><button class="btn btn-danger" onclick="stopW()" id="btnStop">⏹️ 停止</button><button class="btn btn-primary" onclick="restartW()" id="btnRestart">🔄 重启</button></div>

<!-- 内嵌登录区域 -->
<div class="login-area" id="loginArea">
<div class="qr-wrapper" id="qrWrapper"><span style="color:var(--text-secondary)">正在获取二维码...</span></div>
<div class="link-box" id="linkBox" title=""></div>
<div class="status-text" id="loginStatusText">等待扫码...</div>
<div class="btn-row"><button class="btn btn-primary btn-sm" onclick="copyLink()">📋 复制链接</button><button class="btn btn-danger btn-sm" onclick="abortLogin()">🛑 终止登录</button></div></div></div>

<!-- 配置卡片 -->
<div class="card"><div class="card-title">⚙️ 配置</div>

<div class="section-title">🔗 接口地址</div>
<div class="form-group"><label>微信 API 地址</label><div class="api-url" id="wechatBaseUrl">https://ilinkai.weixin.qq.com</div><div class="hint">固定地址，不可修改</div></div>
<div class="form-group"><label>公网地址</label>
<div class="url-input-row"><span class="protocol-prefix" id="protocolPrefix">http://</span><input type="text" id="baseUrl" placeholder="127.0.0.1:5200"></div>
<div class="radio-group"><label><input type="radio" name="protocol" value="http" checked onchange="onProtocolChange()"> http</label><label><input type="radio" name="protocol" value="https" onchange="onProtocolChange()"> https</label></div>
<div class="current-value" id="currentBaseUrl"></div><div class="hint">选择协议并填入地址后保存即可</div></div>

<hr class="divider">

<div class="section-title">📊 系统监控</div>
<div class="toggle-group"><div><div class="toggle-label">图片输出</div><div class="toggle-desc">以图片形式发送系统状态</div></div><label class="toggle"><input type="checkbox" id="useImage"><span class="slider"></span></label></div>
<div class="toggle-group"><div><div class="toggle-label">背景图</div><div class="toggle-desc">使用 data/background.png 作为背景</div></div><label class="toggle"><input type="checkbox" id="useBackground"><span class="slider"></span></label></div>

<hr class="divider">

<div class="section-title">🤖 自动登录</div>
<div class="toggle-group"><div><div class="toggle-label">启动时自动登录微信</div><div class="toggle-desc">插件加载后自动启动微信 Bot</div></div><label class="toggle"><input type="checkbox" id="autoLogin"><span class="slider"></span></label></div>

<div class="btn-group"><button class="btn btn-primary" onclick="saveCfg()">💾 保存配置</button><button class="btn btn-danger" onclick="resetCfg()">🔄 恢复默认</button></div></div></div>

<div class="toast" id="toast"></div>

<!-- 重启确认弹窗 -->
<div class="modal-overlay" id="restartModal"><div class="modal"><h3>⚠️ 确认重启</h3><p>重启将<span style="color:#ff6b6b">清除已登录信息</span>，需要重新扫码登录。<br><br>确定要继续吗？</p><div class="btn-group"><button class="btn btn-primary" onclick="confirmRestart()">✅ 确认重启</button><button class="btn btn-danger" onclick="closeRestartModal()">❌ 取消</button></div></div></div>

<script>
var A = {
    state: '/api/ext/wechat/state',
    config: '/api/ext/wechat/config',
    start: '/api/ext/wechat/start',
    stop: '/api/ext/wechat/stop',
    restart: '/api/ext/wechat/restart',
    login_qr: '/api/ext/wechat/login-qr',
    login_status: '/api/ext/wechat/login-status',
    abort_login: '/api/ext/wechat/abort-login'
};
var currentStatus = 'no_token';
var currentProtocol = 'http';
var loginPollingTimer = null;
var currentQrCode = '';
var lastQrGeneratedAt = 0;
var _initialLoad = true;  // 首次加载标志

function getUserStartedLogin() { return sessionStorage.getItem('wxbot_login_active') === '1'; }
function setUserStartedLogin(v) { sessionStorage.setItem('wxbot_login_active', v ? '1' : '0'); }

function T(m, t) {
    var o = document.getElementById('toast');
    o.textContent = m;
    o.className = 'toast toast-' + t + ' show';
    clearTimeout(o._timeout);
    o._timeout = setTimeout(function() { o.classList.remove('show'); }, 3000);
}

function setBtnLoading(b, t) {
    b.disabled = true;
    b.dataset.origText = b.textContent;
    b.innerHTML = '<span class="loading-spinner"></span> ' + t;
}

function resetBtn(b) {
    if (b.dataset.origText) { b.innerHTML = b.dataset.origText; delete b.dataset.origText; }
    b.disabled = false;
}

function U(d) {
    var c = d.config || {};
    document.getElementById('wechatBaseUrl').textContent = c.wechat_base_url || 'https://ilinkai.weixin.qq.com';

    var rawUrl = c.base_url || 'http://127.0.0.1:5200';
    var baseUrlInput = document.getElementById('baseUrl');

    if (_initialLoad) {
        _initialLoad = false;
        var match = rawUrl.match(/^(https?):\/\/(.+)$/);
        if (match) {
            currentProtocol = match[1];
            baseUrlInput.value = '';
            baseUrlInput.placeholder = match[2];
            document.getElementById('protocolPrefix').textContent = match[1] + '://';
            var radios = document.getElementsByName('protocol');
            for (var i = 0; i < radios.length; i++) { radios[i].checked = (radios[i].value === currentProtocol); }
        } else {
            currentProtocol = 'http';
            baseUrlInput.value = '';
            baseUrlInput.placeholder = rawUrl || '127.0.0.1:5200';
            document.getElementById('protocolPrefix').textContent = 'http://';
        }
    }

    document.getElementById('currentBaseUrl').textContent = '当前: ' + rawUrl;

    document.getElementById('useImage').checked = c.use_image !== false;
    document.getElementById('useBackground').checked = c.use_background === true;
    document.getElementById('autoLogin').checked = c.auto_login === true;

    var b = document.getElementById('statusBadge'),
        s = document.getElementById('statusText'),
        i = document.getElementById('sessionInfo'),
        st = document.getElementById('btnStart'),
        sp = document.getElementById('btnStop'),
        sr = document.getElementById('btnRestart'),
        la = document.getElementById('loginArea'),
        bs = d.bot_status || 'no_token';

    currentStatus = bs;

    if (bs === 'logging_in') {
        b.className = 'status-badge status-loading';
        s.textContent = '登录中...';
        i.style.display = 'none';
        st.disabled = true;
        sp.disabled = false;
        sr.disabled = true;

        // Web端自己触发 或 QQ端触发 → 都显示登录区
        if (d.login_source === 'qq' && !getUserStartedLogin()){
           setUserStartedLogin(true);
        }
        var shouldShow = getUserStartedLogin() || d.login_source === 'qq';

        if (shouldShow) {
            la.classList.add('show');
            if (d.qr_image && d.qr_generated_at > lastQrGeneratedAt) {
                lastQrGeneratedAt = d.qr_generated_at;
                updateQrDisplay(d.qr_image, d.qr_url, d.qr_code);
                startLoginPolling(d.qr_code);
            } else if (d.qr_code && !currentQrCode) {
                currentQrCode = d.qr_code;
                if (d.qr_image) updateQrDisplay(d.qr_image, d.qr_url, d.qr_code);
                startLoginPolling(d.qr_code);
            }
        }
    } else if (bs === 'running') {
        b.className = 'status-badge status-running';
        s.textContent = '运行中';
        if (d.session) {
            i.style.display = 'block';
            if (d.login_source !== 'qq'){
               setUserStartedLogin(false);
            }
            la.classList.remove('show');
            stopLoginPolling();
            document.getElementById('sessBotId').textContent = d.session.accountId || '-';
            document.getElementById('sessTime').textContent = d.session.savedAt || '-';
        } else { i.style.display = 'none'; }
        st.disabled = true;
        sp.disabled = false;
        sr.disabled = false;
        setUserStartedLogin(false);
        la.classList.remove('show');
        stopLoginPolling();
    } else {
        b.className = 'status-badge status-stopped';
        s.textContent = bs === 'stopped' ? '已停止' : '未运行';
        i.style.display = 'none';
        st.disabled = false;
        sp.disabled = true;
        sr.disabled = false;
        if (!getUserStartedLogin()) {
            if (d.login_source !== 'qq'){
               setUserStartedLogin(false);
            }
            la.classList.remove('show');
            stopLoginPolling();
        }
    }
}
function L() {
    fetch(A.state).then(function(r) { return r.json(); }).then(function(r) { if (r.ok) U(r.data); }).catch(function(e) { console.error(e); });
}

function onProtocolChange() {
    var protocol = document.querySelector('input[name="protocol"]:checked').value;
    currentProtocol = protocol;
    document.getElementById('protocolPrefix').textContent = protocol + '://';
}

function saveCfg() {
    var host = document.getElementById('baseUrl').value.trim();
    // 如果编辑框为空，使用 placeholder 的值
    if (!host) {
        host = document.getElementById('baseUrl').placeholder;
    }
    var protocol = document.querySelector('input[name="protocol"]:checked').value;
    var finalUrl = host ? protocol + '://' + host : '';
    var d = {
        base_url: finalUrl,
        use_image: document.getElementById('useImage').checked,
        use_background: document.getElementById('useBackground').checked,
        auto_login: document.getElementById('autoLogin').checked
    };
    fetch(A.config, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(d) })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) {
                T('✅ 配置已保存', 'success');
                // 保存后更新当前标签
                document.getElementById('currentBaseUrl').textContent = '当前: ' + finalUrl;
                // 清空编辑框，用新值更新 placeholder
                var input = document.getElementById('baseUrl');
                input.value = '';
                input.placeholder = host;
            } else { T('❌ ' + r.message, 'error'); }
        })
        .catch(function(e) { T('保存失败: ' + e.message, 'error'); });
}

function startW() {
    if (currentStatus === 'running') { T('⚠️ 已在运行中', 'error'); return; }
    if (currentStatus === 'logging_in') { T('⚠️ 正在登录中', 'error'); return; }
    var b = document.getElementById('btnStart');
    setBtnLoading(b, '启动中...');
    setUserStartedLogin(true);
    var la = document.getElementById('loginArea');
    la.classList.add('show');
    document.getElementById('qrWrapper').innerHTML = '<span style="color:var(--text-secondary)">正在获取二维码...</span>';
    document.getElementById('loginStatusText').textContent = '等待扫码...';
    fetch(A.start, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) {
                T('✅ ' + r.message, 'success');
                if (r.need_login && r.qr_image) {
                    lastQrGeneratedAt = Date.now() / 1000;
                    updateQrDisplay(r.qr_image, r.qr_url, r.qr_code);
                    startLoginPolling(r.qr_code);
                } else if (!r.need_login) {
                    setUserStartedLogin(false);
                    la.classList.remove('show');
                }
            } else { T('❌ ' + r.message, 'error'); setUserStartedLogin(false); la.classList.remove('show'); }
        })
        .catch(function(e) { T('启动失败: ' + e.message, 'error'); setUserStartedLogin(false); la.classList.remove('show'); })
        .finally(function() { resetBtn(b); setTimeout(L, 1500); });
}

function stopW() {
    if (currentStatus !== 'running' && currentStatus !== 'logging_in') { T('⚠️ 当前未在运行', 'error'); return; }
    if (!confirm('确定停止微信Bot吗？')) return;
    var b = document.getElementById('btnStop');
    setBtnLoading(b, '停止中...');
    setUserStartedLogin(false);
    fetch(A.stop, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            T(r.ok ? '✅ ' + r.message : '❌ ' + r.message, r.ok ? 'success' : 'error');
            stopLoginPolling();
            document.getElementById('loginArea').classList.remove('show');
        })
        .catch(function(e) { T('停止失败: ' + e.message, 'error'); })
        .finally(function() { resetBtn(b); setTimeout(L, 1500); });
}

function restartW() {
    if (currentStatus !== 'running' && currentStatus !== 'stopped') { T('⚠️ 当前状态无法重启', 'error'); return; }
    document.getElementById('restartModal').classList.add('show');
}

function confirmRestart() {
    document.getElementById('restartModal').classList.remove('show');
    var b = document.getElementById('btnRestart');
    setBtnLoading(b, '重启中...');
    setUserStartedLogin(true);
    var la = document.getElementById('loginArea');
    la.classList.add('show');
    document.getElementById('qrWrapper').innerHTML = '<span style="color:var(--text-secondary)">正在获取二维码...</span>';
    document.getElementById('loginStatusText').textContent = '等待扫码...';
    fetch(A.restart, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) {
            if (r.ok) {
                T('✅ ' + r.message, 'success');
                if (r.need_login && r.qr_image) {
                    lastQrGeneratedAt = Date.now() / 1000;
                    updateQrDisplay(r.qr_image, r.qr_url, r.qr_code);
                    startLoginPolling(r.qr_code);
                }
            } else { T('❌ ' + r.message, 'error'); setUserStartedLogin(false); la.classList.remove('show'); }
        })
        .catch(function(e) { T('重启失败: ' + e.message, 'error'); setUserStartedLogin(false); la.classList.remove('show'); })
        .finally(function() { resetBtn(b); setTimeout(L, 1500); });
}

function closeRestartModal() { document.getElementById('restartModal').classList.remove('show'); }

function resetCfg() {
    if (!confirm('恢复默认配置？此操作会立即保存。')) return;
    document.querySelector('input[name="protocol"][value="http"]').checked = true;
    document.getElementById('protocolPrefix').textContent = 'http://';
    document.getElementById('baseUrl').value = '';
    document.getElementById('baseUrl').placeholder = '127.0.0.1:5200';
    document.getElementById('useImage').checked = true;
    document.getElementById('useBackground').checked = false;
    document.getElementById('autoLogin').checked = false;
    saveCfg();
}

function updateQrDisplay(qrImage, qrUrl, qrCode) {
    currentQrCode = qrCode;
    var qrWrapper = document.getElementById('qrWrapper');
    if (qrImage) {
        qrWrapper.innerHTML = '<img src="' + qrImage + '" alt="二维码" style="max-width:240px;border-radius:8px" onerror="this.parentElement.innerHTML=\'<span style=color:var(--danger)>二维码加载失败，请复制链接手动打开</span>\'">';
    } else if (qrUrl) {
        qrWrapper.innerHTML = '<img src="' + qrUrl + '" alt="二维码" style="max-width:240px;border-radius:8px" onerror="this.parentElement.innerHTML=\'<span style=color:var(--danger)>二维码加载失败，请复制链接手动打开</span>\'">';
    }
    if (qrUrl) { document.getElementById('linkBox').textContent = qrUrl; document.getElementById('linkBox').title = qrUrl; }
    document.getElementById('loginStatusText').textContent = '等待扫码...';
}

function copyLink() {
    var link = document.getElementById('linkBox').textContent;
    if (!link) { T('⚠️ 暂无链接', 'error'); return; }
    if (navigator.clipboard) {
        navigator.clipboard.writeText(link).then(function() { T('✅ 链接已复制', 'success'); }).catch(function() { fallbackCopy(link); });
    } else { fallbackCopy(link); }
}

function fallbackCopy(text) {
    var ta = document.createElement('textarea'); ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); T('✅ 链接已复制', 'success'); } catch(e) { T('❌ 复制失败', 'error'); }
    document.body.removeChild(ta);
}

function abortLogin() {
    setUserStartedLogin(false);
    fetch(A.abort_login, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(r) { T(r.ok ? '🛑 已终止' : '❌ ' + r.message, r.ok ? 'success' : 'error'); })
        .catch(function(e) { T('操作失败: ' + e.message, 'error'); })
        .finally(function() { stopLoginPolling(); document.getElementById('loginArea').classList.remove('show'); setTimeout(L, 1500); });
}

function startLoginPolling(qrCode) {
    stopLoginPolling();
    if (!qrCode) return;
    loginPollingTimer = setInterval(function() {
        fetch(A.login_status + '?qrcode=' + encodeURIComponent(qrCode))
            .then(function(r) { return r.json(); })
            .then(function(r) {
                if (!r.ok) return;
                var status = r.data.status;
                if (status === 'scaned') {
                    document.getElementById('loginStatusText').textContent = '👀 已扫码，请在手机上确认...';
                } else if (status === 'confirmed') {
                    document.getElementById('loginStatusText').textContent = '✅ 登录成功！';
                    stopLoginPolling();
                    setUserStartedLogin(false);
                    setTimeout(function() {
                        document.getElementById('loginArea').classList.remove('show');
                        L();
                    }, 1000);
                } else if (status === 'expired') {
                    document.getElementById('loginStatusText').textContent = '⚠️ 二维码已过期，请重新启动';
                    stopLoginPolling();
                    setUserStartedLogin(false);
                }
            }).catch(function() {});
    }, 2000);
}
function stopLoginPolling() { if (loginPollingTimer) { clearInterval(loginPollingTimer); loginPollingTimer = null; } }

document.addEventListener('click', function(e) { if (e.target.id === 'restartModal') { e.target.classList.remove('show'); } });

// 页面加载时检查是否有活跃的登录进程
(function() {
    if (getUserStartedLogin()) {
        document.getElementById('loginArea').classList.add('show');
    }
})();

L();
setInterval(L, 3000);
</script></body></html>'''

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
    if _wechat_running and _wechat_session:
        s = f"## 🤖 微信 Bot\n---\n✅ 运行中\nBot: `{_wechat_session.get('accountId','?')}`\n登录: {_wechat_session.get('savedAt','?')}"
        buttons = [[{'text': '登出', 'data': '微信登出', 'enter': True},{'text': '重启', 'data': '微信重启', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    elif _login_in_progress:
        s = "## 🤖 微信 Bot\n---\n🔄 登录中..."
        buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    else:
        s = "## 🤖 微信 Bot\n---\n❌ 未运行\n发送 `微信登录` 启动"
        buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    await event.reply(s, msg_type=2, buttons=buttons)

@handler(r'^微信登录$', name='微信登录', desc='启动微信Bot', owner_only=True)
async def cmd_wechat_login(event, match):
    buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    if _wechat_running:
        await event.reply("## ⚠️ 已在运行中", msg_type=2, buttons=buttons)
        return
    existing = load_token()
    if existing:
        success, msg = await start_wechat()
        if success:
            await event.reply(f"## ✅ 已启动\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2, buttons=buttons)
            return
        clear_token()
    await event.reply("## 🔐 正在生成二维码...\n> 发送 `微信终止` 取消", msg_type=2, buttons=buttons)
    success, msg = await start_wechat(event)
    if success:
        await event.reply(f"## ✅ 登录成功\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2, buttons=buttons)
    else:
        # 如果是用户主动终止，不再重复提示（cmd_wechat_abort 已经发过）
        if '终止' in msg:
            return
        buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
        await event.reply(f"## ❌ {msg}", msg_type=2, buttons=buttons)


@handler(r'^微信终止$', name='微信终止', desc='终止登录', owner_only=True)
async def cmd_wechat_abort(event, match):
    global _login_abort, _login_in_progress, _login_source
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    if _wechat_running:
        await event.reply("## ⚠️ 已运行中，请用 `微信登出`", msg_type=2, buttons=buttons)
    elif not _login_in_progress:
        await event.reply("## ℹ️ 无登录进程", msg_type=2, buttons=buttons)
    else:
        _login_abort = True
        _login_in_progress = False
        _login_source = ""
        await event.reply("## 🛑 已终止", msg_type=2, buttons=buttons)

@handler(r'^微信登出$', name='微信登出', desc='停止微信Bot', owner_only=True)
async def cmd_wechat_logout(event, match):
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    success, msg = await stop_wechat()
    await event.reply(f"## {'👋 已登出' if success else '⚠️ '+msg}", msg_type=2, buttons=buttons)

@handler(r'^微信重启$', name='微信重启', desc='重启微信Bot', owner_only=True)
async def cmd_wechat_restart(event, match):
    buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True},{'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    await event.reply("## 🔄 重启中...（将清除登录信息，需重新扫码）", msg_type=2, buttons=buttons)
    if _wechat_running: await stop_wechat()
    await asyncio.sleep(2); clear_token()
    await event.reply("## 🔐 正在生成二维码...\n> 发送 `微信终止` 取消", msg_type=2, buttons=buttons)
    success, msg = await start_wechat(event)
    if success:
        await event.reply(f"## ✅ 重启成功\nBot: `{_wechat_session.get('accountId','?')}`", msg_type=2, buttons=buttons)
    else:
        await event.reply(f"## {'🛑 已终止' if '终止' in msg else '❌ '+msg}", msg_type=2, buttons=buttons)

@handler(r'^微信帮助$', name='微信帮助', desc='查看帮助', owner_only=True)
async def cmd_wechat_help(event, match):
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},{'text': '登出', 'data': '微信登出', 'enter': True},{'text': '状态', 'data': '微信状态', 'enter': True}]]
    await event.reply("## 🤖 微信 Bot 帮助\n---\n**QQ命令**\n<qqbot-cmd-input text='微信登录' /><qqbot-cmd-input text='微信终止' /><qqbot-cmd-input text='微信登出' /><qqbot-cmd-input text='微信重启' /><qqbot-cmd-input text='微信状态' /><qqbot-cmd-input text='微信帮助' /><qqbot-cmd-input text='系统状态' />\n\n**微信端**\n`系统状态` / `帮助` / `机器人列表` / `启动` / `关闭` / `dau` / `重启`\n\n**Web面板**\n侧边栏「微信Bot 配置」", msg_type=2, buttons=buttons)
    
from .app.system_status import cmd_system_status  # noqa: F401, E402