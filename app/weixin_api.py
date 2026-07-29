"""微信 API 客户端（纯文本模式）"""
import os, json, base64, random, logging
from typing import Optional
from dataclasses import dataclass, asdict
from enum import IntEnum
import httpx

log = logging.getLogger('Elaina.微信Bot')
CHANNEL_VERSION = "2.4.6"; ILINK_APP_ID = "bot"; ILINK_APP_CLIENT_VERSION = "132102"

class MessageType(IntEnum): NONE = 0; USER = 1; BOT = 2
class MessageItemType(IntEnum): NONE = 0; TEXT = 1
class MessageState(IntEnum): NEW = 0; GENERATING = 1; FINISH = 2

@dataclass
class TextItem: text: Optional[str] = None
@dataclass
class MessageItem: type: Optional[int] = None; text_item: Optional[TextItem] = None
@dataclass
class WeixinMessage: from_user_id: Optional[str] = None; to_user_id: Optional[str] = None; client_id: Optional[str] = None; message_type: Optional[int] = None; message_state: Optional[int] = None; item_list: Optional[list] = None; context_token: Optional[str] = None; run_id: Optional[str] = None
@dataclass
class GetUpdatesResp: ret: Optional[int] = None; msgs: Optional[list] = None; get_updates_buf: Optional[str] = None; sync_buf: Optional[str] = None

def _random_wechat_uin() -> str: return base64.b64encode(str(random.randint(0, 2**32 - 1)).encode()).decode()
def _clean_none(data):
    if isinstance(data, dict): return {k: _clean_none(v) for k, v in data.items() if v is not None}
    elif isinstance(data, list): return [_clean_none(item) for item in data]
    return data
def _dataclass_to_dict(obj): return _clean_none(asdict(obj))

class WeixinApiClient:
    def __init__(self, base_url: str, token: str, timeout_ms: int = 15_000):
        self.base_url = base_url.rstrip("/") if base_url else ""; self.token = token; self.timeout_ms = timeout_ms
        self._client = httpx.AsyncClient(timeout=timeout_ms / 1000)

    def _build_headers(self, body_str: str) -> dict:
        headers = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token", "Content-Length": str(len(body_str.encode("utf-8"))), "X-WECHAT-UIN": _random_wechat_uin(), "iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _api_post(self, endpoint: str, body: dict, timeout_ms: int) -> dict:
        body["base_info"] = {"channel_version": CHANNEL_VERSION}
        body_json = json.dumps(body, ensure_ascii=False)
        headers = self._build_headers(body_json)
        url = f"{self.base_url}/{endpoint}"
        try:
            resp = await self._client.post(url, content=body_json, headers=headers, timeout=timeout_ms / 1000)
            resp.raise_for_status(); return resp.json()
        except httpx.TimeoutException: return {}
        except httpx.HTTPStatusError as e: raise RuntimeError(f"{endpoint} HTTP {e.response.status_code}") from e

    async def get_updates(self, get_updates_buf: str = "", timeout_ms: int = 35_000) -> GetUpdatesResp:
        body = {"get_updates_buf": get_updates_buf}
        try:
            data = await self._api_post("ilink/bot/getupdates", body, timeout_ms)
            if not data: return GetUpdatesResp(ret=0, msgs=[], get_updates_buf=get_updates_buf)
            buf = data.get('get_updates_buf') or data.get('sync_buf') or ""
            raw_msgs = data.get('msgs', []); msgs = []
            for m in raw_msgs:
                if isinstance(m, dict):
                    kwargs = {k: v for k, v in m.items() if k in WeixinMessage.__dataclass_fields__}
                    msg = WeixinMessage(**kwargs)
                    if msg.item_list:
                        parsed = []
                        for item in msg.item_list:
                            if isinstance(item, dict):
                                ik = {k: v for k, v in item.items() if k in MessageItem.__dataclass_fields__}
                                if 'text_item' in item and isinstance(item['text_item'], dict): ik['text_item'] = TextItem(**item['text_item'])
                                parsed.append(MessageItem(**ik))
                        msg.item_list = parsed
                    msgs.append(msg)
            return GetUpdatesResp(ret=data.get('ret', 0), msgs=msgs, get_updates_buf=buf)
        except Exception as e: log.error(f"[API] get_updates: {e}"); return GetUpdatesResp(ret=0, msgs=[], get_updates_buf=get_updates_buf)

    async def send_message(self, msg: WeixinMessage):
        body = {"msg": _dataclass_to_dict(msg)}
        await self._api_post("ilink/bot/sendmessage", body, self.timeout_ms)

    async def close(self): await self._client.aclose()

class WeixinMessageSender:
    def __init__(self, client: WeixinApiClient): self.client = client
    async def send_text(self, to_user_id: str, text: str, context_token: Optional[str] = None) -> dict:
        import uuid
        msg = WeixinMessage(to_user_id=to_user_id, message_type=MessageType.BOT, message_state=MessageState.FINISH, context_token=context_token, client_id=f"wx-{uuid.uuid4()}", run_id=f"wx-run-{uuid.uuid4()}", item_list=[MessageItem(type=MessageItemType.TEXT, text_item=TextItem(text=text))])
        try: await self.client.send_message(msg); return {"ok": True}
        except Exception as e: return {"ok": False, "error": str(e)}
    async def close(self): await self.client.close()