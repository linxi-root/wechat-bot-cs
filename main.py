"""
微信 iLink Bot 插件 - 主入口
"""

import asyncio
import base64
import concurrent.futures
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from io import BytesIO
from typing import Optional, Dict, Any

import aiohttp
import yaml
import qrcode
from PIL import Image

from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page, register_route
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, '微信Bot')

__plugin_meta__ = {
    'name': 'wechat-bot',
    'author': '乄杺',
    'description': '利用微信ClawBot实现管理框架机器人、查看机器人DAU数据、查看系统状态',
    'version': '1.0.4',
    'github': 'https://github.com/linxi-root/elainabot-wechat-bot',
}

# ══════════════════════════════════════════════════════════════════════════
# 路径 & 子模块导入
# ══════════════════════════════════════════════════════════════════════════

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


def _reload_ip_fallback():
    try:
        if 'ip_fallback' in sys.modules:
            mod = importlib.reload(sys.modules['ip_fallback'])
        else:
            mod = importlib.import_module('ip_fallback')
    except Exception as e:
        log.error(f"[微信Bot] 加载 ip_fallback 失败: {e}")
        raise

    if not hasattr(mod, 'set_use_fallback'):
        log.warning("[微信Bot] ip_fallback 缺少 set_use_fallback，"
                    "请确认 ip_fallback.py 已更新到最新版；本次使用兼容实现")

        def _compat_set_use_fallback(v): pass
        def _compat_get_use_fallback(): return False
        def _compat_build_candidates(host, fallback_ips): return [None]

        mod.set_use_fallback = _compat_set_use_fallback
        mod.get_use_fallback = _compat_get_use_fallback
        mod.build_candidates = _compat_build_candidates
    return mod


ip_fallback = _reload_ip_fallback()

DATA_DIR = os.path.join(PLUGIN_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.yaml')
WECHAT_TOKEN_FILE = os.path.join(DATA_DIR, '.weixin-token.json')
WECHAT_BASE_URL = 'https://ilinkai.weixin.qq.com'

DEFAULT_CONFIG = {
    'use_image': True,
    'use_background': False,
    'auto_login': False,
    'use_ip_fallback': False,
}
TOKEN_EXPIRE_DAYS = 7

WeixinApiClient = None
WeixinMessageSender = None
WechatCommand = None
SessionExpiredError = RuntimeError
_SUBMODULES_OK = False
_SUBMODULE_ERR = ""


def _purge_plugin_submodules():
    prefixes = ('app.weixin_api', 'app.commands', 'app.system_status')
    for name in list(sys.modules.keys()):
        if name in prefixes or any(name.endswith('.' + p) for p in prefixes):
            try:
                del sys.modules[name]
            except Exception:
                pass


def _load_submodules() -> bool:
    global WeixinApiClient, WeixinMessageSender, WechatCommand
    global SessionExpiredError, _SUBMODULES_OK, _SUBMODULE_ERR

    _purge_plugin_submodules()
    errors = []
    try:
        from .app.weixin_api import (
            WeixinApiClient as _C, WeixinMessageSender as _S,
            SessionExpiredError as _E)
        from .app.commands import WechatCommand as _Cmd
        WeixinApiClient, WeixinMessageSender = _C, _S
        SessionExpiredError, WechatCommand = _E, _Cmd
        _SUBMODULES_OK = True
        return True
    except Exception as e:
        errors.append(f"相对: {e}")
    try:
        for name, mod in list(sys.modules.items()):
            if getattr(mod, '__file__', None) == __file__ and '.' in name:
                pkg = name.rsplit('.', 1)[0]
                m_api = importlib.import_module(f"{pkg}.app.weixin_api")
                m_cmd = importlib.import_module(f"{pkg}.app.commands")
                WeixinApiClient = m_api.WeixinApiClient
                WeixinMessageSender = m_api.WeixinMessageSender
                SessionExpiredError = getattr(m_api, 'SessionExpiredError', RuntimeError)
                WechatCommand = m_cmd.WechatCommand
                _SUBMODULES_OK = True
                return True
    except Exception as e:
        errors.append(f"包名: {e}")
    try:
        from app.weixin_api import (
            WeixinApiClient as _C, WeixinMessageSender as _S,
            SessionExpiredError as _E)
        from app.commands import WechatCommand as _Cmd
        WeixinApiClient, WeixinMessageSender = _C, _S
        SessionExpiredError, WechatCommand = _E, _Cmd
        _SUBMODULES_OK = True
        return True
    except Exception as e:
        errors.append(f"sys.path: {e}")
    _SUBMODULE_ERR = " | ".join(errors)
    log.error(f"[微信Bot] 子模块加载失败: {_SUBMODULE_ERR}")
    return False


# ══════════════════════════════════════════════════════════════════════════
# 插件独立线程 + 独立事件循环
# ══════════════════════════════════════════════════════════════════════════

_plugin_loop: Optional[asyncio.AbstractEventLoop] = None
_plugin_thread: Optional[threading.Thread] = None
_plugin_thread_lock = threading.Lock()
_plugin_loop_ready = threading.Event()


def _plugin_thread_runner():
    global _plugin_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _plugin_loop = loop
    _plugin_loop_ready.set()
    try:
        loop.run_forever()
    except Exception as e:
        log.error(f"[微信Bot] 插件线程异常: {e}")
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        finally:
            loop.close()
            _plugin_loop = None
            log.info("[微信Bot] 插件线程已退出")


def _ensure_plugin_thread() -> bool:
    global _plugin_thread
    with _plugin_thread_lock:
        if _plugin_thread is not None and _plugin_thread.is_alive():
            return _plugin_loop is not None
        _plugin_loop_ready.clear()
        _plugin_thread = threading.Thread(
            target=_plugin_thread_runner,
            name="wechat-bot-worker",
            daemon=True,
        )
        _plugin_thread.start()
        if not _plugin_loop_ready.wait(timeout=10):
            log.error("[微信Bot] 插件线程启动超时")
            return False
        return _plugin_loop is not None


def _submit(coro) -> concurrent.futures.Future:
    if _plugin_loop is None:
        raise RuntimeError("插件线程未启动")
    return asyncio.run_coroutine_threadsafe(coro, _plugin_loop)


async def _await_plugin(coro, timeout: float = 60):
    future = _submit(coro)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
    except asyncio.TimeoutError:
        future.cancel()
        raise


# ══════════════════════════════════════════════════════════════════════════
# 全局状态
# ══════════════════════════════════════════════════════════════════════════

_config: dict = {}
_wechat_task: Optional[asyncio.Task] = None
_wechat_running = False
_wechat_session: Optional[Dict] = None
_wechat_initialized = False

_login_lock: Optional[asyncio.Lock] = None
_login_in_progress = False
_login_abort = False
_login_source = ""
_login_future: Optional[asyncio.Task] = None
_current_login_holder: Optional[Dict[str, Any]] = None
_qr_ready_event: Optional[asyncio.Event] = None
_login_done_event: Optional[asyncio.Event] = None

_current_qr_code = ""
_current_qr_link = ""
_current_qr_image_base64 = ""
_qr_generated_at: float = 0


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
        if not line or line.startswith('#'):
            continue
        pkg = line.split('>=')[0].split('==')[0].split('~=')[0].split('!=')[0].strip()
        if pkg:
            required.append(pkg)
    if not required:
        return
    try:
        result = subprocess.check_output(
            [sys.executable, '-m', 'pip', 'list', '--format=columns'],
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        log.error("[微信Bot] pip list 失败")
        return
    installed = set()
    for line in result.split('\n')[2:]:
        parts = line.split()
        if parts:
            installed.add(parts[0])
    missing = [p for p in required
               if p not in installed
               and p.lower() not in {i.lower() for i in installed}]
    if not missing:
        return
    log.info(f"[微信Bot] 安装: {missing}")
    try:
        subprocess.check_call(
            [sys.executable, '-m', 'pip', 'install', '-q'] + missing,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("[微信Bot] 安装完成")
    except subprocess.CalledProcessError:
        log.error(f"[微信Bot] 安装失败: pip install {' '.join(missing)}")


# ══════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    global _config
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(DEFAULT_CONFIG, f, allow_unicode=True, default_flow_style=False)
        _config = dict(DEFAULT_CONFIG)
        return _config
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    cfg = {k: raw.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    if set(raw.keys()) != set(cfg.keys()) or any(raw.get(k) != cfg[k] for k in cfg):
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        except Exception as e:
            log.warning(f"[微信Bot] 配置文件规范化失败: {e}")
    _config = cfg
    return _config


def _read_config_direct() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return dict(DEFAULT_CONFIG)
    return {k: raw.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}


def update_config(updates: dict) -> bool:
    global _config
    cfg = _read_config_direct()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULT_CONFIG})
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
    _config.update(cfg)
    return True


# ══════════════════════════════════════════════════════════════════════════
# 图床 & 通用发图
# ══════════════════════════════════════════════════════════════════════════

def _get_hosting():
    try:
        from core.application import get_app
        app = get_app()
        if not app:
            return None
        mm = getattr(app, 'module_manager', None)
        if not mm:
            return None
        return mm.get('image_hosting')
    except Exception as e:
        log.warning(f"[图床] 获取模块失败: {e}")
        return None


async def _upload_to_hosting(image_bytes: bytes,
                             filename: str = "image.png",
                             event=None) -> Optional[str]:
    hosting = _get_hosting()
    if not hosting:
        log.warning("[图床] 模块未启用")
        return None
    token_manager = None
    sender = None
    if event is not None:
        try:
            bot = getattr(event, 'bot', None)
            if bot is not None:
                token_manager = getattr(bot, 'token_manager', None)
                sender = getattr(bot, 'sender', None)
        except Exception:
            pass
    try:
        url = await hosting.upload_any(
            image_bytes, filename,
            token_manager=token_manager, sender=sender)
    except Exception as e:
        log.error(f"[图床] upload_any 异常: {e}")
        return None
    if isinstance(url, str) and url.startswith('http'):
        log.info(f"[图床] 上传成功: {url}")
        return url
    log.warning(f"[图床] 上传失败或返回异常: {url!r}")
    return None


async def _reply_qq_image(event, image_bytes: bytes, caption: str = "",
                          filename: str = "image.png") -> bool:
    """QQ 端发图：优先图床 markdown，失败回退 reply_image"""
    image_url = await _upload_to_hosting(image_bytes, filename, event=event)
    if image_url:
        md = f"![img]({image_url})"
        if caption:
            md = f"{caption}\n\n{md}"
        try:
            await event.reply(md)
            return True
        except Exception as e:
            log.warning(f"[图床] markdown 发送失败: {e}")
    try:
        await event.reply_image(image_bytes, caption or filename)
        return True
    except Exception as e:
        log.error(f"[QQ发图] reply_image 失败: {e}")
        if caption:
            try:
                await event.reply(caption)
            except Exception:
                pass
        return False


# ══════════════════════════════════════════════════════════════════════════
# Token
# ══════════════════════════════════════════════════════════════════════════

def load_token() -> Optional[Dict]:
    if not os.path.exists(WECHAT_TOKEN_FILE):
        return None
    try:
        with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        saved_at = data.get('savedAt', '')
        if saved_at:
            try:
                if (datetime.now() - datetime.fromisoformat(saved_at)).days > TOKEN_EXPIRE_DAYS:
                    os.remove(WECHAT_TOKEN_FILE)
                    return None
            except Exception:
                pass
        return data if data.get('token') and data.get('baseUrl') else None
    except Exception:
        return None


def save_token(data: Dict):
    data['savedAt'] = datetime.now().isoformat()
    with open(WECHAT_TOKEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(WECHAT_TOKEN_FILE, 0o600)


def clear_token():
    if os.path.exists(WECHAT_TOKEN_FILE):
        os.remove(WECHAT_TOKEN_FILE)


def _save_context_token(user_id: str, context_token: str):
    try:
        if os.path.exists(WECHAT_TOKEN_FILE):
            with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {}
        data['context_token'] = context_token
        data['last_user_id'] = user_id
        data['last_update'] = datetime.now().isoformat()
        with open(WECHAT_TOKEN_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"[微信Bot] 保存 context_token 失败: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 二维码
# ══════════════════════════════════════════════════════════════════════════

def generate_qr_image(url: str, size: int = 360) -> bytes:
    qr = qrcode.QRCode(version=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    if hasattr(img, 'get_image'):
        img = img.get_image()
    img = img.convert('RGB')
    img = img.resize((size, size), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def generate_qr_base64(url: str, size: int = 280) -> str:
    img_bytes = generate_qr_image(url, size)
    return "data:image/png;base64," + base64.b64encode(img_bytes).decode()


# ══════════════════════════════════════════════════════════════════════════
# 微信 API 请求（QR 阶段复用 session）
# ══════════════════════════════════════════════════════════════════════════

_qr_session: Optional[aiohttp.ClientSession] = None
_qr_session_node: Optional[str] = None


async def _get_qr_session(candidate: Optional[str]) -> aiohttp.ClientSession:
    global _qr_session, _qr_session_node
    if (_qr_session is not None
            and not _qr_session.closed
            and _qr_session_node == candidate):
        return _qr_session
    if _qr_session is not None and not _qr_session.closed:
        try:
            await _qr_session.close()
        except Exception:
            pass
    _qr_session = aiohttp.ClientSession(
        connector=ip_fallback.make_connector(candidate),
        timeout=aiohttp.ClientTimeout(total=20, connect=10),
    )
    _qr_session_node = candidate
    return _qr_session


async def _close_qr_session():
    global _qr_session, _qr_session_node
    if _qr_session is not None and not _qr_session.closed:
        try:
            await _qr_session.close()
        except Exception:
            pass
    _qr_session = None
    _qr_session_node = None


async def _fetch_qr_code() -> tuple:
    url = f"{WECHAT_BASE_URL}/ilink/bot/get_bot_qrcode?bot_type=3"
    req_timeout = aiohttp.ClientTimeout(total=15, connect=10)
    errors = []
    candidates = ip_fallback.build_candidates(
        ip_fallback.WECHAT_API_HOST,
        ip_fallback.WECHAT_API_FALLBACK_IPS)
    for candidate in candidates:
        label = candidate or "DNS"
        try:
            session = await _get_qr_session(candidate)
            async with session.get(url, timeout=req_timeout) as resp:
                text = await resp.text()
                if resp.status != 200:
                    errors.append(f"{label}: HTTP {resp.status}")
                    continue
                try:
                    qr_data = json.loads(text)
                except json.JSONDecodeError as e:
                    errors.append(f"{label}: 非 JSON: {e}")
                    continue
                qrcode_str = qr_data.get("qrcode", "")
                if not qrcode_str:
                    errors.append(f"{label}: 无 qrcode 字段")
                    continue
                qr_link = (qr_data.get("qrcode_img_content")
                           or f"https://liteapp.weixin.qq.com/q/7GiQu1"
                              f"?qrcode={qrcode_str}&bot_type=3")
                ip_fallback.set_working_node(
                    ip_fallback.WECHAT_API_HOST, candidate)
                log.info(f"[微信Bot] ✅ 二维码获取成功（{label}）")
                return qrcode_str, qr_link
        except asyncio.TimeoutError:
            errors.append(f"{label}: 超时")
            continue
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            continue
    raise Exception("获取二维码失败: " + " | ".join(errors))


async def _query_qr_status(qrcode_str: str) -> dict:
    """轮询二维码状态。

    注意：微信 iLink 的 get_qrcode_status 是长轮询接口，用户扫码前
    服务端可能不返回响应。单次 5s 超时是正常现象，不应视为错误。
    所有候选节点都超时时返回 {"status": "wait", "_timeout": True}。
    """
    url = f"{WECHAT_BASE_URL}/ilink/bot/get_qrcode_status"
    params = {"qrcode": qrcode_str}
    req_timeout = aiohttp.ClientTimeout(total=5, connect=3)
    errors = []
    timed_out = 0
    candidates = ip_fallback.build_candidates(
        ip_fallback.WECHAT_API_HOST,
        ip_fallback.WECHAT_API_FALLBACK_IPS)
    for candidate in candidates:
        label = candidate or "DNS"
        try:
            session = await _get_qr_session(candidate)
            async with session.get(url, params=params,
                                   timeout=req_timeout) as resp:
                text = await resp.text()
                if resp.status != 200:
                    errors.append(f"{label}: HTTP {resp.status}")
                    continue
                data = json.loads(text)
                ip_fallback.set_working_node(
                    ip_fallback.WECHAT_API_HOST, candidate)
                return data
        except asyncio.TimeoutError:
            timed_out += 1
            errors.append(f"{label}: 超时")
            continue
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            continue

    # 所有候选都超时 → 长轮询挂起（用户未扫码），正常返回 wait
    if timed_out == len(candidates) and timed_out > 0:
        return {"status": "wait", "_timeout": True}

    raise Exception("二维码状态查询失败: " + " | ".join(errors))

# ══════════════════════════════════════════════════════════════════════════
# 统一登录流程
# ══════════════════════════════════════════════════════════════════════════

async def _do_login_background(qr_ev: asyncio.Event,
                               done_ev: asyncio.Event,
                               holder: dict):
    global _login_in_progress, _login_source
    global _current_qr_code, _current_qr_link, _current_qr_image_base64, _qr_generated_at
    global _wechat_session, _wechat_task, _wechat_running
    try:
        if _login_abort:
            holder.update({"ok": False, "msg": "登录已被用户终止", "user_friendly": True})
            qr_ev.set(); done_ev.set()
            return

        try:
            qrcode_str, qr_link = await _fetch_qr_code()
        except Exception as e:
            log.error(f"[微信Bot] 获取二维码失败: {e}")
            holder.update({"ok": False, "msg": f"获取二维码失败: {e}", "user_friendly": True})
            qr_ev.set(); done_ev.set()
            return

        _current_qr_code = qrcode_str
        _current_qr_link = qr_link
        _current_qr_image_base64 = generate_qr_base64(qr_link)
        _qr_generated_at = time.time()
        log.info(f"[微信Bot] 📱 {qr_link}")
        qr_ev.set()

        deadline = time.time() + 5 * 60
        refresh_count = 0
        scanned_logged = False
        current_qr = qrcode_str

        while time.time() < deadline:
            if _login_abort:
                holder.update({"ok": False, "msg": "登录已被用户终止", "user_friendly": True})
                done_ev.set()
                return
            try:
                status_data = await _query_qr_status(current_qr)
            except Exception as e:
                if _login_abort:
                    holder.update({"ok": False, "msg": "登录已被用户终止", "user_friendly": True})
                    done_ev.set()
                    return
                log.warning(f"[微信Bot] 查询二维码状态失败（将重试）: {e}")
                await asyncio.sleep(1)
                continue

            status = status_data.get("status", "wait")

            if status == "scaned":
                if not scanned_logged:
                    log.info("[微信Bot] 👀 已扫码，等待确认")
                    scanned_logged = True
                await asyncio.sleep(1)
                continue

            if status == "expired":
                refresh_count += 1
                if refresh_count > 1:
                    holder.update({"ok": False, "msg": "二维码多次过期", "user_friendly": True})
                    done_ev.set()
                    return
                try:
                    new_qr, new_link = await _fetch_qr_code()
                    current_qr = new_qr
                    _current_qr_code = new_qr
                    _current_qr_link = new_link
                    _current_qr_image_base64 = generate_qr_base64(new_link)
                    _qr_generated_at = time.time()
                    scanned_logged = False
                    log.info(f"[微信Bot] 🔄 二维码已刷新: {new_link}")
                    qr_ev.set()
                    # 让 QQ 侧的 cmd_wechat_login / cmd_wechat_restart
                    # 通过 _wait_qr_event_async 感知到 _current_qr_link 变化
                except Exception as e:
                    log.error(f"[微信Bot] 刷新二维码失败: {e}")
                await asyncio.sleep(2)
                continue

            if status == "confirmed":
                log.info("[微信Bot] ✅ 登录成功")
                token_data = {
                    "token": status_data["bot_token"],
                    "baseUrl": status_data.get("baseurl", WECHAT_BASE_URL),
                    "accountId": status_data.get("ilink_bot_id", ""),
                    "userId": status_data.get("ilink_user_id", ""),
                    "savedAt": datetime.now().isoformat(),
                }
                save_token(token_data)
                _wechat_session = token_data
                _wechat_running = True
                _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
                holder.update({"ok": True, "msg": "登录成功", "token": token_data})
                done_ev.set()
                return

            await asyncio.sleep(3)

        holder.update({"ok": False, "msg": "登录超时", "user_friendly": True})
        done_ev.set()
    except Exception as e:
        log.exception(f"[微信Bot] 登录后台任务异常，内部错误{e}")
        holder.update({"ok": False, "msg": f"内部错误: {e}", "user_friendly": False})
        try:
            qr_ev.set(); done_ev.set()
        except Exception:
            pass
    finally:
        _login_in_progress = False
        _login_source = ""
        try:
            await asyncio.shield(_close_qr_session())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


async def _start_login_async(source: str):
    global _login_lock, _login_in_progress, _login_abort, _login_source
    global _login_future, _current_login_holder
    global _qr_ready_event, _login_done_event
    global _current_qr_code, _current_qr_link, _current_qr_image_base64, _qr_generated_at

    if _login_lock is None:
        _login_lock = asyncio.Lock()

    async with _login_lock:
        if _wechat_running:
            return False, "已在运行中"
        if _login_in_progress:
            return False, "登录已在进行中"

        _login_in_progress = True
        _login_abort = False
        _login_source = source
        holder: Dict[str, Any] = {"ok": None, "msg": "", "token": None}
        qr_ev = asyncio.Event()
        done_ev = asyncio.Event()
        _current_login_holder = holder
        _qr_ready_event = qr_ev
        _login_done_event = done_ev
        _current_qr_code = ""
        _current_qr_link = ""
        _current_qr_image_base64 = ""
        _qr_generated_at = 0

        _login_future = asyncio.create_task(
            _do_login_background(qr_ev, done_ev, holder))
        return True, "登录流程已启动"


async def _wait_qr_ready(timeout: float = 30) -> bool:
    ev = _qr_ready_event
    if ev is None:
        return False
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _wait_login_done(timeout: float = 360) -> Dict:
    ev = _login_done_event
    holder = _current_login_holder
    if ev is None or holder is None:
        return {"ok": False, "msg": "无登录进程"}
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "msg": "等待登录结果超时"}
    return dict(holder)


async def _wait_qr_event_async(after_link: str, timeout: float = 350) -> Dict:
    """★ v1.0.4 新增：在插件线程等待二维码刷新或登录完成。

    - after_link：调用方已经发送给用户的最后一张二维码链接
    - 返回值：
        {"type": "done", "holder": {...}}
        {"type": "refreshed", "qr_link": str, "qr_image": str}
        {"type": "timeout"}
    """
    deadline = time.time() + timeout

    def _check():
        if _login_done_event is not None and _login_done_event.is_set():
            holder = (dict(_current_login_holder)
                      if _current_login_holder is not None
                      else {"ok": False, "msg": "无登录进程"})
            return {"type": "done", "holder": holder}
        if _current_qr_link and _current_qr_link != after_link:
            return {
                "type": "refreshed",
                "qr_link": _current_qr_link,
                "qr_image": _current_qr_image_base64,
            }
        return None

    # 立即检查一次，避免错过已经发生的事件
    r = _check()
    if r is not None:
        return r

    while time.time() < deadline:
        await asyncio.sleep(0.5)
        r = _check()
        if r is not None:
            return r
    return {"type": "timeout"}


async def _is_login_in_progress_async() -> bool:
    return _login_in_progress


async def _get_qr_info() -> Dict:
    return {
        "qr_code": _current_qr_code,
        "qr_url": _current_qr_link,
        "qr_image": _current_qr_image_base64,
        "generated_at": _qr_generated_at,
    }


async def _do_stop_wechat():
    global _wechat_running, _wechat_task, _wechat_session
    global _login_abort, _login_in_progress, _login_future
    global _qr_ready_event, _login_done_event, _current_login_holder

    if not _wechat_running and not _login_in_progress:
        try:
            await _close_qr_session()
        except Exception:
            pass
        return False, "未在运行"

    _login_abort = True
    _login_in_progress = False
    _wechat_running = False

    if _login_future and not _login_future.done():
        _login_future.cancel()
        try:
            await _login_future
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    if _wechat_task:
        _wechat_task.cancel()
        try:
            await _wechat_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    _wechat_session = None
    _login_abort = False
    _login_future = None

    if _qr_ready_event is not None:
        try: _qr_ready_event.set()
        except Exception: pass
    if _login_done_event is not None:
        try: _login_done_event.set()
        except Exception: pass
    if _current_login_holder is not None:
        _current_login_holder.update({"ok": False, "msg": "已终止", "user_friendly": True})

    try:
        await _close_qr_session()
    except Exception:
        pass

    return True, "已停止"


# ══════════════════════════════════════════════════════════════════════════
# 消息循环
# ══════════════════════════════════════════════════════════════════════════

async def wechat_main_loop(session_data: Dict):
    global _wechat_running, _wechat_session

    if not _SUBMODULES_OK:
        log.error(f"[微信Bot] 子模块未加载: {_SUBMODULE_ERR}")
        _wechat_running = False
        return

    client = WeixinApiClient(session_data["baseUrl"], session_data["token"])
    sender = WeixinMessageSender(client)
    buf = ""
    log.info("[微信Bot] 消息循环启动")
    while _wechat_running:
        try:
            resp = await client.get_updates(buf, timeout_ms=38_000)
            if resp.get_updates_buf:
                buf = resp.get_updates_buf
            if not resp.msgs:
                continue
            for msg in resp.msgs:
                if msg.message_type != 1 or not msg.from_user_id:
                    continue
                text = ""
                context_token = msg.context_token or ""
                if msg.item_list:
                    for item in msg.item_list:
                        if item.type == 1 and item.text_item and item.text_item.text:
                            text = item.text_item.text
                            break
                if not text:
                    continue
                if context_token:
                    _save_context_token(msg.from_user_id, context_token)
                #log.info(f"[微信消息] 收到: {text[:50]}")
                handler_, args = WechatCommand.match(text)
                if handler_:
                    await handler_(sender, msg.from_user_id, context_token, args)
                else:
                    await sender.send_text(
                        msg.from_user_id,
                        f"收到消息: {text}\n发送「帮助」查看可用指令",
                        context_token)
        except asyncio.CancelledError:
            break
        except SessionExpiredError as e:
            log.error(f"[微信Bot] Session 过期: {e}")
            _wechat_running = False
            _wechat_session = None
            try: clear_token()
            except Exception: pass
            break
        except Exception as e:
            if "session timeout" in str(e).lower() or "-14" in str(e):
                log.error(f"[微信Bot] Session 过期: {e}")
                _wechat_running = False
                _wechat_session = None
                try: clear_token()
                except Exception: pass
                break
            log.error(f"[微信Bot] 轮询出错: {e}")
            await asyncio.sleep(3)
    await sender.close()
    log.info("[微信Bot] 消息循环停止")


# ══════════════════════════════════════════════════════════════════════════
# 高层业务 API
# ══════════════════════════════════════════════════════════════════════════

async def _api_get_state_async() -> dict:
    cfg = _read_config_direct()
    has_token = os.path.exists(WECHAT_TOKEN_FILE)
    session_info = None
    if has_token:
        try:
            with open(WECHAT_TOKEN_FILE, 'r') as f:
                d = json.load(f)
            session_info = {'accountId': d.get('accountId', ''),
                            'savedAt': d.get('savedAt', '')}
        except Exception:
            pass
    if _wechat_running:
        bot_status = 'running'
    elif _login_in_progress:
        bot_status = 'logging_in'
    elif has_token:
        bot_status = 'stopped'
    else:
        bot_status = 'no_token'
    # ★ 新增：最近一次登录失败原因
    last_error = ""
    last_error_friendly = True
    holder = _current_login_holder
    if (holder is not None
            and holder.get("ok") is False
            and not _login_in_progress):
        last_error = holder.get("msg", "")
        last_error_friendly = holder.get("user_friendly", True)
    return {
        'config': cfg,
        'wechat_base_url': WECHAT_BASE_URL,
        'bot_status': bot_status,
        'running': _wechat_running,
        'initialized': _wechat_initialized,
        'login_in_progress': _login_in_progress,
        'session': session_info,
        'qr_code': _current_qr_code,
        'qr_url': _current_qr_link,
        'qr_image': _current_qr_image_base64,
        'qr_generated_at': _qr_generated_at,
        'login_source': _login_source,
        'last_error': last_error,                    # ★ 新增
        'last_error_friendly': last_error_friendly,  # ★ 新增
    }

async def _api_start_async():
    global _wechat_session, _wechat_task, _wechat_running
    if _wechat_running:
        return {'ok': False, 'message': '已在运行中', 'need_login': False,
                'reason': 'running'}
    existing = load_token()
    if existing:
        _wechat_session = existing
        _wechat_running = True
        _wechat_task = asyncio.create_task(wechat_main_loop(_wechat_session))
        return {'ok': True, 'message': '已启动（复用 token）',
                'need_login': False}
    started, msg = await _start_login_async("web")
    if not started and "已在进行中" in msg:
        return {'ok': True, 'message': msg, 'need_login': True,
                'reason': 'already_logging_in'}
    return {'ok': started, 'message': msg, 'need_login': True,
            'reason': 'started' if started else 'error'}


async def _api_stop_async():
    ok, msg = await _do_stop_wechat()
    return {'ok': ok, 'message': msg}


async def _api_restart_async():
    await _do_stop_wechat()
    await asyncio.sleep(1)
    clear_token()
    started, msg = await _start_login_async("web")
    return {'ok': started, 'message': msg, 'cleared': True,
            'need_login': True}


async def _api_abort_login_async():
    global _login_abort, _login_in_progress, _login_source
    global _qr_ready_event, _login_done_event, _current_login_holder
    _login_abort = True
    _login_in_progress = False
    _login_source = ""
    if _qr_ready_event is not None:
        try: _qr_ready_event.set()
        except Exception: pass
    if _login_done_event is not None:
        try: _login_done_event.set()
        except Exception: pass
    if _current_login_holder is not None:
        _current_login_holder.update({"ok": False, "msg": "已终止", "user_friendly": True})
    return {'ok': True, 'message': '已终止'}


# ══════════════════════════════════════════════════════════════════════════
# Web API
# ══════════════════════════════════════════════════════════════════════════

@register_route('GET', '/api/ext/wechat/login-qr', auth=False)
async def api_get_login_qr(request):
    from aiohttp import web
    info = await _await_plugin(_get_qr_info(), timeout=5)
    return web.json_response({'ok': True, 'data': info})


@register_route('GET', '/api/ext/wechat/login-status', auth=False)
async def api_get_login_status(request):
    from aiohttp import web
    qrcode_str = request.query.get('qrcode', '')
    if not qrcode_str:
        return web.json_response({'ok': False, 'message': '缺少qrcode参数'}, status=400)
    try:
        status_data = await _await_plugin(_query_qr_status(qrcode_str), timeout=10)
        return web.json_response(
            {'ok': True, 'data': {'status': status_data.get('status', 'wait')}})
    except Exception as e:
        return web.json_response({'ok': False, 'message': str(e)}, status=500)


@register_route('POST', '/api/ext/wechat/abort-login', auth=False)
async def api_abort_login(request):
    from aiohttp import web
    result = await _await_plugin(_api_abort_login_async(), timeout=10)
    return web.json_response(result)


@register_route('GET', '/api/ext/wechat/state', auth=False)
async def api_get_state(request):
    from aiohttp import web
    data = await _await_plugin(_api_get_state_async(), timeout=10)
    return web.json_response({'ok': True, 'data': data})


@register_route('POST', '/api/ext/wechat/config', auth=False)
async def api_save_config(request):
    from aiohttp import web
    try:
        body = await request.json()
        keys = ['use_image', 'use_background', 'auto_login', 'use_ip_fallback']
        updates = {k: bool(body[k]) for k in keys if k in body}
        if not updates:
            return web.json_response({'ok': False, 'message': '无有效配置'}, status=400)
        update_config(updates)
        if 'use_ip_fallback' in updates:
            ip_fallback.set_use_fallback(updates['use_ip_fallback'])
            log.info(f"[微信Bot] IP fallback: "
                     f"{'开' if updates['use_ip_fallback'] else '关'}")
        return web.json_response({'ok': True, 'message': '已保存', 'data': updates})
    except Exception as e:
        return web.json_response({'ok': False, 'message': str(e)}, status=400)


@register_route('POST', '/api/ext/wechat/restart', auth=False)
async def api_restart(request):
    from aiohttp import web
    result = await _await_plugin(_api_restart_async(), timeout=20)
    return web.json_response(result)


@register_route('POST', '/api/ext/wechat/stop', auth=False)
async def api_stop(request):
    from aiohttp import web
    result = await _await_plugin(_api_stop_async(), timeout=15)
    return web.json_response(result)


@register_route('POST', '/api/ext/wechat/start', auth=False)
async def api_start(request):
    from aiohttp import web
    result = await _await_plugin(_api_start_async(), timeout=15)
    if result.get('need_login'):
        try:
            info = await _await_plugin(_get_qr_info(), timeout=3)
            result['qr_code'] = info.get('qr_code', '')
            result['qr_url'] = info.get('qr_url', '')
            result['qr_image'] = info.get('qr_image', '')
        except Exception:
            pass
    return web.json_response(result)


@register_route('POST', '/api/ext/wechat/send-image', auth=False)
async def api_send_image(request):
    from aiohttp import web

    if not _wechat_running or not _wechat_session:
        return web.json_response({'ok': False, 'message': '微信 Bot 未运行'},
                                 status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'ok': False, 'message': '请求体必须为 JSON'},
                                 status=400)

    image_b64 = (body.get('image') or '').strip()
    if not image_b64:
        return web.json_response({'ok': False, 'message': '缺少 image 字段'},
                                 status=400)
    if image_b64.startswith('data:'):
        try:
            image_b64 = image_b64.split(',', 1)[1]
        except Exception:
            return web.json_response({'ok': False, 'message': 'data URI 格式错误'},
                                     status=400)
    try:
        image_data = base64.b64decode(image_b64)
    except Exception as e:
        return web.json_response({'ok': False, 'message': f'base64 解码失败: {e}'},
                                 status=400)
    if not image_data:
        return web.json_response({'ok': False, 'message': '图片数据为空'},
                                 status=400)
    if len(image_data) > 10 * 1024 * 1024:
        return web.json_response({'ok': False, 'message': '图片过大（>10MB）'},
                                 status=400)

    to_user_id = body.get('to_user_id') or ''
    context_token = body.get('context_token') or ''
    if not to_user_id or not context_token:
        try:
            with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f:
                td = json.load(f)
            to_user_id = to_user_id or td.get('last_user_id') or td.get('userId') or ''
            context_token = context_token or td.get('context_token') or ''
        except Exception:
            pass
    if not to_user_id:
        return web.json_response(
            {'ok': False, 'message': '无目标用户（请先在微信端发一条消息）'},
            status=400)

    client = WeixinApiClient(_wechat_session["baseUrl"], _wechat_session["token"])
    sender = WeixinMessageSender(client)
    try:
        result = await sender.send_image(to_user_id, image_data, context_token)
    finally:
        await sender.close()

    if result.get('ok'):
        return web.json_response({'ok': True, 'message': '已发送', 'data': result})
    return web.json_response(
        {'ok': False, 'message': result.get('error', '发送失败')}, status=500)


# ══════════════════════════════════════════════════════════════════════════
# Web 页面（含 IP Fallback 开关）
# ══════════════════════════════════════════════════════════════════════════

CONFIG_PAGE_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>微信 Bot 配置</title>
<style>
:root{
  --bg:#f6f7fb;--bg-glow-1:rgba(124,93,255,.12);--bg-glow-2:rgba(91,140,255,.10);
  --panel:#ffffff;--panel-inner:#f8f9fc;--border:#e6e8ef;--border-2:#d8dbe5;
  --border-focus:rgba(124,93,255,.45);--text:#171a2e;--text-dim:#5b6076;
  --text-muted:#9599ad;--accent-1:#7c5dff;--accent-2:#5b8cff;
  --accent-soft:rgba(124,93,255,.08);--success:#10b981;--success-soft:rgba(16,185,129,.1);
  --danger:#ef4444;--danger-soft:rgba(239,68,68,.08);--warn:#f59e0b;
  --radius-lg:16px;--radius:12px;--radius-sm:10px;--radius-xs:8px;
  --shadow-sm:0 1px 2px rgba(23,26,46,.04),0 1px 3px rgba(23,26,46,.03);
  --shadow:0 2px 8px rgba(23,26,46,.05),0 4px 16px rgba(23,26,46,.04);
  --shadow-lg:0 8px 24px rgba(23,26,46,.08),0 16px 48px rgba(23,26,46,.06);
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',system-ui,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.6;padding:40px 20px 80px;min-height:100vh;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;position:relative;overflow-x:hidden;}
body::before,body::after{content:'';position:fixed;border-radius:50%;filter:blur(120px);pointer-events:none;z-index:0;}
body::before{width:560px;height:560px;background:var(--bg-glow-1);top:-200px;left:-120px;}
body::after{width:480px;height:480px;background:var(--bg-glow-2);top:30%;right:-140px;}
.container{max-width:860px;margin:0 auto;position:relative;z-index:1}
.hero{display:flex;align-items:center;gap:16px;margin-bottom:28px;padding:0 4px}
.hero-icon{width:56px;height:56px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:28px;
  background:linear-gradient(135deg,#7c5dff,#5b8cff);box-shadow:0 8px 24px rgba(124,93,255,.25),inset 0 1px 0 rgba(255,255,255,.3);flex-shrink:0;}
.hero-text h1{font-size:24px;font-weight:700;letter-spacing:-.3px;color:var(--text);}
.hero-text p{color:var(--text-dim);font-size:13px;margin-top:2px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius-lg);padding:24px;margin-bottom:16px;
  box-shadow:var(--shadow-sm);transition:box-shadow .2s ease,border-color .2s ease,transform .2s ease;}
.card:hover{box-shadow:var(--shadow);border-color:var(--border-2);}
.card-head{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.card-icon{width:38px;height:38px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;
  background:linear-gradient(135deg,rgba(124,93,255,.1),rgba(91,140,255,.1));border:1px solid rgba(124,93,255,.1);}
.card-icon.config-icon{background:linear-gradient(135deg,rgba(16,185,129,.1),rgba(91,140,255,.1));border-color:rgba(16,185,129,.1);}
.card-title{flex:1;min-width:0}
.card-title h2{font-size:15px;font-weight:600;color:var(--text);letter-spacing:-.1px}
.card-title p{font-size:12px;color:var(--text-muted);margin-top:1px}
.status-badge{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:999px;font-size:13px;font-weight:500;
  white-space:nowrap;border:1px solid transparent;transition:all .3s ease;}
.status-badge .dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.status-running{background:var(--success-soft);color:var(--success);border-color:rgba(16,185,129,.2);}
.status-running .dot{background:var(--success);box-shadow:0 0 0 3px rgba(16,185,129,.15);animation:pulse-success 2s ease-in-out infinite;}
.status-stopped{background:var(--danger-soft);color:var(--danger);border-color:rgba(239,68,68,.2);}
.status-stopped .dot{background:var(--danger)}
.status-loading{background:var(--accent-soft);color:var(--accent-1);border-color:rgba(124,93,255,.2);}
.status-loading .dot{background:var(--accent-1);animation:pulse-accent 1.4s ease-in-out infinite;}
@keyframes pulse-success{0%,100%{box-shadow:0 0 0 3px rgba(16,185,129,.15)}50%{box-shadow:0 0 0 6px rgba(16,185,129,.04)}}
@keyframes pulse-accent{0%,100%{box-shadow:0 0 0 3px rgba(124,93,255,.15)}50%{box-shadow:0 0 0 6px rgba(124,93,255,.04)}}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 18px;border:none;border-radius:var(--radius-sm);
  font-size:14px;font-weight:500;font-family:inherit;cursor:pointer;white-space:nowrap;transition:all .18s cubic-bezier(.4,0,.2,1);}
.btn:active:not(:disabled){transform:translateY(1px) scale(.99)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn-primary{background:linear-gradient(135deg,var(--accent-1),var(--accent-2));color:#fff;
  box-shadow:0 4px 12px rgba(124,93,255,.25),inset 0 1px 0 rgba(255,255,255,.2);}
.btn-primary:hover:not(:disabled){box-shadow:0 6px 18px rgba(124,93,255,.35),inset 0 1px 0 rgba(255,255,255,.25);transform:translateY(-1px);}
.btn-primary:active:not(:disabled){transform:translateY(0) scale(.99)}
.btn-danger{background:rgba(239,68,68,.06);color:var(--danger);border:1px solid rgba(239,68,68,.2);}
.btn-danger:hover:not(:disabled){background:rgba(239,68,68,.12);border-color:rgba(239,68,68,.35);}
.btn-ghost{background:var(--panel-inner);color:var(--text);border:1px solid var(--border-2);}
.btn-ghost:hover:not(:disabled){border-color:var(--border-focus);color:var(--accent-1);background:var(--accent-soft);}
.btn-sm{padding:7px 14px;font-size:13px}
.btn-group{display:flex;gap:10px;flex-wrap:wrap}
.session-info{background:var(--panel-inner);border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px 18px;margin-top:14px;
  display:grid;grid-template-columns:auto 1fr;gap:6px 20px;font-size:13px;animation:fadeSlide .3s ease;}
.session-info .k{color:var(--text-muted);font-size:12px;display:flex;align-items:center}
.session-info .v{font-family:'JetBrains Mono','SF Mono',Consolas,monospace;color:var(--text);font-size:12px;word-break:break-all;}
@keyframes fadeSlide{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.login-area{display:none;margin-top:18px;padding:24px;background:var(--panel-inner);border:1px solid var(--border);border-radius:var(--radius);}
.login-area.show{display:block;animation:slideDown .3s cubic-bezier(.4,0,.2,1)}
@keyframes slideDown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.qr-wrap{display:flex;justify-content:center;align-items:center;margin-bottom:16px;min-height:60px}
.qr-wrap img{width:220px;height:220px;border-radius:14px;border:1px solid rgba(124,93,255,.15);background:#fff;padding:10px;
  image-rendering:pixelated;box-shadow:0 8px 32px rgba(23,26,46,.1),0 0 0 6px rgba(124,93,255,.04);animation:qrIn .4s cubic-bezier(.4,0,.2,1);}
@keyframes qrIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
.qr-wrap .spinner{width:40px;height:40px;border:3px solid rgba(124,93,255,.12);border-top-color:var(--accent-1);
  border-radius:50%;animation:spin .8s linear infinite;}
.login-status{text-align:center;font-size:13px;color:var(--text-dim);margin-bottom:14px;font-weight:500;}
.login-error{display:none;margin-top:14px;padding:12px 16px;background:#fef2f2;border:1px solid rgba(239,68,68,.3);border-radius:var(--radius-sm);color:#b91c1c;font-size:13px;line-height:1.6;animation:slideDown .3s cubic-bezier(.4,0,.2,1);}
.login-error.show{display:block}
.link-box{display:flex;align-items:center;gap:10px;background:#fff;border:1px solid var(--border);border-radius:var(--radius-xs);
  padding:10px 14px;margin-bottom:14px;font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-dim);}
.link-box > span:first-child{opacity:.5;flex-shrink:0}
.link-box .text{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;user-select:all;}
.toggle-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 0;border-top:1px solid var(--border);}
.toggle-row:first-of-type{border-top:none;padding-top:0}
.toggle-row:last-of-type{padding-bottom:0}
.toggle-info .title{font-size:14px;font-weight:500;color:var(--text)}
.toggle-info .desc{font-size:12px;color:var(--text-muted);margin-top:2px}
.switch{position:relative;width:44px;height:24px;flex-shrink:0;cursor:pointer;display:inline-block;}
.switch input{display:none}
.switch .track{position:absolute;inset:0;background:#e4e6ee;border:1px solid transparent;border-radius:999px;cursor:pointer;transition:all .2s ease;}
.switch .track::after{content:'';position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;
  transition:transform .2s cubic-bezier(.4,0,.2,1);box-shadow:0 1px 3px rgba(0,0,0,.15);}
.switch input:checked + .track{background:linear-gradient(135deg,var(--accent-1),var(--accent-2));box-shadow:0 2px 10px rgba(124,93,255,.3);}
.switch input:checked + .track::after{transform:translateX(20px)}
.switch.saving .track{opacity:.6}
.toast{position:fixed;top:24px;right:24px;z-index:9999;padding:12px 20px;border-radius:var(--radius-sm);font-size:14px;font-weight:500;
  box-shadow:var(--shadow-lg);opacity:0;transform:translateX(24px) scale(.98);pointer-events:none;
  transition:all .25s cubic-bezier(.4,0,.2,1);max-width:360px;background:#fff;border:1px solid var(--border);}
.toast.show{opacity:1;transform:none;pointer-events:auto}
.toast-success{color:#047857;border-color:rgba(16,185,129,.3);background:#ecfdf5}
.toast-error{color:#b91c1c;border-color:rgba(239,68,68,.3);background:#fef2f2}
.toast-info{color:#4338ca;border-color:rgba(124,93,255,.3);background:#eef2ff}
.modal-mask{position:fixed;inset:0;background:rgba(23,26,46,.4);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  display:none;align-items:center;justify-content:center;z-index:1000;padding:20px;}
.modal-mask.show{display:flex;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal{background:#fff;border:1px solid var(--border);border-radius:var(--radius-lg);padding:28px;max-width:440px;width:100%;
  box-shadow:0 24px 64px rgba(23,26,46,.2);animation:modalIn .25s cubic-bezier(.4,0,.2,1);}
@keyframes modalIn{from{opacity:0;transform:scale(.96) translateY(8px)}to{opacity:1;transform:none}}
.modal h3{font-size:17px;margin-bottom:12px;color:var(--warn);display:flex;align-items:center;gap:10px;}
.modal p{color:var(--text-dim);font-size:14px;line-height:1.7;margin-bottom:22px}
.modal p b{color:var(--danger)}
.modal .actions{display:flex;gap:10px;justify-content:flex-end}
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;
  border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;}
@keyframes spin{to{transform:rotate(360deg)}}
@media (max-width:640px){
  body{padding:24px 16px 60px}
  .container{max-width:100%}
  .hero-icon{width:48px;height:48px;font-size:24px}
  .hero-text h1{font-size:20px}
  .card{padding:20px}
  .card-head{flex-wrap:wrap}
  .status-badge{order:3;width:100%;justify-content:center}
  .btn-group .btn{flex:1}
  .toast{top:16px;right:16px;left:16px;max-width:none}
  .qr-wrap img{width:180px;height:180px}
}
</style>
</head>
<body>
<div class="container">

  <div class="hero">
    <div class="hero-icon">🤖</div>
    <div class="hero-text">
      <h1>微信 Bot</h1>
      <p>扫码登录 · 消息收发 · 状态监控</p>
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-icon">📡</div>
      <div class="card-title">
        <h2>运行状态</h2>
        <p>Bot 实时状态与操作</p>
      </div>
      <span id="statusBadge" class="status-badge status-stopped"><span class="dot"></span><span id="statusText">加载中…</span></span>
    </div>

    <div class="btn-group">
      <button class="btn btn-primary" onclick="startW()" id="btnStart">▶️ 启动</button>
      <button class="btn btn-danger"  onclick="stopW()"  id="btnStop">⏹️ 停止</button>
      <button class="btn btn-ghost"   onclick="restartW()" id="btnRestart">🔄 重启</button>
    </div>

    <div class="session-info" id="sessionInfo" style="display:none">
      <div class="k">Bot ID</div>   <div class="v" id="sessBotId">-</div>
      <div class="k">登录时间</div><div class="v" id="sessTime">-</div>
    </div>

      <div class="login-area" id="loginArea">
        <div class="qr-wrap" id="qrWrapper"><div class="spinner"></div></div>
        <div class="login-status" id="loginStatusText">正在生成二维码…</div>
        <div class="link-box">
          <span>🔗</span>
          <span class="text" id="linkBox"></span>
        </div>
        <div class="btn-group" style="justify-content:center">
          <button class="btn btn-ghost btn-sm" onclick="copyLink()">📋 复制链接</button>
          <button class="btn btn-danger btn-sm" onclick="abortLogin()">🛑 终止登录</button>
        </div>
      </div>
      <div class="login-error" id="loginError"></div>   <!-- ★ 新增 -->
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="card-icon config-icon">⚙️</div>
      <div class="card-title">
        <h2>配置</h2>
        <p>切换立即生效</p>
      </div>
    </div>

    <div class="toggle-row">
      <div class="toggle-info">
        <div class="title">图片输出</div>
        <div class="desc">以图片形式发送系统状态</div>
      </div>
      <label class="switch" id="swUseImage"><input type="checkbox" id="useImage"><span class="track"></span></label>
    </div>

    <div class="toggle-row">
      <div class="toggle-info">
        <div class="title">使用背景图</div>
        <div class="desc">需要 data/background.png</div>
      </div>
      <label class="switch" id="swUseBackground"><input type="checkbox" id="useBackground"><span class="track"></span></label>
    </div>

    <div class="toggle-row">
      <div class="toggle-info">
        <div class="title">启动时自动登录</div>
        <div class="desc">插件加载后自动启动微信 Bot</div>
      </div>
      <label class="switch" id="swAutoLogin"><input type="checkbox" id="autoLogin"><span class="track"></span></label>
    </div>

    <div class="toggle-row">
      <div class="toggle-info">
        <div class="title">IP Fallback</div>
        <div class="desc">DNS 解析失败时启用内置 IP 列表（默认关闭）</div>
      </div>
      <label class="switch" id="swUseIpFallback"><input type="checkbox" id="useIpFallback"><span class="track"></span></label>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<div class="modal-mask" id="restartModal">
  <div class="modal">
    <h3>⚠️ 确认重启</h3>
    <p>重启将<b>清除当前登录信息</b>，需要重新扫码登录。确定要继续吗？</p>
    <div class="actions">
      <button class="btn btn-ghost" onclick="closeRestartModal()">取消</button>
      <button class="btn btn-primary" onclick="confirmRestart()">确认重启</button>
    </div>
  </div>
</div>

<script>
var API = {
  state:        '/api/ext/wechat/state',
  config:       '/api/ext/wechat/config',
  start:        '/api/ext/wechat/start',
  stop:         '/api/ext/wechat/stop',
  restart:      '/api/ext/wechat/restart',
  login_status: '/api/ext/wechat/login-status',
  abort_login:  '/api/ext/wechat/abort-login'
};

var state = {
  status: 'no_token',
  loginTimer: null,
  qrCode: '',
  lastQrGen: 0,
  dirtyKeys: {}
};

function $(id){return document.getElementById(id)}
function getUserLogin(){return sessionStorage.getItem('wxbot_login_active')==='1'}
function setUserLogin(v){sessionStorage.setItem('wxbot_login_active', v?'1':'0')}

function toast(msg, type){
  var t = $('toast');
  t.textContent = msg;
  t.className = 'toast toast-' + (type||'info') + ' show';
  clearTimeout(t._t);
  t._t = setTimeout(function(){ t.classList.remove('show'); }, 3000);
}

function loading(btn, txt){
  btn.disabled = true;
  btn.dataset.orig = btn.innerHTML;
  btn.innerHTML = '<span class="spin"></span> ' + txt;
}
function resetBtn(btn){
  if (btn.dataset.orig){ btn.innerHTML = btn.dataset.orig; delete btn.dataset.orig; }
  btn.disabled = false;
}

function bindToggle(inputId, key, swId){
  var input = $(inputId);
  var sw = $(swId);
  input.addEventListener('change', function(){
    var val = input.checked;
    state.dirtyKeys[key] = val;
    if (sw) sw.classList.add('saving');
    var payload = {}; payload[key] = val;
    fetch(API.config, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r){return r.json()}).then(function(r){
      if (!r.ok) {
        toast('保存失败: ' + (r.message || '未知错误'), 'error');
        input.checked = !val;
        state.dirtyKeys[key] = !val;
      } else {
        toast('已保存', 'success');
      }
    }).catch(function(e){
      toast('保存失败: ' + e.message, 'error');
      input.checked = !val;
      state.dirtyKeys[key] = !val;
    }).finally(function(){
      if (sw) sw.classList.remove('saving');
      setTimeout(function(){ delete state.dirtyKeys[key]; }, 1500);
    });
  });
}

function render(d){
  var c = d.config || {};
  if (!('use_image' in state.dirtyKeys))
    $('useImage').checked = c.use_image !== false;
  if (!('use_background' in state.dirtyKeys))
    $('useBackground').checked = c.use_background === true;
  if (!('auto_login' in state.dirtyKeys))
    $('autoLogin').checked = c.auto_login === true;
  if (!('use_ip_fallback' in state.dirtyKeys))
    $('useIpFallback').checked = c.use_ip_fallback === true;

  var bs = d.bot_status || 'no_token';
  state.status = bs;

  var badge = $('statusBadge'), txt = $('statusText');
  var bStart = $('btnStart'), bStop = $('btnStop'), bRestart = $('btnRestart');
  var sess = $('sessionInfo'), la = $('loginArea');

  if (bs === 'logging_in'){
    badge.className = 'status-badge status-loading';
    txt.textContent = '登录中…';
    bStart.disabled = true; bStop.disabled = false; bRestart.disabled = true;
    sess.style.display = 'none';
    var hasQr = !!(d.qr_image || d.qr_code);
    var show = getUserLogin() || d.login_source === 'qq'
            || d.login_source === 'web' || hasQr;
    if (show){
      la.classList.add('show');
      if (!d.qr_image && !d.qr_code){
        var wrap = $('qrWrapper');
        if (!wrap.querySelector('.spinner')){
          wrap.innerHTML = '<div class="spinner"></div>';
        }
        $('loginStatusText').textContent = '正在生成二维码…';
        return;
      }
      if (d.qr_image && d.qr_generated_at > state.lastQrGen){
        state.lastQrGen = d.qr_generated_at;
        updateQr(d.qr_image, d.qr_url, d.qr_code);
        startPoll(d.qr_code);
      } else if (d.qr_code && !state.qrCode){
        state.qrCode = d.qr_code;
        if (d.qr_image) updateQr(d.qr_image, d.qr_url, d.qr_code);
        startPoll(d.qr_code);
      }
    }
  } else if (bs === 'running'){
    badge.className = 'status-badge status-running';
    txt.textContent = '运行中';
    bStart.disabled = true; bStop.disabled = false; bRestart.disabled = false;
    if (d.session){
      sess.style.display = 'grid';
      $('sessBotId').textContent = d.session.accountId || '-';
      $('sessTime').textContent  = d.session.savedAt  || '-';
    } else {
      sess.style.display = 'none';
    }
    setUserLogin(false);
    la.classList.remove('show');
    stopPoll();
  } else {
    badge.className = 'status-badge status-stopped';
    txt.textContent = bs === 'stopped' ? '已停止' : '未运行';
    bStart.disabled = false; bStop.disabled = true; bRestart.disabled = false;
    sess.style.display = 'none';
    if (!getUserLogin()){
      la.classList.remove('show');
      stopPoll();
    }
  }

  // ★ 新增：显示最近一次登录失败原因
  var errBox = $('loginError');
  if (d.last_error && bs !== 'logging_in') {
    if (d.last_error_friendly) {
      errBox.textContent = '⚠️ ' + d.last_error;
    } else {
      errBox.textContent = '⚠️ 登录失败，请查看日志或发送「微信登录」重试';
    }
    errBox.classList.add('show');
  } else {
    errBox.classList.remove('show');
  }
}


function poll(){
  fetch(API.state).then(function(r){return r.json()}).then(function(r){
    if (r.ok) render(r.data);
  }).catch(function(){});
}

function startW(){
  if (state.status === 'running'){ toast('已在运行中','error'); return; }
  if (state.status === 'logging_in'){ toast('正在登录中','error'); return; }
  var b = $('btnStart');
  loading(b, '启动中…');
  setUserLogin(true);
  var la = $('loginArea'); la.classList.add('show');
  $('qrWrapper').innerHTML = '<div class="spinner"></div>';
  $('loginStatusText').textContent = '正在生成二维码…';
  $('loginError').classList.remove('show');

  fetch(API.start, {method:'POST'}).then(function(r){return r.json()}).then(function(r){
    if (r.ok){
      toast(r.message,'success');
      if (r.need_login && r.qr_image){
        state.lastQrGen = Date.now()/1000;
        updateQr(r.qr_image, r.qr_url, r.qr_code);
        startPoll(r.qr_code);
      } else if (!r.need_login){
        setUserLogin(false); la.classList.remove('show');
      }
    } else {
      toast(r.message || '启动失败','error');
      setUserLogin(false); la.classList.remove('show');
    }
  }).catch(function(e){ toast('启动失败: '+e.message,'error'); setUserLogin(false); la.classList.remove('show'); })
    .finally(function(){ resetBtn(b); setTimeout(poll, 500); });
}

function stopW(){
  if (state.status !== 'running' && state.status !== 'logging_in'){ toast('当前未在运行','error'); return; }
  if (!confirm('确定停止微信Bot吗？')) return;
  var b = $('btnStop');
  loading(b, '停止中…');
  setUserLogin(false);
  fetch(API.stop, {method:'POST'}).then(function(r){return r.json()}).then(function(r){
    toast(r.message, r.ok?'success':'error');
    stopPoll();
    $('loginArea').classList.remove('show');
  }).catch(function(e){ toast('停止失败: '+e.message,'error'); })
    .finally(function(){ resetBtn(b); setTimeout(poll, 1500); });
}

function restartW(){
  if (state.status !== 'running' && state.status !== 'stopped'){ toast('当前状态无法重启','error'); return; }
  $('restartModal').classList.add('show');
}
function closeRestartModal(){ $('restartModal').classList.remove('show'); }

function confirmRestart(){
  closeRestartModal();
  var b = $('btnRestart');
  loading(b, '重启中…');
  setUserLogin(true);
  var la = $('loginArea'); la.classList.add('show');
  $('qrWrapper').innerHTML = '<div class="spinner"></div>';
  $('loginStatusText').textContent = '正在生成二维码…';
  $('loginError').classList.remove('show');

  fetch(API.restart, {method:'POST'}).then(function(r){return r.json()}).then(function(r){
    if (r.ok){
      toast(r.message,'success');
      if (r.need_login && r.qr_image){
        state.lastQrGen = Date.now()/1000;
        updateQr(r.qr_image, r.qr_url, r.qr_code);
        startPoll(r.qr_code);
      }
    } else {
      toast(r.message || '重启失败','error');
      setUserLogin(false); la.classList.remove('show');
    }
  }).catch(function(e){ toast('重启失败: '+e.message,'error'); setUserLogin(false); la.classList.remove('show'); })
    .finally(function(){ resetBtn(b); setTimeout(poll, 500); });
}

function updateQr(img, url, code){
  state.qrCode = code || '';
  var wrap = $('qrWrapper');
  if (img){ wrap.innerHTML = '<img src="'+img+'" alt="二维码">'; }
  else if (url){ wrap.innerHTML = '<img src="'+url+'" alt="二维码">'; }
  if (url){ $('linkBox').textContent = url; }
  $('loginStatusText').textContent = '等待扫码…';
}

function copyLink(){
  var t = $('linkBox').textContent;
  if (!t){ toast('暂无链接','error'); return; }
  if (navigator.clipboard){
    navigator.clipboard.writeText(t).then(function(){ toast('链接已复制','success'); }, function(){ fallbackCopy(t); });
  } else fallbackCopy(t);
}
function fallbackCopy(t){
  var ta = document.createElement('textarea');
  ta.value = t; ta.style.position='fixed'; ta.style.left='-9999px';
  document.body.appendChild(ta); ta.select();
  try{ document.execCommand('copy'); toast('链接已复制','success'); }catch(e){ toast('复制失败','error'); }
  document.body.removeChild(ta);
}

function abortLogin(){
  setUserLogin(false);
  fetch(API.abort_login, {method:'POST'}).then(function(r){return r.json()}).then(function(r){
    toast(r.message || '已终止', r.ok?'success':'error');
  }).finally(function(){
    stopPoll();
    $('loginArea').classList.remove('show');
    setTimeout(poll, 1200);
  });
}

function startPoll(qr){
  stopPoll();
  if (!qr) return;
  state.loginTimer = setInterval(function(){
    fetch(API.login_status + '?qrcode=' + encodeURIComponent(qr))
      .then(function(r){return r.json()}).then(function(r){
        if (!r.ok) return;
        var s = r.data.status;
        if (s === 'scaned'){
          $('loginStatusText').textContent = '👀 已扫码，请在手机上确认…';
        } else if (s === 'confirmed'){
          $('loginStatusText').textContent = '✅ 登录成功！';
          stopPoll(); setUserLogin(false);
          setTimeout(function(){ $('loginArea').classList.remove('show'); poll(); }, 1000);
        } else if (s === 'expired'){
          $('loginStatusText').textContent = '⚠️ 二维码已过期，请重新启动';
          stopPoll(); setUserLogin(false);
        }
      }).catch(function(){});
  }, 2000);
}
function stopPoll(){ if (state.loginTimer){ clearInterval(state.loginTimer); state.loginTimer=null; } }

document.addEventListener('click', function(e){
  if (e.target.id === 'restartModal') e.target.classList.remove('show');
});

(function init(){
  bindToggle('useImage', 'use_image', 'swUseImage');
  bindToggle('useBackground', 'use_background', 'swUseBackground');
  bindToggle('autoLogin', 'auto_login', 'swAutoLogin');
  bindToggle('useIpFallback', 'use_ip_fallback', 'swUseIpFallback');

  if (getUserLogin()) $('loginArea').classList.add('show');
  poll();
  setInterval(poll, 3000);
})();
</script>
</body>
</html>'''

register_page(
    key='wechat-bot-config',
    label='微信Bot 配置',
    source='plugin',
    source_name='wechat_bot',
    html=CONFIG_PAGE_HTML,
    icon='robot',
)


# ══════════════════════════════════════════════════════════════════════════
# 生命周期
# ══════════════════════════════════════════════════════════════════════════

@on_load
async def init():
    global _wechat_initialized
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _check_and_install_deps)
    load_config()

    try:
        ip_fallback.set_use_fallback(_config.get('use_ip_fallback', False))
    except Exception as e:
        log.warning(f"[微信Bot] 应用 IP fallback 开关失败: {e}")

    _load_submodules()

    if not _ensure_plugin_thread():
        log.error("[微信Bot] 插件线程启动失败")
    else:
        log.info("[微信Bot] 插件线程已就绪")

    _wechat_initialized = True
    try:
        enabled = ip_fallback.get_use_fallback()
    except Exception:
        enabled = False
    log.info(f"[微信Bot] IP fallback: {'开' if enabled else '关（默认 DNS）'}")

    auto_login = _config.get('auto_login', False)
    if auto_login:
        log.info("[微信Bot] 自动启动...")
        try:
            result = await _await_plugin(_api_start_async(), timeout=20)
            log.info(f"[微信Bot] 自动启动: {result['message']}")
        except Exception as e:
            log.error(f"[微信Bot] 自动启动失败: {e}")
    else:
        log.info("[微信Bot] 已加载（静默模式）")


@on_unload
async def cleanup():
    global _wechat_initialized, _plugin_thread
    try:
        if _plugin_loop is not None:
            await _await_plugin(_do_stop_wechat(), timeout=15)
    except Exception as e:
        log.warning(f"[微信Bot] 卸载时停止失败: {e}")

    if _plugin_loop is not None:
        try:
            _plugin_loop.call_soon_threadsafe(_plugin_loop.stop)
        except Exception:
            pass

    unregister_page('wechat-bot-config')
    _wechat_initialized = False

    try:
        sys.modules.pop('ip_fallback', None)
        _purge_plugin_submodules()
    except Exception:
        pass

    log.info("[微信Bot] 已卸载")


# ══════════════════════════════════════════════════════════════════════════
# QQ 控制指令
# ══════════════════════════════════════════════════════════════════════════

async def _wait_qr_updates_qq(event, buttons, initial_link: str):
    """★ v1.0.4：QQ 端持续转发二维码刷新 + 登录最终结果

    - 收到 refreshed → 发送新二维码图片和链接
    - 收到 done → 发送最终结果并返回
    - 收到 timeout → 若后台已结束则退出，否则继续等
    """
    after_link = initial_link
    while True:
        try:
            status = await _await_plugin(
                _wait_qr_event_async(after_link, timeout=350), timeout=360)
        except Exception as e:
            log.error(f"[微信Bot] 等待二维码事件失败: {e}")
            return

        t = status.get("type")
        if t == "done":
            holder = status.get("holder") or {}
            if holder.get("ok"):
                token = holder.get("token") or {}
                await event.reply(
                    f"## ✅ 登录成功\nBot: `{token.get('accountId','?')}`",
                    msg_type=2, buttons=buttons)
            else:
                msg = holder.get('msg', '登录未成功')
                if '终止' in msg:
                    log.info("[微信Bot] 登录已被用户终止，跳过重复回复")
                    return
                await event.reply(f"## ❌ {msg}", msg_type=2, buttons=buttons)
            return

        if t == "refreshed":
            new_link = status.get("qr_link", "")
            after_link = new_link
            try:
                new_bytes = generate_qr_image(new_link)
                await _reply_qq_image(event, new_bytes,
                                      "📱 二维码已刷新", "wechat-qr.png")
                await event.reply(
                    f"## 🔄 二维码已刷新\n---\n```\n{new_link}\n```\n"
                    f"> 发送 `微信终止` 取消",
                    msg_type=2, buttons=buttons)
            except Exception as e:
                log.error(f"[微信Bot] 发送刷新二维码失败: {e}")
            continue

        if t == "timeout":
            # 超时点：检查后台是否已结束
            try:
                still = await _await_plugin(_is_login_in_progress_async(), timeout=3)
            except Exception:
                still = False
            if not still:
                await event.reply(
                    "## ⏱️ 等待超时，后台登录流程已结束",
                    msg_type=2, buttons=buttons)
                return
            # 后台仍在跑（极少见），继续等
            continue

        # 未知类型：退出保护
        log.warning(f"[微信Bot] 未知的 qr event 类型: {t}")
        return


@handler(r'^微信状态$', name='微信状态', desc='查看微信Bot运行状态', owner_only=True)
async def cmd_wechat_status(event, match):
    data = await _await_plugin(_api_get_state_async(), timeout=10)
    bs = data.get('bot_status')
    if bs == 'running' and data.get('session'):
        sess = data['session']
        s = (f"## 🤖 微信 Bot\n---\n✅ 运行中\n"
             f"Bot: `{sess.get('accountId','?')}`\n"
             f"登录: {sess.get('savedAt','?')}")
        buttons = [[{'text': '登出', 'data': '微信登出', 'enter': True},
                    {'text': '重启', 'data': '微信重启', 'enter': True},
                    {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    elif bs == 'logging_in':
        s = "## 🤖 微信 Bot\n---\n🔄 登录中..."
        buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},
                    {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
        await event.reply(s, msg_type=2, buttons=buttons)

        try:
            info = await _await_plugin(_get_qr_info(), timeout=5)
            qr_link = info.get('qr_url', '')
            if qr_link:
                qr_bytes = generate_qr_image(qr_link)
                await _reply_qq_image(event, qr_bytes,
                                      "📱 请扫描上方二维码", "wechat-qr.png")
                await event.reply(
                    f"## 📱 微信扫码登录\n---\n```\n{qr_link}\n```",
                    msg_type=2, buttons=buttons)
        except Exception as e:
            log.error(f"[微信Bot] 状态查询附带二维码失败: {e}")
        return
    else:
        s = "## 🤖 微信 Bot\n---\n❌ 未运行\n发送 `微信登录` 启动"
        buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},
                    {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    await event.reply(s, msg_type=2, buttons=buttons)


@handler(r'^微信登录$', name='微信登录', desc='启动微信Bot', owner_only=True)
async def cmd_wechat_login(event, match):
    buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},
                {'text': '状态', 'data': '微信状态', 'enter': True},
                {'text': '帮助', 'data': '微信帮助', 'enter': True}]]

    data = await _await_plugin(_api_get_state_async(), timeout=10)
    bs = data.get('bot_status')

    if bs == 'running':
        await event.reply("## ⚠️ 已在运行中", msg_type=2, buttons=buttons)
        return

    if bs == 'logging_in':
        await event.reply(
            "## ℹ️ 登录已在进行中，二维码稍后发送\n> 发送 `微信终止` 取消",
            msg_type=2, buttons=buttons)
    else:
        # 用"启动"而不是"生成二维码"，因为有 token 时会直接复用
        await event.reply("## 🔐 正在启动...\n> 发送 `微信终止` 取消",
                          msg_type=2, buttons=buttons)
        result = await _await_plugin(_api_start_async(), timeout=15)
        if not result.get('ok'):
            await event.reply(
                f"## ❌ {result.get('message', '启动失败')}",
                msg_type=2, buttons=buttons)
            return
        # ★ 关键修复：有 token 直接启动成功，不需要二维码
        if not result.get('need_login'):
            await event.reply(
                f"## ✅ 已启动\n{result.get('message', '复用登录凭证')}",
                msg_type=2, buttons=buttons)
            return

    # 需要登录 → 走二维码流程
    qr_ok = await _await_plugin(_wait_qr_ready(30), timeout=35)
    if not qr_ok:
        await event.reply("## ❌ 生成二维码超时", msg_type=2, buttons=buttons)
        return

    info = await _await_plugin(_get_qr_info(), timeout=5)
    qr_link = info.get('qr_url', '')
    if not qr_link:
        await event.reply("## ⚠️ 二维码未就绪", msg_type=2, buttons=buttons)
        return

    try:
        qr_bytes = generate_qr_image(qr_link)
        await _reply_qq_image(event, qr_bytes,
                              "📱 请扫描上方二维码", "wechat-qr.png")
        await event.reply(
            f"## 📱 微信扫码登录\n---\n```\n{qr_link}\n```\n"
            f"> 发送 `微信终止` 取消",
            msg_type=2, buttons=buttons)
    except Exception as e:
        log.error(f"[微信Bot] 发二维码失败: {e}")

    await _wait_qr_updates_qq(event, buttons, qr_link)

@handler(r'^微信终止$', name='微信终止', desc='终止登录', owner_only=True)
async def cmd_wechat_abort(event, match):
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},
                {'text': '状态', 'data': '微信状态', 'enter': True},
                {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    data = await _await_plugin(_api_get_state_async(), timeout=10)
    if data.get('bot_status') == 'running':
        await event.reply("## ⚠️ 已运行中，请用 `微信登出`", msg_type=2, buttons=buttons)
        return
    if data.get('bot_status') != 'logging_in':
        await event.reply("## ℹ️ 无登录进程", msg_type=2, buttons=buttons)
        return
    await _await_plugin(_api_abort_login_async(), timeout=10)
    await event.reply("## 🛑 已终止", msg_type=2, buttons=buttons)


@handler(r'^微信登出$', name='微信登出', desc='停止微信Bot', owner_only=True)
async def cmd_wechat_logout(event, match):
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},
                {'text': '状态', 'data': '微信状态', 'enter': True},
                {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    result = await _await_plugin(_api_stop_async(), timeout=15)
    ok = result.get('ok')
    msg = result.get('message', '')
    await event.reply(f"## {'👋 已登出' if ok else '⚠️ '+msg}",
                      msg_type=2, buttons=buttons)


@handler(r'^微信重启$', name='微信重启', desc='重启微信Bot', owner_only=True)
async def cmd_wechat_restart(event, match):
    buttons = [[{'text': '终止', 'data': '微信终止', 'enter': True},
                {'text': '状态', 'data': '微信状态', 'enter': True},
                {'text': '帮助', 'data': '微信帮助', 'enter': True}]]
    await event.reply("## 🔄 重启中...（将清除登录信息，需重新扫码）",
                      msg_type=2, buttons=buttons)
    result = await _await_plugin(_api_restart_async(), timeout=20)

    # 防御性检查：_api_restart_async 当前总是返回 need_login=True，
    # 但保留检查以应对未来变更
    if not result.get('ok'):
        await event.reply(
            f"## ❌ {result.get('message', '重启失败')}",
            msg_type=2, buttons=buttons)
        return
    if not result.get('need_login'):
        await event.reply(
            f"## ✅ 已重启\n{result.get('message', '')}",
            msg_type=2, buttons=buttons)
        return

    qr_ok = await _await_plugin(_wait_qr_ready(30), timeout=35)
    if not qr_ok:
        await event.reply("## ❌ 生成二维码超时", msg_type=2, buttons=buttons)
        return

    info = await _await_plugin(_get_qr_info(), timeout=5)
    qr_link = info.get('qr_url', '')
    if not qr_link:
        await event.reply("## ⚠️ 二维码未就绪", msg_type=2, buttons=buttons)
        return

    try:
        qr_bytes = generate_qr_image(qr_link)
        await _reply_qq_image(event, qr_bytes,
                              "📱 请扫描上方二维码", "wechat-qr.png")
        await event.reply(
            f"## 📱 微信扫码登录\n---\n```\n{qr_link}\n```\n"
            f"> 发送 `微信终止` 取消",
            msg_type=2, buttons=buttons)
    except Exception as e:
        log.error(f"[微信Bot] 发二维码失败: {e}")

    await _wait_qr_updates_qq(event, buttons, qr_link)

@handler(r'^微信帮助$', name='微信帮助', desc='查看帮助', owner_only=True)
async def cmd_wechat_help(event, match):
    buttons = [[{'text': '登录', 'data': '微信登录', 'enter': True},
                {'text': '登出', 'data': '微信登出', 'enter': True},
                {'text': '状态', 'data': '微信状态', 'enter': True}]]
    await event.reply(
        "## 🤖 微信 Bot 帮助\n---\n**QQ命令**\n"
        "<qqbot-cmd-input text='微信登录' />     <qqbot-cmd-input text='微信终止' />\n"
        "<qqbot-cmd-input text='微信登出' />     <qqbot-cmd-input text='微信重启' />\n"
        "<qqbot-cmd-input text='微信状态' />     <qqbot-cmd-input text='微信帮助' />\n"
        "<qqbot-cmd-input text='系统状态' />\n\n"
        "**微信端**\n"
        "`帮助`           `机器人列表`\n"
        "`启动`           `关闭` \n `dau`            `重启`\n"
        "`系统状态`\n"
        "**Web面板**\n侧边栏「微信Bot 配置」",
        msg_type=2, buttons=buttons,
    )


@handler(r'^/?系统状态$', name='系统状态', desc='查看系统资源占用',
         priority=5, block=True, owner_only=True)
async def cmd_system_status_qq(event, match):
    from .app.system_status import (collect_system_data,
                                    generate_status_image,
                                    generate_status_md)
    data = collect_system_data()
    cfg = _read_config_direct()
    use_image = cfg.get('use_image', True)
    buttons = [[{'text': '🔄 刷新状态', 'data': '系统状态', 'enter': True}]]

    if not use_image:
        await event.reply(generate_status_md(data), buttons=buttons)
        return

    loop = asyncio.get_event_loop()
    try:
        img = await loop.run_in_executor(None, generate_status_image, data)
    except Exception as e:
        log.error(f"[系统状态] 生成图片失败: {e}")
        await event.reply(f"生成失败: {e}")
        return
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    img_bytes = buf.getvalue()

    image_url = await _upload_to_hosting(img_bytes, "status.png", event=event)
    if not image_url:
        await event.reply(
            "⚠️ 图床不可用，已降级为文本模式\n\n" + generate_status_md(data),
            buttons=buttons)
        return
    md = f"![img #{img.size[0]}px #{img.size[1]}px]({image_url})"
    await event.reply(md, buttons=buttons)