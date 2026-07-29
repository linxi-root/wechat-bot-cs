"""
指令处理模块 - 装饰器风格注册
"""

import asyncio
import os
import json
import sys
from datetime import datetime

from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, '指令处理')

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')
WECHAT_TOKEN_FILE = os.path.join(DATA_DIR, '.weixin-token.json')


class WechatCommand:
    """微信指令装饰器"""
    _commands = []

    def __init__(self, *keywords: str, prefix: bool = False):
        self.keywords = [k.lower() for k in keywords]
        self.prefix = prefix

    def __call__(self, func):
        self._commands.append({'keywords': self.keywords, 'handler': func, 'prefix': self.prefix})
        return func

    @classmethod
    def match(cls, text: str):
        text_lower = text.strip().lower()
        for cmd in cls._commands:
            if not cmd['prefix'] and text_lower in cmd['keywords']:
                return cmd['handler'], ''
        for cmd in cls._commands:
            if cmd['prefix']:
                for kw in cmd['keywords']:
                    if text_lower == kw or text_lower.startswith(kw + ' '):
                        return cmd['handler'], text_lower[len(kw):].strip()
        return None, ''


# ══════════════════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════════════════

def _get_app():
    try:
        from core.application import get_app
        return get_app()
    except Exception: return None

def _get_bot_registry():
    app = _get_app()
    return app.bot_registry if app else None

def _get_config():
    try:
        from core.base.config import cfg
        return cfg
    except Exception: return None

def _get_all_bot_configs():
    cfg = _get_config()
    if not cfg: return []
    try:
        return [c for c in cfg.get_bot_configs() if isinstance(c, dict)]
    except Exception: return []

def _find_bot_config(appid):
    for bc in _get_all_bot_configs():
        if isinstance(bc, dict) and str(bc.get('appid', '')) == appid:
            return bc
    return None

def _mask_id(s, n=3):
    if not s or len(s) <= n * 2: return s or '?'
    return f'{s[:n]}****{s[-n:]}'


# ══════════════════════════════════════════════════════════════════════════
# 微信端指令
# ══════════════════════════════════════════════════════════════════════════

@WechatCommand('系统状态')
async def cmd_system_status(sender, to_user_id: str, context_token: str, text: str):
    from .system_status import collect_system_data, generate_status_text
    data = collect_system_data()
    await sender.send_text(to_user_id, generate_status_text(data), context_token)
    log.info(f"[微信指令] 系统状态")

@WechatCommand('帮助')
async def cmd_help(sender, to_user_id: str, context_token: str, text: str):
    await sender.send_text(to_user_id, "📋 可用指令：\n\n系统状态 — 服务器状态\n机器人列表 / bots — 机器人列表\n启动 / start <appid> — 启动机器人\n关闭 / stop <appid> — 关闭机器人\ndau <appid> [0726] — 日活统计\n重启 — 重启进程\n帮助 — 此帮助", context_token)

@WechatCommand('你好', 'hello', 'hi', '在吗')
async def cmd_hello(sender, to_user_id: str, context_token: str, text: str):
    await sender.send_text(to_user_id, "你好！发送「帮助」查看可用指令。", context_token)

@WechatCommand('机器人列表', 'bots')
async def cmd_bot_list(sender, to_user_id: str, context_token: str, text: str):
    registry = _get_bot_registry()
    all_configs = _get_all_bot_configs()
    if not all_configs:
        await sender.send_text(to_user_id, "📋 当前无机器人配置", context_token); return

    running_ids = set()
    bot_names = {}
    if registry:
        for bot in list(registry):
            aid = str(getattr(bot, 'appid', ''))
            if aid: running_ids.add(aid); bot_names[aid] = getattr(bot, 'name', '') or aid

    lines = ["📋 机器人列表：", ""]
    for i, bc in enumerate(all_configs, 1):
        if not isinstance(bc, dict): continue
        appid = str(bc.get('appid', '?'))
        running = appid in running_ids
        name = bot_names.get(appid) or appid
        status = "✅ 运行中" if running else "⏸️ 已停止"
        lines.append(f"{i}. {name} | {appid} | {status}")
    await sender.send_text(to_user_id, '\n'.join(lines), context_token)

@WechatCommand('启动', 'start', prefix=True)
async def cmd_start_bot(sender, to_user_id: str, context_token: str, text: str):
    appid = text.strip()
    if not appid: await sender.send_text(to_user_id, "❌ 请提供 AppID", context_token); return
    bc = _find_bot_config(appid)
    if not bc: await sender.send_text(to_user_id, f"❌ 未找到: {appid}", context_token); return
    registry = _get_bot_registry()
    if not registry: await sender.send_text(to_user_id, "❌ 无法获取实例", context_token); return
    if registry.get(appid): await sender.send_text(to_user_id, f"⚠️ 已在运行中", context_token); return
    try:
        app = _get_app()
        if app and app.bot_registry:
            inst = await app.bot_registry._start_one(bc)
            if inst: await sender.send_text(to_user_id, f"✅ {inst.name} ({appid}) 已启动", context_token)
            else: await sender.send_text(to_user_id, f"❌ 启动失败", context_token)
    except Exception as e: await sender.send_text(to_user_id, f"❌ 启动失败: {e}", context_token)

@WechatCommand('关闭', 'stop', prefix=True)
async def cmd_stop_bot(sender, to_user_id: str, context_token: str, text: str):
    appid = text.strip()
    if not appid: await sender.send_text(to_user_id, "❌ 请提供 AppID", context_token); return
    
    registry = _get_bot_registry()
    if not registry: await sender.send_text(to_user_id, "❌ 无法获取实例", context_token); return
    
    bot = registry.get(appid)
    if not bot: await sender.send_text(to_user_id, f"⚠️ {appid} 未在运行中", context_token); return

    try:
        name = getattr(bot, 'name', appid)
        # ★ 和框架一致：先 pop 再 stop
        registry._bots.pop(appid, None)
        await bot.stop()
        await sender.send_text(to_user_id, f"✅ {name} ({appid}) 已关闭", context_token)
        log.info(f"[微信指令] 关闭 {appid}")
    except Exception as e:
        await sender.send_text(to_user_id, f"❌ 关闭失败: {e}", context_token)


        
@WechatCommand('dau', prefix=True)
async def cmd_dau(sender, to_user_id: str, context_token: str, text: str):
    args = text.strip()
    if not args: await sender.send_text(to_user_id, "❌ 格式: dau <appid> [MMDD]", context_token); return
    parts = args.split(); appid = parts[0]; date_str = parts[1] if len(parts) > 1 else ''
    registry = _get_bot_registry()
    if not registry: await sender.send_text(to_user_id, "❌ 无法获取实例", context_token); return
    bot = registry.get(appid)
    if not bot: await sender.send_text(to_user_id, f"❌ 未找到或未运行: {appid}", context_token); return
    if not bot.log_service: await sender.send_text(to_user_id, "❌ 日志服务未启动", context_token); return
    try:
        import time as _time; t0 = _time.time()
        if date_str and len(date_str) == 4:
            year = datetime.now().year; month, day = int(date_str[:2]), int(date_str[2:])
            try: target_date = datetime(year, month, day); target_date = datetime(year-1, month, day) if target_date > datetime.now() else target_date
            except ValueError: await sender.send_text(to_user_id, "❌ 日期格式错误", context_token); return
            app = _get_app(); dau_svc = app.dau_service if app else None
            if not dau_svc: await sender.send_text(to_user_id, "❌ DAU 服务未启动", context_token); return
            data = await dau_svc.load(appid, target_date.strftime('%Y-%m-%d'))
            if not data: await sender.send_text(to_user_id, f"❌ 无数据", context_token); return
            lines = [f"📊 {bot.name} DAU", f"📅 {target_date.strftime('%m-%d')}", "", f"👤 活跃用户: {data.get('active_users',0)}", f"👥 活跃群组: {data.get('active_groups',0)}", f"💬 总消息: {data.get('total_messages',0)}"]
        else:
            today = datetime.now().strftime('%Y-%m-%d')
            agg = bot.log_service.query('message', "SELECT COUNT(*) AS total, COUNT(DISTINCT CASE WHEN user_id!='' THEN user_id END) AS users, COUNT(DISTINCT CASE WHEN group_id!='' AND group_id!='c2c' THEN group_id END) AS groups_ FROM log", date=today)
            if not agg or not agg[0].get('total'): await sender.send_text(to_user_id, "❌ 今日无数据", context_token); return
            stats = dict(agg[0])
            lines = [f"📊 {bot.name} 今日 DAU", f"📅 {datetime.now().strftime('%m-%d %H:%M')}", "", f"👤 活跃用户: {stats.get('users',0)}", f"👥 活跃群组: {stats.get('groups_',0)}", f"💬 总消息: {stats.get('total',0)}"]
        lines.append(f"🕒 {round((_time.time()-t0)*1000)}ms")
        await sender.send_text(to_user_id, '\n'.join(lines), context_token)
    except Exception as e: await sender.send_text(to_user_id, f"❌ 查询失败: {e}", context_token)

@WechatCommand('重启')
async def cmd_restart(sender, to_user_id: str, context_token: str, text: str):
    await sender.send_text(to_user_id, "🔄 正在重启...", context_token)
    await asyncio.sleep(0.5)
    try:
        app = _get_app()
        if app: app._restart_requested = True
        if app and app._stop_event: app._stop_event.set(); return
    except Exception: pass
    python = sys.executable; os.execv(python, [python] + sys.argv)


# ══════════════════════════════════════════════════════════════════════════
# QQ 端系统状态
# ══════════════════════════════════════════════════════════════════════════

async def handle_qq_system_status(event):
    from .system_status import collect_system_data, generate_status_image, generate_status_md, _get_config_value, DATA_DIR as SYS_DATA_DIR
    data = collect_system_data()
    use_image = _get_config_value('use_image', True)
    base_url = _get_config_value('base_url', 'http://127.0.0.1:5200')
    status_path = os.path.join(SYS_DATA_DIR, 'status.png')
    if not base_url: base_url = 'http://127.0.0.1:5200'
    if use_image:
        try:
            img = generate_status_image(data); img.save(status_path, format='PNG', quality=95, optimize=True)
        except Exception as e: await event.reply(f"生成图片失败: {e}"); return
        timestamp = int(datetime.now().timestamp())
        image_url = f"{base_url.rstrip('/')}/api/ext/wechat/status.png?t={timestamp}"
        md_content = f"![img #{img.size[0]}px #{img.size[1]}px]({image_url})"
        buttons = [[{'text': '🔄 刷新状态', 'data': '系统状态', 'enter': True}]]
        await event.reply(md_content, buttons=buttons)
    else:
        text_md = generate_status_md(data)
        buttons = [[{'text': '🔄 刷新状态', 'data': '系统状态', 'enter': True}]]
        await event.reply(text_md, buttons=buttons)

def read_token_info() -> dict:
    if not os.path.exists(WECHAT_TOKEN_FILE): return {}
    try:
        with open(WECHAT_TOKEN_FILE, 'r', encoding='utf-8') as f: return json.load(f)
    except Exception: return {}