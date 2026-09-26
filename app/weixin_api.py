"""微信 iLink API 客户端（文本 + 图片）

v2 关键改动：
  · get_updates 检测 ret == -14，抛 SessionExpiredError 让消息循环退出
  · send_image 的 mid_size 使用密文长度
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from enum import IntEnum
from typing import Optional
from urllib.parse import urlparse

import aiohttp

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
import ip_fallback  # noqa: E402

log = logging.getLogger('Elaina.微信Bot')

CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = "132102"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
SESSION_EXPIRED_RET = -14


class SessionExpiredError(RuntimeError):
    """iLink 会话过期，调用方应停止消息循环"""
    pass


class MessageType(IntEnum):
    NONE = 0; USER = 1; BOT = 2


class MessageItemType(IntEnum):
    NONE = 0; TEXT = 1; IMAGE = 2


class MessageState(IntEnum):
    NEW = 0; GENERATING = 1; FINISH = 2


@dataclass
class TextItem:
    text: Optional[str] = None


@dataclass
class ImageMedia:
    encrypt_query_param: Optional[str] = None
    aes_key: Optional[str] = None
    encrypt_type: Optional[int] = None


@dataclass
class ImageItem:
    media: Optional[ImageMedia] = None
    mid_size: Optional[int] = None


@dataclass
class MessageItem:
    type: Optional[int] = None
    text_item: Optional[TextItem] = None
    image_item: Optional[ImageItem] = None


@dataclass
class WeixinMessage:
    from_user_id: Optional[str] = None
    to_user_id: Optional[str] = None
    client_id: Optional[str] = None
    message_type: Optional[int] = None
    message_state: Optional[int] = None
    item_list: Optional[list] = None
    context_token: Optional[str] = None
    run_id: Optional[str] = None


@dataclass
class GetUpdatesResp:
    ret: Optional[int] = None
    msgs: Optional[list] = None
    get_updates_buf: Optional[str] = None
    sync_buf: Optional[str] = None


def _random_wechat_uin() -> str:
    return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()


def _clean_none(data):
    if isinstance(data, dict):
        return {k: _clean_none(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [_clean_none(x) for x in data]
    return data


def _dataclass_to_dict(obj):
    return _clean_none(asdict(obj))


def _aes_encrypt_pkcs7(data: bytes, key: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    return AES.new(key, AES.MODE_ECB).encrypt(pad(data, 16))


class WeixinApiClient:
    def __init__(self, base_url: str, token: str, timeout_ms: int = 15_000):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.token = token
        self.timeout_ms = timeout_ms
        self._host = ip_fallback.WECHAT_API_HOST
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_node: Optional[str] = None
        self._session_lock = asyncio.Lock()

    async def _ensure_session(self, candidate: Optional[str]):
        async with self._session_lock:
            if (self._session is not None
                    and not self._session.closed
                    and self._session_node == candidate):
                return self._session
            if self._session is not None and not self._session.closed:
                try:
                    await self._session.close()
                except Exception:
                    pass
            connector = ip_fallback.make_connector(candidate)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=120, connect=30, sock_read=60),
            )
            self._session_node = candidate
            log.info(f"[API] 创建持久 session（节点={candidate or 'DNS'}）")
            return self._session

    async def _reset_session(self):
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                try:
                    await self._session.close()
                except Exception:
                    pass
            self._session = None
            self._session_node = None

    def _build_headers(self, body_str: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Content-Length": str(len(body_str.encode("utf-8"))),
            "X-WECHAT-UIN": _random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _api_post(self, endpoint: str, body: dict,
                        timeout_ms: int) -> Optional[dict]:
        body["base_info"] = {"channel_version": CHANNEL_VERSION}
        body_json = json.dumps(body, ensure_ascii=False)
        headers = self._build_headers(body_json)
        url = f"{self.base_url}/{endpoint}"
        timeout_sec = timeout_ms / 1000

        candidates = ip_fallback.build_candidates(
            self._host, ip_fallback.WECHAT_API_FALLBACK_IPS)
        errors = []

        for candidate in candidates:
            label = candidate or "DNS"
            try:
                session = await self._ensure_session(candidate)
                req_timeout = aiohttp.ClientTimeout(total=timeout_sec)
                t0 = time.monotonic()
                async with session.post(url, data=body_json,
                                        headers=headers,
                                        timeout=req_timeout) as resp:
                    text = await resp.text()
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    if resp.status != 200:
                        errors.append(f"{label}: HTTP {resp.status}")
                        continue
                    try:
                        data = json.loads(text) if text else {}
                    except json.JSONDecodeError as e:
                        errors.append(f"{label}: 非 JSON: {e}")
                        continue
                    if ip_fallback.get_working_node(self._host) != candidate:
                        log.info(f"[API] 节点切换 → {label}")
                        ip_fallback.set_working_node(self._host, candidate)
                    if endpoint != "ilink/bot/getupdates":
                        log.debug(f"[API] {endpoint} {label} {elapsed_ms:.0f}ms")
                    return data
            except asyncio.TimeoutError:
                errors.append(f"{label}: 超时")
                await self._reset_session()
                continue
            except aiohttp.ClientError as e:
                errors.append(f"{label}: {type(e).__name__}")
                await self._reset_session()
                continue
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}: {e}")
                continue

        log.debug(f"[API] {endpoint} 所有节点失败: {' | '.join(errors)}")
        return None

    async def get_updates(self, get_updates_buf: str = "",
                          timeout_ms: int = 35_000) -> GetUpdatesResp:
        """长轮询。★ ret == -14 时抛 SessionExpiredError"""
        body = {"get_updates_buf": get_updates_buf}
        try:
            data = await self._api_post("ilink/bot/getupdates",
                                        body, timeout_ms + 5_000)
        except Exception as e:
            log.error(f"[API] get_updates 异常: {e}")
            return GetUpdatesResp(ret=0, msgs=[], get_updates_buf=get_updates_buf)

        if not data:
            return GetUpdatesResp(ret=0, msgs=[], get_updates_buf=get_updates_buf)

        # ★ 会话过期检测
        ret = data.get('ret')
        if ret == SESSION_EXPIRED_RET:
            raise SessionExpiredError(f"iLink 会话已过期（ret={ret}）")
        errmsg = str(data.get('errmsg', '') or data.get('msg', '') or '').lower()
        if 'session' in errmsg and ('expire' in errmsg or 'timeout' in errmsg):
            raise SessionExpiredError(f"iLink 会话已过期: {errmsg}")

        buf = data.get('get_updates_buf') or data.get('sync_buf') or ""
        raw_msgs = data.get('msgs', []) or []
        msgs = []
        for m in raw_msgs:
            if not isinstance(m, dict):
                continue
            kwargs = {k: v for k, v in m.items()
                      if k in WeixinMessage.__dataclass_fields__}
            msg = WeixinMessage(**kwargs)
            if msg.item_list:
                parsed = []
                for item in msg.item_list:
                    if isinstance(item, dict):
                        ik = {k: v for k, v in item.items()
                              if k in MessageItem.__dataclass_fields__}
                        if 'text_item' in item and isinstance(item['text_item'], dict):
                            ik['text_item'] = TextItem(**item['text_item'])
                        parsed.append(MessageItem(**ik))
                msg.item_list = parsed
            msgs.append(msg)
        return GetUpdatesResp(ret=ret or 0, msgs=msgs, get_updates_buf=buf)

    async def send_message(self, msg: WeixinMessage):
        body = {"msg": _dataclass_to_dict(msg)}
        await self._api_post("ilink/bot/sendmessage", body, self.timeout_ms)

    async def get_upload_url(self, filekey: str, to_user_id: str,
                             rawsize: int, rawfilemd5: str, filesize: int,
                             aeskey: str, media_type: int = 1,
                             no_need_thumb: bool = True) -> dict:
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": no_need_thumb,
            "aeskey": aeskey,
        }
        return await self._api_post("ilink/bot/getuploadurl",
                                     body, self.timeout_ms) or {}

    async def upload_to_cdn(self, upload_url: str, encrypted_data: bytes) -> str:
        parsed = urlparse(upload_url)
        path_and_query = parsed.path
        if parsed.query:
            path_and_query += "?" + parsed.query

        candidates = ip_fallback.build_candidates(
            ip_fallback.WECHAT_CDN_HOST,
            ip_fallback.WECHAT_CDN_FALLBACK_IPS)
        timeout = aiohttp.ClientTimeout(total=120, connect=30)
        errors = []

        for candidate in candidates:
            label = candidate or "DNS"
            try:
                if candidate is None:
                    session_url = upload_url
                    connector = None
                else:
                    session_url = (f"https://{ip_fallback.WECHAT_CDN_HOST}"
                                   f"{path_and_query}")
                    connector = ip_fallback.make_connector(candidate, limit=5)

                async with aiohttp.ClientSession(
                        connector=connector, timeout=timeout) as session:
                    async with session.post(
                            session_url,
                            data=encrypted_data,
                            headers={"Content-Type": "application/octet-stream"}
                    ) as resp:
                        resp.raise_for_status()
                        param = resp.headers.get("x-encrypted-param", "")
                        if param:
                            ip_fallback.set_working_node(
                                ip_fallback.WECHAT_CDN_HOST, candidate)
                            log.info(f"[CDN] 上传成功（{label}）")
                            return param
                        errors.append(f"{label}: 无 x-encrypted-param")
            except Exception as e:
                errors.append(f"{label}: {type(e).__name__}")
                continue

        raise RuntimeError("CDN 上传失败: " + " | ".join(errors))

    async def close(self):
        await self._reset_session()


class WeixinMessageSender:
    def __init__(self, client: WeixinApiClient):
        self.client = client

    async def send_text(self, to_user_id: str, text: str,
                        context_token: Optional[str] = None) -> dict:
        msg = WeixinMessage(
            to_user_id=to_user_id,
            message_type=MessageType.BOT,
            message_state=MessageState.FINISH,
            context_token=context_token,
            client_id=f"wx-{uuid.uuid4()}",
            run_id=f"wx-run-{uuid.uuid4()}",
            item_list=[MessageItem(type=MessageItemType.TEXT,
                                    text_item=TextItem(text=text))],
        )
        try:
            await self.client.send_message(msg)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def send_image(self, to_user_id: str, image_data: bytes,
                         context_token: Optional[str] = None) -> dict:
        if not image_data:
            return {"ok": False, "error": "空图片数据"}
        try:
            aes_key_hex = secrets.token_hex(16)
            aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()
            filekey = secrets.token_hex(16)
            raw_size = len(image_data)
            raw_md5 = hashlib.md5(image_data).hexdigest()
            key_bytes = bytes.fromhex(aes_key_hex)
            encrypted = _aes_encrypt_pkcs7(image_data, key_bytes)
            enc_size = len(encrypted)   # ★ 密文大小

            up = await self.client.get_upload_url(
                filekey=filekey,
                to_user_id=to_user_id,
                rawsize=raw_size,
                rawfilemd5=raw_md5,
                filesize=enc_size,
                aeskey=aes_key_hex,
                media_type=1,
                no_need_thumb=True,
            )
            upload_full_url = up.get("upload_full_url") or ""
            upload_param = up.get("upload_param") or ""
            if upload_full_url:
                cdn_url = upload_full_url
            elif upload_param:
                cdn_url = (f"{CDN_BASE_URL}/upload"
                           f"?encrypted_query_param={upload_param}&filekey={filekey}")
            else:
                return {"ok": False, "error": f"获取上传地址失败: {up}"}

            download_param = await self.client.upload_to_cdn(cdn_url, encrypted)
            if not download_param:
                return {"ok": False, "error": "CDN 未返回 x-encrypted-param"}

            msg = WeixinMessage(
                to_user_id=to_user_id,
                message_type=MessageType.BOT,
                message_state=MessageState.FINISH,
                context_token=context_token,
                client_id=f"wx-{uuid.uuid4()}",
                run_id=f"wx-run-{uuid.uuid4()}",
                item_list=[MessageItem(
                    type=MessageItemType.IMAGE,
                    image_item=ImageItem(
                        media=ImageMedia(
                            encrypt_query_param=download_param,
                            aes_key=aes_key_b64,
                            encrypt_type=1,
                        ),
                        mid_size=enc_size,   # ★ 密文大小
                    ),
                )],
            )
            await self.client.send_message(msg)
            return {"ok": True, "raw_size": raw_size, "enc_size": enc_size}
        except Exception as e:
            log.error(f"[图片发送] 失败: {e}")
            return {"ok": False, "error": str(e)}

    async def close(self):
        await self.client.close()