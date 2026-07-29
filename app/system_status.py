"""
系统状态模块
"""

import os, logging
from datetime import datetime
from io import BytesIO

import yaml, psutil, platform
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web

from core.plugin.decorators import handler
from core.plugin.web_pages import register_route
from core.base.logger import get_logger, PLUGIN

log = get_logger(PLUGIN, '系统状态')

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.yaml')
FONT_PATH = os.path.join(PLUGIN_DIR, 'Microsoft YaHei.ttf')

DEFAULT_CONFIG = {'base_url': 'http://127.0.0.1:5200', 'use_image': True, 'use_background': False}


def _read_config() -> dict:
    if not os.path.exists(CONFIG_PATH): return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f: return {**DEFAULT_CONFIG, **(yaml.safe_load(f) or {})}
    except Exception: return dict(DEFAULT_CONFIG)


def _get_config_value(key: str, default=None):
    val = _read_config().get(key, default)
    return val if val is not None else default


@register_route('GET', '/api/ext/wechat/status.png', auth=False)
async def serve_status(request):
    status_path = os.path.join(DATA_DIR, 'status.png')
    if os.path.exists(status_path):
        resp = web.FileResponse(status_path)
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    return web.Response(status=404, text='Image not found')


WIDTH = 600; MIN_HEIGHT = 520; MAX_HEIGHT = 1200
PADDING_TOP = 20; PADDING_BOTTOM = 20
BG_COLOR = (30, 30, 30); TEXT_COLOR = (250, 250, 250)
ACCENT_COLOR = (0, 200, 255); BAR_BG = (60, 60, 60)

S1 = 50; S2 = 12; S3 = 36; S4 = 16; S5 = 44; S6 = 16; S7 = 60; S8 = 40; S9 = 12; S10 = 12; S11 = 40


def _dpb(draw, x, y, w, h, p, c=ACCENT_COLOR):
    fw = int(w * p / 100)
    draw.rectangle([x, y, x + w, y + h], fill=BAR_BG)
    if fw > 0: draw.rectangle([x, y, x + fw, y + h], fill=c)


def _gth(font, text):
    b = font.getbbox(text); return b[3] - b[1]


def _load_fonts():
    try:
        return (ImageFont.truetype(FONT_PATH, 36), ImageFont.truetype(FONT_PATH, 28), ImageFont.truetype(FONT_PATH, 24))
    except Exception: return (ImageFont.load_default(),) * 3


def collect_system_data() -> dict:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    os_name = platform.system()
    try: cpu = psutil.cpu_percent(interval=0.5) or 0.0
    except Exception: cpu = 0.0
    mem = psutil.virtual_memory()
    disks = []
    for p in psutil.disk_partitions():
        if 'loop' in p.device: continue
        if p.fstype:
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append((p.device, p.mountpoint, u.percent, u.used/(1024**3), u.total/(1024**3)))
            except PermissionError: continue
    return {'os_name': os_name, 'time': now, 'cpu_percent': cpu,
            'mem_percent': mem.percent, 'mem_used': mem.used/(1024**3), 'mem_total': mem.total/(1024**3),
            'disk_list': disks}


def generate_status_image(data: dict) -> Image.Image:
    tf, lf, df = _load_fonts()
    use_bg = _get_config_value('use_background', False)
    bg_path = os.path.join(DATA_DIR, 'background.png')

    lm = 30; mpw = 480; ph = 24; y = PADDING_TOP
    y += _gth(tf, "系统状态") + S1
    y += _gth(lf, f"系统：{data['os_name']}") + S2
    y += _gth(lf, f"时间：{data['time']}") + S3
    y += _gth(lf, f"CPU：{data['cpu_percent']:.1f}%") + S4; y += ph + S5
    mt = f"内存：{data['mem_percent']:.1f}% ({data['mem_used']:.1f}/{data['mem_total']:.1f}GB)"
    y += _gth(lf, mt) + S6; y += ph + S7
    y += _gth(lf, "磁盘") + S8

    dps = []; dbh = ph - 4
    for d in data['disk_list']:
        dev, mnt, pct, usd, tot = d
        y += _gth(df, f"{mnt} ({dev}) {pct:.1f}%") + S9; y += dbh + S10
        y += _gth(df, f"{usd:.1f}GB/{tot:.1f}GB") + S11
        dps.append({'device': dev, 'mount': mnt, 'percent': pct, 'used': usd, 'total': tot})
        if y > MAX_HEIGHT - PADDING_BOTTOM - 40: break

    th = max(y + PADDING_BOTTOM, MIN_HEIGHT)
    img = Image.new('RGB', (WIDTH, th), BG_COLOR)
    if use_bg and os.path.exists(bg_path):
        try:
            bg = Image.open(bg_path).convert('RGB').resize((WIDTH, th), Image.LANCZOS)
            img.paste(bg, (0, 0))
        except Exception: pass

    draw = ImageDraw.Draw(img); y = PADDING_TOP
    def dt(t, f, s):
        nonlocal y
        draw.text((lm, y), t, font=f, fill=TEXT_COLOR, anchor='la')
        y += _gth(f, t) + s

    dt("系统状态", tf, S1); dt(f"系统：{data['os_name']}", lf, S2)
    dt(f"时间：{data['time']}", lf, S3); dt(f"CPU：{data['cpu_percent']:.1f}%", lf, S4)
    _dpb(draw, lm, y, mpw, ph, data['cpu_percent']); y += ph + S5
    dt(mt, lf, S6); _dpb(draw, lm, y, mpw, ph, data['mem_percent']); y += ph + S7
    dt("磁盘", lf, S8)
    rc = 0
    for dp in dps:
        dt(f"{dp['mount']} ({dp['device']}) {dp['percent']:.1f}%", df, S9)
        _dpb(draw, lm, y, mpw-20, dbh, dp['percent'], c=(100,200,100)); y += dbh + S10
        dt(f"{dp['used']:.1f}GB/{dp['total']:.1f}GB", df, S11); rc += 1
    hd = len(data['disk_list']) - rc
    if hd > 0: draw.text((lm, y+2), f"...还有 {hd} 个磁盘未显示", font=df, fill=(150,150,150), anchor='la')
    return img


def get_status_image_bytes() -> bytes:
    log.info("[系统状态] ===== 开始生成图片 =====")
    data = collect_system_data()
    log.info(f"[系统状态] 数据: cpu={data['cpu_percent']}%, mem={data['mem_percent']}%")
    img = generate_status_image(data)
    log.info(f"[系统状态] 图片: {img.size}, mode={img.mode}")

    status_path = os.path.join(DATA_DIR, 'status.png')
    img.save(status_path, format='PNG', quality=95, optimize=True)
    file_size = os.path.getsize(status_path)
    log.info(f"[系统状态] 保存: {status_path}, size={file_size} bytes")

    buf = BytesIO(); img.save(buf, format='PNG'); result = buf.getvalue()
    log.info(f"[系统状态] bytes: {len(result)} bytes, 头: {result[:8].hex()}")
    if result[:4] == b'\x89PNG': log.info("[系统状态] ✅ PNG 头正确")
    else: log.error(f"[系统状态] ❌ PNG 头错误: {result[:8].hex()}")
    log.info("[系统状态] ===== 图片生成完成 =====")
    return result


def generate_status_text(data: dict) -> str:
    def bar(p, l=10): f = int(round(p/100*l)); return '█'*f + '░'*(l-f)
    lines = [f"📊 系统状态", f"系统：{data['os_name']}", f"时间：{data['time']}", "",
             f"CPU：{data['cpu_percent']:.1f}% {bar(data['cpu_percent'])}",
             f"内存：{data['mem_percent']:.1f}% {bar(data['mem_percent'])} ({data['mem_used']:.1f}/{data['mem_total']:.1f}GB)", "",
             "💾 磁盘："]
    for dev, mnt, pct, usd, tot in data['disk_list']:
        lines.append(f"  {mnt} ({dev}) {pct:.1f}% {bar(pct)} {usd:.1f}/{tot:.1f}GB")
    return '\n'.join(lines)


def generate_status_md(data: dict) -> str:
    def bar(p, l=10): f = int(round(p/100*l)); return '█'*f + '░'*(l-f)
    cpu = f"> **CPU**：{data['cpu_percent']:.1f}%\n> {bar(data['cpu_percent'])}"
    mem = f"> **内存**：{data['mem_percent']:.1f}%\n> {bar(data['mem_percent'])}\n> （{data['mem_used']:.1f}/{data['mem_total']:.1f}GB）"
    disks = []
    for dev, mnt, pct, usd, tot in data['disk_list']:
        disks.append(f"> **{dev}** ({mnt})：{pct:.1f}%\n> {bar(pct)}\n> （{usd:.1f}/{tot:.1f}GB）")
    return f"# 📊 系统状态\n\n> **{data['os_name']}** | {data['time']}\n\n{cpu}\n\n{mem}\n\n## 💾 磁盘\n{chr(10).join(disks) if disks else '> 无'}"


@handler(r'^/?系统状态$', name='系统状态', desc='查看系统资源占用', priority=5, block=True, owner_only=True)
async def cmd_system_status(event, match):
    from .commands import handle_qq_system_status
    await handle_qq_system_status(event)