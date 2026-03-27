"""
WebVPN URL 编码/解码工具 + 会话管理
北邮 WebVPN 使用 AES-CFB 加密主机名
"""
import binascii
import json
import os
from urllib.parse import urlparse, urlencode, parse_qs

from Crypto.Cipher import AES

WEBVPN_BASE = "https://webvpn.bupt.edu.cn"
AES_KEY = b'wrdvpnisthebest!'
AES_IV = b'wrdvpnisthebest!'
KEY_HEX = binascii.hexlify(AES_KEY).decode('utf-8')


def _encrypt_host(host: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_CFB, AES_IV, segment_size=128)
    encrypted = cipher.encrypt(host.encode('utf-8'))
    return binascii.hexlify(encrypted).decode('utf-8')


def _decrypt_host(hex_str: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_CFB, AES_IV, segment_size=128)
    decrypted = cipher.decrypt(binascii.unhexlify(hex_str))
    return decrypted.decode('utf-8')


def encode_webvpn_url(url: str) -> str:
    """将校内 URL 编码为 WebVPN URL"""
    parsed = urlparse(url)
    protocol = parsed.scheme
    host = parsed.hostname
    port = parsed.port
    path = parsed.path

    encrypted_host = _encrypt_host(host)

    if port and (
        (protocol == 'https' and port != 443) or
        (protocol == 'http' and port != 80)
    ):
        vpn_url = f"{WEBVPN_BASE}/{protocol}-{port}/{KEY_HEX}{encrypted_host}{path}"
    else:
        vpn_url = f"{WEBVPN_BASE}/{protocol}/{KEY_HEX}{encrypted_host}{path}"

    if parsed.query:
        vpn_url += f"?{parsed.query}"

    return vpn_url


def decode_webvpn_url(vpn_url: str) -> str:
    """将 WebVPN URL 解码为原始校内 URL"""
    parsed = urlparse(vpn_url)
    path_parts = parsed.path.strip('/').split('/', 2)

    if len(path_parts) < 2:
        return vpn_url

    proto_part = path_parts[0]
    host_part = path_parts[1]
    rest_path = '/' + path_parts[2] if len(path_parts) > 2 else '/'

    # 解析协议和端口
    if '-' in proto_part:
        protocol, port_str = proto_part.rsplit('-', 1)
        port = int(port_str)
    else:
        protocol = proto_part
        port = None

    # 去掉固定 KEY_HEX 前缀，解密主机名
    encrypted_hex = host_part[len(KEY_HEX):]
    host = _decrypt_host(encrypted_hex)

    if port:
        origin_url = f"{protocol}://{host}:{port}{rest_path}"
    else:
        origin_url = f"{protocol}://{host}{rest_path}"

    if parsed.query:
        origin_url += f"?{parsed.query}"

    return origin_url


COOKIE_FILE = os.path.join(os.path.dirname(__file__), "data", "cookies.json")


def save_cookies(cookies: list[dict]):
    """保存 cookies 到本地文件"""
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def load_cookies() -> list[dict] | None:
    """从本地文件加载 cookies"""
    if not os.path.exists(COOKIE_FILE):
        return None
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def cookies_to_httpx(cookies: list[dict]) -> "httpx.Cookies":
    """
    将 Playwright cookies 转换为 httpx.Cookies，保留 domain 和 path 作用域。
    这样同名 cookie（如 JSESSIONID）在不同 path 下可以共存且正确分发。
    """
    import httpx as _httpx

    jar = _httpx.Cookies()
    for c in cookies:
        domain = c.get('domain', '')
        if 'bupt.edu.cn' not in domain:
            continue
        # httpx 的 domain 不需要前导点
        domain = domain.lstrip('.')
        jar.set(
            c['name'],
            c['value'],
            domain=domain,
            path=c.get('path', '/'),
        )
    return jar
