"""集中管理微信 API / CDN 的 IP fallback 与自定义 resolver

★ 所有 fallback IP 都在这里维护，一处修改，全局生效。
★ 当前部署在成都，DNS 直连可用，fallback 列表留空。
  若将来部署到无法直连的地区，在下方填入可用 IP 即可。
"""

import socket
from typing import Optional

import aiohttp


# ══════════════════════════════════════════════════════════════════════════
# 配置区 —— 只在这里改
# ══════════════════════════════════════════════════════════════════════════

# 微信 iLink API 域名
WECHAT_API_HOST = 'ilinkai.weixin.qq.com'
WECHAT_API_FALLBACK_IPS = [
    # 成都部署：DNS 直连可用，留空即可。
    # 若未来某天又不通，把华东可用 IP 填进来：
    # '180.101.242.203',
    # '101.227.131.211',
    # '61.151.230.245',
    # '117.89.176.78',
]

# 微信 CDN 域名
WECHAT_CDN_HOST = 'novac2c.cdn.weixin.qq.com'
WECHAT_CDN_FALLBACK_IPS = [
    # 成都部署：DNS 直连可用，留空即可。
]

# CDN 上传转发代理（直连 CDN 不通时才需要）
CDN_PROXY_URL = ''
CDN_PROXY_TOKEN = ''


# ══════════════════════════════════════════════════════════════════════════
# 进程内状态：每个 host 的"当前可用节点"
# ══════════════════════════════════════════════════════════════════════════

_working_nodes: dict = {}


def get_working_node(host: str) -> Optional[str]:
    return _working_nodes.get(host)


def set_working_node(host: str, node: Optional[str]):
    _working_nodes[host] = node


def build_candidates(host: str, fallback_ips: list) -> list:
    """构造候选列表：上次成功的 → fallback IP → DNS 兜底

    列表为空时，只返回 [None]，即使用系统 DNS。
    """
    working = _working_nodes.get(host)
    candidates: list = []
    if working is not None:
        candidates.append(working)
    for ip in fallback_ips:
        if ip != working:
            candidates.append(ip)
    candidates.append(None)   # None = 系统 DNS
    return candidates


# ══════════════════════════════════════════════════════════════════════════
# Resolver / Connector
# ══════════════════════════════════════════════════════════════════════════

class StaticResolver(aiohttp.abc.AbstractResolver):
    """把域名解析到固定 IP，TLS 握手仍用原域名做 SNI（支持 IPv4 / IPv6）"""

    def __init__(self, ip: str):
        self._ip = ip

    async def resolve(self, host, port=0, family=socket.AF_INET):
        fam = socket.AF_INET6 if ':' in self._ip else socket.AF_INET
        return [{
            'hostname': host,
            'host': self._ip,
            'port': port or 443,
            'family': fam,
            'proto': 0,
            'flags': 0,
        }]

    async def close(self):
        pass


def make_connector(candidate: Optional[str],
                   limit: int = 20,
                   keepalive: int = 60) -> aiohttp.TCPConnector:
    """根据候选节点创建 Connector；candidate 为 None 时使用系统 DNS"""
    if candidate is None:
        return aiohttp.TCPConnector(
            limit=limit, keepalive_timeout=keepalive, ttl_dns_cache=300)
    return aiohttp.TCPConnector(
        resolver=StaticResolver(candidate),
        limit=limit, keepalive_timeout=keepalive)