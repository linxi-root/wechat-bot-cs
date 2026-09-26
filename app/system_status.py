"""系统状态模块 — 采集 + 图片/文本生成"""

import os
import platform
from datetime import datetime
from io import BytesIO
from typing import Optional

import psutil
import yaml
from PIL import Image, ImageDraw, ImageFont

PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'config.yaml')
BG_IMAGE_PATH = os.path.join(DATA_DIR, 'background.png')
FONT_PATH = os.path.join(PLUGIN_DIR, 'Microsoft YaHei.ttf')

WIDTH, MIN_HEIGHT, MAX_HEIGHT = 600, 520, 1200
PAD_TOP, PAD_BOTTOM = 20, 20

BG_COLOR     = (30, 30, 30)
TEXT_COLOR   = (250, 250, 250)
ACCENT_COLOR = (0, 200, 255)
BAR_BG       = (60, 60, 60)
DISK_COLOR   = (100, 200, 100)

SP_TITLE       = 50
SP_OS          = 12
SP_TIME        = 36
SP_CPU_TEXT    = 16
SP_CPU_BAR     = 44
SP_MEM_TEXT    = 16
SP_MEM_BAR     = 60
SP_DISK_TITLE  = 40
SP_DISK_LABEL  = 12
SP_DISK_BAR    = 12
SP_DISK_DETAIL = 40


def _read_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _find_font() -> Optional[str]:
    if os.path.exists(FONT_PATH):
        return FONT_PATH
    import sys as _sys
    system_fonts = {
        'win32':  ['C:/Windows/Fonts/msyh.ttc'],
        'linux':  ['/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
                   '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'],
        'darwin': ['/System/Library/Fonts/PingFang.ttc'],
    }
    for p in system_fonts.get(_sys.platform, []):
        if os.path.exists(p):
            return p
    return None


# ═══════════════════════════════════════════════════════════════
#  数据采集
# ═══════════════════════════════════════════════════════════════

def collect_system_data() -> dict:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        cpu = psutil.cpu_percent(interval=0.5) or 0.0
    except Exception:
        cpu = 0.0
    mem = psutil.virtual_memory()

    disks = []
    for p in psutil.disk_partitions():
        if 'loop' in p.device or 'snap' in p.device:
            continue
        if p.fstype:
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append((p.device, p.mountpoint, u.percent,
                              u.used / 1024**3, u.total / 1024**3))
            except Exception:
                continue

    return {
        'os_name': platform.system(),
        'time': now,
        'cpu_percent': cpu,
        'mem_percent': mem.percent,
        'mem_used': mem.used / 1024**3,
        'mem_total': mem.total / 1024**3,
        'disk_list': disks,
    }


# ═══════════════════════════════════════════════════════════════
#  图片生成
# ═══════════════════════════════════════════════════════════════

def _draw_bar(draw, x, y, w, h, pct, color=ACCENT_COLOR):
    draw.rectangle([x, y, x + w, y + h], fill=BAR_BG)
    if pct > 0:
        draw.rectangle([x, y, x + int(w * pct / 100), y + h], fill=color)


def _text_h(font, text):
    bbox = font.getbbox(text)
    return bbox[3] - bbox[1]


def generate_status_image(data: dict) -> Image.Image:
    fp = _find_font()
    try:
        tf = ImageFont.truetype(fp, 36) if fp else ImageFont.load_default()
        lf = ImageFont.truetype(fp, 28) if fp else ImageFont.load_default()
        df = ImageFont.truetype(fp, 24) if fp else ImageFont.load_default()
    except Exception:
        tf = lf = df = ImageFont.load_default()

    SP = (SP_TITLE, SP_OS, SP_TIME, SP_CPU_TEXT, SP_CPU_BAR,
          SP_MEM_TEXT, SP_MEM_BAR, SP_DISK_TITLE, SP_DISK_LABEL,
          SP_DISK_BAR, SP_DISK_DETAIL)
    lm, pw, ph = 30, 480, 24

    # 高度预计算
    y = PAD_TOP
    y += _text_h(tf, "系统状态") + SP[0]
    y += _text_h(lf, f"系统：{data['os_name']}") + SP[1]
    y += _text_h(lf, f"时间：{data['time']}") + SP[2]
    y += _text_h(lf, f"CPU：{data['cpu_percent']:.1f}%") + SP[3]
    y += ph + SP[4]
    mem = f"内存：{data['mem_percent']:.1f}% ({data['mem_used']:.1f}/{data['mem_total']:.1f}GB)"
    y += _text_h(lf, mem) + SP[5]
    y += ph + SP[6]
    y += _text_h(lf, "磁盘") + SP[7]
    dph = ph - 4
    dcount = 0
    for dev, mnt, pct, used, total in data['disk_list']:
        y += _text_h(df, f"{mnt} ({dev}) {pct:.1f}%") + SP[8]
        y += dph + SP[9]
        y += _text_h(df, f"{used:.1f}GB/{total:.1f}GB") + SP[10]
        dcount += 1
        if y > MAX_HEIGHT - PAD_BOTTOM - 40:
            break

    th = max(y + PAD_BOTTOM, MIN_HEIGHT)
    img = Image.new('RGB', (WIDTH, th), BG_COLOR)

    cfg = _read_config()
    if cfg.get('use_background') and os.path.exists(BG_IMAGE_PATH):
        try:
            bg = Image.open(BG_IMAGE_PATH).convert('RGB')
            if bg.size[0] < WIDTH or bg.size[1] < th:
                bg = bg.resize((WIDTH, th), Image.LANCZOS)
            else:
                left = (bg.size[0] - WIDTH) // 2
                top = (bg.size[1] - th) // 2
                bg = bg.crop((left, top, left + WIDTH, top + th))
            img.paste(bg, (0, 0))
        except Exception:
            pass

    draw = ImageDraw.Draw(img)
    y = PAD_TOP

    def dt(text, font, sp):
        nonlocal y
        draw.text((lm, y), text, font=font, fill=TEXT_COLOR, anchor='la')
        y += _text_h(font, text) + sp

    dt("系统状态", tf, SP[0])
    dt(f"系统：{data['os_name']}", lf, SP[1])
    dt(f"时间：{data['time']}", lf, SP[2])
    dt(f"CPU：{data['cpu_percent']:.1f}%", lf, SP[3])
    _draw_bar(draw, lm, y, pw, ph, data['cpu_percent'])
    y += ph + SP[4]
    dt(mem, lf, SP[5])
    _draw_bar(draw, lm, y, pw, ph, data['mem_percent'])
    y += ph + SP[6]
    dt("磁盘", lf, SP[7])
    for dev, mnt, pct, used, total in data['disk_list'][:dcount]:
        dt(f"{mnt} ({dev}) {pct:.1f}%", df, SP[8])
        _draw_bar(draw, lm, y, pw - 20, dph, pct, DISK_COLOR)
        y += dph + SP[9]
        dt(f"{used:.1f}GB/{total:.1f}GB", df, SP[10])
    hidden = len(data['disk_list']) - dcount
    if hidden > 0:
        draw.text((lm, y + 2), f"...还有 {hidden} 个磁盘未显示",
                  font=df, fill=(150, 150, 150), anchor='la')

    return img


def get_status_image_bytes(data: Optional[dict] = None) -> bytes:
    """生成系统状态图片并返回 PNG bytes（不落盘、不外链）"""
    if data is None:
        data = collect_system_data()
    img = generate_status_image(data)
    buf = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def generate_status_text(data: dict) -> str:
    def bar(p, l=10):
        f = int(round(p / 100 * l))
        return '█' * f + '░' * (l - f)

    lines = [
        "📊 系统状态",
        f"系统：{data['os_name']}",
        f"时间：{data['time']}",
        "",
        f"CPU：{data['cpu_percent']:.1f}% {bar(data['cpu_percent'])}",
        f"内存：{data['mem_percent']:.1f}% {bar(data['mem_percent'])} "
        f"({data['mem_used']:.1f}/{data['mem_total']:.1f}GB)",
        "",
        "💾 磁盘：",
    ]
    for dev, mnt, pct, used, total in data['disk_list']:
        lines.append(f"  {mnt} ({dev}) {pct:.1f}% {bar(pct)} "
                     f"{used:.1f}/{total:.1f}GB")
    return '\n'.join(lines)


def generate_status_md(data: dict) -> str:
    def bar(p, l=10):
        f = int(round(p / 100 * l))
        return '█' * f + '░' * (l - f)

    cpu = f"> **CPU**：{data['cpu_percent']:.1f}%\n> {bar(data['cpu_percent'])}"
    mem = (f"> **内存**：{data['mem_percent']:.1f}%\n"
           f"> {bar(data['mem_percent'])}\n"
           f"> （{data['mem_used']:.1f}/{data['mem_total']:.1f}GB）")
    disks = []
    for dev, mnt, pct, used, total in data['disk_list']:
        disks.append(f"> **{dev}** ({mnt})：{pct:.1f}%\n"
                     f"> {bar(pct)}\n"
                     f"> （{used:.1f}/{total:.1f}GB）")
    return (f"# 📊 系统状态\n\n"
            f"> **{data['os_name']}** | {data['time']}\n\n"
            f"{cpu}\n\n{mem}\n\n## 💾 磁盘\n"
            f"{chr(10).join(disks) if disks else '> 无'}")