# 🤖 微信 Bot 插件 for ElainaBot

[![Version](https://img.shields.io/badge/version-2.7.0-blue)](https://github.com/yourname/elainabot-wechat-bot)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/)

在 ElainaBot QQ 框架中运行微信 Bot，实现微信消息的自动接收与回复。

## ✨ 功能特性

- 🔐 微信扫码登录
- 💬 微信消息自动接收与回复
- 📊 系统状态查询（CPU/内存/磁盘）
- 🖥️ Web 面板配置管理
- 📦 依赖自动安装
- 🎯 装饰器风格指令注册
- 🤖 多实例管理（启动/关闭/列表）
- 📈 DAU 日活统计

## 📁 目录结构

```

plugins/wechat_bot/
├── main.py                    # 主入口
├── app/
│   ├── init.py
│   ├── commands.py            # 指令注册（装饰器风格）
│   ├── system_status.py       # 系统状态模块
│   └── weixin_api.py          # 微信 API 客户端
├── Microsoft YaHei.ttf        # 字体文件
├── requirements.txt           # 依赖列表
└── data/
     ├── config.yaml            # 配置文件（自动生成）
     ├── background.png         # 开启背景图开关时，该图片必须存在
     ├── .weixin-token.json     # 登录凭证（自动生成）
     └── status.png             # 系统状态图（自动生成）

```

## 🚀 快速开始

### 1. 安装

将插件放入 ElainaBot 的 `plugins/` 目录：

```bash
cd ElainaBot/plugins
git clone https://github.com/yourname/elainabot-wechat-bot.git
```

### 2. 字体文件

将 Microsoft YaHei.ttf 放入插件根目录（用于系统状态图片生成）。

### 3. 启动插件

插件启动后会自动安装缺失依赖，无需手动操作。

## 📖 使用说明

### QQ 端指令（仅主人可用）

|指令|说明|
|-----|-----|
|微信登录 | 获取二维码登录微信|
|微信终止 | 终止正在进行的登录|
|微信登出 | 停止微信 `Bot`|
|微信重启 | 清除凭证并重新登录|
|微信状态 | 查看微信 `Bot` 运行状态|
|微信帮助 | 查看帮助信息|
|系统状态 | 查看服务器资源占用|

### 微信端指令

|指令|说明|
|-----|-----|
|系统状态 / status | 查看服务器资源占用|
|机器人列表 / bots | 查看所有机器人及状态|
|启动 appid | 启动指定机器人|
|关闭 appid | 关闭指定机器人|
|dau appid 日期| 查看日活统计|
|重启 | 重启机器人进程|
|帮助 / help | 查看可用指令|
|你好 / hello | 打招呼|

## Web 面板

在 ElainaBot Web 面板侧边栏点击「微信Bot 配置」：

- 查看微信 Bot 运行状态
- 启动 / 停止 / 重启微信 Bot
- 修改配置（API 地址、公网地址等）
- 切换图片/文本输出模式
- 自动登录开关

## ⚙️ 配置文件

data/config.yaml：

```yaml
wechat_base_url: "https://ilinkai.weixin.qq.com"  # 微信 API 地址
base_url: "http://127.0.0.1:5200"                   # QQ 端图片公网地址
use_image: true                                      # 图片输出模式
use_background: false                                # 自定义背景图
auto_login: false                                    # 启动时自动登录
```

## 🔧 添加新指令

#### 在 `app/commands.py` 中使用装饰器注册微信指令：

```python
from app.commands import WechatCommand

@WechatCommand('天气', 'weather')
async def cmd_weather(sender, to_user_id, context_token, text):
    """天气查询"""
    await sender.send_text(to_user_id, "今天天气晴，25°C", context_token)
```

支持精确匹配和前缀匹配：

```python
# 精确匹配：用户发送"帮助"触发
@WechatCommand('帮助', 'help')

# 前缀匹配：用户发送"启动 102917770"触发
@WechatCommand('启动', 'start', prefix=True)
async def cmd_start(sender, to_user_id, context_token, text):
    appid = text  # text = "102917770"
```

#### 在 `main.py` 中添加QQ指令:

```python
@handler(r'^图片$', name='图片', desc='发送网络图片示例', owner_only=True)
async def send_image(event, match):
    await event.reply_image(
        "https://i0.hdslb.com/bfs/openplatform/559162218f455ea859c783dceeda65cb1c724f4c.png",
        "reply_image 方法发送")
```

## 📦 依赖

```
aiohttp>=3.9.0
httpx>=0.27.0
qrcode>=7.4.0
Pillow>=10.0.0
psutil>=5.9.0
pyyaml>=6.0
```

插件启动时自动检查并安装，也可手动安装：

```bash
pip install -r plugins/wechat_bot/requirements.txt
```

❓ 常见问题

Q: 微信 Bot 启动后收不到消息？

A: 发送 微信状态 检查运行状态，如提示「未运行」则发送 微信登录 重新扫码。

Q: 微信登录二维码在哪看？

A: 发送 微信登录 后，二维码会通过 QQ 消息发送，同时服务器终端也会显示。

Q: 如何切换图片/文本模式？

A: 在 Web 面板「微信Bot 配置」中切换「图片输出」开关并保存。

Q: token 过期了怎么办？

A: 发送 微信重启 清除旧凭证并重新扫码登录。

Q: 如何关闭某个机器人？

A: 微信端发送 关闭 <appid>，例如 关闭 102111170。

📄 许可证

MIT License
