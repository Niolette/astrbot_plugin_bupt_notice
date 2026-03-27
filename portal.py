"""
信息门户通知爬取模块
支持通过 WebVPN 访问北邮信息门户和通知公告系统
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from astrbot.api import logger

from .webvpn import encode_webvpn_url, cookies_to_httpx, load_cookies

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SEEN_FILE = os.path.join(DATA_DIR, "seen_notices.json")

# 北邮常用通知源（使用 JSON API 端点 get-list.html）
NOTICE_SOURCES = {
    "webapp_tzgg": {
        "name": "通知公告",
        "url": "https://webapp.bupt.edu.cn/extensions/wap/news/get-list.html?p=1&type=tzgg",
        "type": "tzgg",
    },
    "webapp_xnxw": {
        "name": "校内新闻",
        "url": "https://webapp.bupt.edu.cn/extensions/wap/news/get-list.html?p=1&type=xnxw",
        "type": "xnxw",
    },
    "my_portal": {
        "name": "信息门户",
        "url": "https://my.bupt.edu.cn/",
        "type": None,
    },
}


@dataclass
class Notice:
    id: str
    title: str
    source: str
    url: str
    date: str = ""
    content: str = ""
    author: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Notice":
        return Notice(**d)

    def digest(self) -> str:
        return hashlib.md5(f"{self.source}:{self.id}:{self.title}".encode()).hexdigest()


def _load_seen() -> set[str]:
    """加载已推送的通知 ID"""
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(seen: set[str]):
    """保存已推送的通知 ID"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(seen), f, ensure_ascii=False)


def mark_seen(notices: list[Notice]):
    """标记通知为已推送"""
    seen = _load_seen()
    for n in notices:
        seen.add(n.digest())
    _save_seen(seen)


async def fetch_notices(
    portal_url: str | None = None,
    max_count: int = 10,
) -> list[Notice]:
    """
    获取新通知（未推送过的）

    Args:
        portal_url: 自定义门户 URL，为 None 则使用默认源
        max_count: 最多返回条数

    Returns:
        新通知列表
    """
    cookies_list = load_cookies()
    if not cookies_list:
        logger.warning("无保存的 cookies，请先登录")
        return []

    cookie_dict = cookies_to_httpx(cookies_list)
    seen = _load_seen()

    all_notices: list[Notice] = []

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        follow_redirects=True,
        verify=False,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
    ) as client:
        # 爬取 webapp 通知公告
        for source_key, source_info in NOTICE_SOURCES.items():
            if source_key == "my_portal":
                continue  # 信息门户单独处理
            try:
                notices = await _fetch_webapp_notices(
                    client, source_info["url"], source_info["name"]
                )
                all_notices.extend(notices)
            except Exception as e:
                logger.error(f"获取 {source_info['name']} 失败: {e}")

        # 如果指定了自定义 URL
        if portal_url and portal_url not in [s["url"] for s in NOTICE_SOURCES.values()]:
            try:
                notices = await _fetch_generic_page(client, portal_url, "自定义源")
                all_notices.extend(notices)
            except Exception as e:
                logger.error(f"获取自定义源失败: {e}")

        # 尝试信息门户
        try:
            notices = await _fetch_my_portal(client)
            all_notices.extend(notices)
        except Exception as e:
            logger.debug(f"获取信息门户失败（可能需要额外认证）: {e}")

    # 过滤已推送的
    new_notices = [n for n in all_notices if n.digest() not in seen]

    return new_notices[:max_count]


async def fetch_latest_notice() -> Notice | None:
    """
    获取最新的一条校内通知（不过滤已推送）

    Returns:
        最新通知，如果获取失败则返回 None
    """
    cookies_list = load_cookies()
    if not cookies_list:
        logger.warning("无保存的 cookies，请先登录")
        return None

    cookie_dict = cookies_to_httpx(cookies_list)

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        follow_redirects=True,
        verify=False,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        }
    ) as client:
        # 从通知公告获取
        try:
            notices = await _fetch_webapp_notices(
                client,
                NOTICE_SOURCES["webapp_tzgg"]["url"],
                NOTICE_SOURCES["webapp_tzgg"]["name"],
            )
            if notices:
                return notices[0]
        except Exception as e:
            logger.error(f"获取最新通知失败: {e}")

    return None


async def _fetch_webapp_notices(
    client: httpx.AsyncClient, url: str, source_name: str
) -> list[Notice]:
    """通过 JSON API (get-list.html) 获取 webapp 通知"""
    vpn_url = encode_webvpn_url(url)
    logger.info(f"正在访问 {source_name}: {vpn_url}")

    resp = await client.get(vpn_url)
    logger.info(f"{source_name} 响应: status={resp.status_code}, content-type={resp.headers.get('content-type', 'unknown')}, body_len={len(resp.text)}")

    if resp.status_code != 200:
        logger.warning(f"访问 {source_name} 返回 {resp.status_code}")
        # 如果返回登录页，可能 session 失效
        if "扫码登录" in resp.text or "do-login" in resp.text:
            logger.warning("检测到未授权，可能需要重新登录")
        return []

    notices = []
    resp_text = resp.text

    # 记录响应前 500 字符用于调试
    logger.debug(f"{source_name} 响应内容前500字符: {resp_text[:500]}")

    # 尝试解析 JSON
    try:
        result = resp.json()
        logger.info(f"{source_name} JSON 解析成功，顶层键: {list(result.keys()) if isinstance(result, dict) else type(result).__name__}")

        # 情况1: {"data": {"tzgg": [...], ...}} 或 {"data": [...]}
        data = result if isinstance(result, list) else result.get("data", result)

        items_list = []
        if isinstance(data, dict):
            # data 是字典，遍历所有值找列表
            for key, val in data.items():
                logger.debug(f"  data['{key}'] type={type(val).__name__}, len={len(val) if isinstance(val, (list, dict, str)) else 'N/A'}")
                if isinstance(val, list):
                    items_list.extend(val)
        elif isinstance(data, list):
            items_list = data

        logger.info(f"{source_name} 共找到 {len(items_list)} 条原始数据")

        for item in items_list:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", ""))
            title = item.get("title", "")
            if not title:
                continue

            content = item.get("text", "") or item.get("desc", "") or item.get("content", "")
            created = item.get("created", 0)
            author = item.get("author", "") or item.get("source", "")

            # 时间戳转日期
            date_str = ""
            if created:
                try:
                    ts = int(created)
                    # 如果是毫秒时间戳
                    if ts > 1e12:
                        ts = ts // 1000
                    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    date_str = str(created)

            # 提取 type 参数
            classify_id = "tzgg"
            if "type=" in url:
                classify_id = url.split("type=")[-1].split("&")[0]

            detail_url = f"https://webapp.bupt.edu.cn/extensions/wap/news/detail.html?id={item_id}&classify_id={classify_id}"

            notices.append(Notice(
                id=item_id,
                title=title,
                source=source_name,
                url=detail_url,
                date=date_str,
                content=content,
                author=author,
            ))

        logger.info(f"{source_name} 解析完成，获取 {len(notices)} 条通知")
        return notices

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"{source_name} JSON 解析失败: {e}")

    # JSON 失败，尝试 HTML 解析
    logger.info(f"{source_name} 尝试 HTML 回退解析")
    soup = BeautifulSoup(resp_text, "lxml")
    notices = _parse_notice_page(soup, source_name, url)
    logger.info(f"{source_name} HTML 解析获取 {len(notices)} 条通知")
    return notices


async def _fetch_my_portal(client: httpx.AsyncClient) -> list[Notice]:
    """爬取信息门户消息"""
    portal_url = NOTICE_SOURCES["my_portal"]["url"]
    vpn_url = encode_webvpn_url(portal_url)

    resp = await client.get(vpn_url)
    if resp.status_code != 200:
        return []

    # 信息门户可能有多种格式，尝试解析
    html = resp.text
    soup = BeautifulSoup(html, "lxml")

    notices = []

    # 尝试查找常见的通知元素
    for selector in [
        ".notice-list li", ".msg-list li", ".news-list li",
        "table.notice tr", ".todo-list li",
        "[class*='notice'] li", "[class*='msg'] li",
        ".list-item", ".news-item", ".notice-item",
    ]:
        items = soup.select(selector)
        if items:
            for i, item in enumerate(items):
                link = item.select_one("a")
                if not link:
                    continue
                title = link.get_text(strip=True)
                href = link.get("href", "")
                if not title:
                    continue

                date_el = item.select_one(
                    ".date, .time, [class*='date'], [class*='time'], span:last-child"
                )
                date_str = date_el.get_text(strip=True) if date_el else ""

                notices.append(Notice(
                    id=href or f"portal_{i}",
                    title=title,
                    source="信息门户",
                    url=href,
                    date=date_str,
                ))
            break

    return notices


async def _fetch_generic_page(
    client: httpx.AsyncClient, url: str, source_name: str
) -> list[Notice]:
    """通用页面爬取"""
    vpn_url = encode_webvpn_url(url)
    resp = await client.get(vpn_url)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    return _parse_notice_page(soup, source_name, url)


def _parse_notice_page(soup: BeautifulSoup, source_name: str, base_url: str) -> list[Notice]:
    """通用通知页面解析"""
    notices = []

    # 策略1: 查找列表项
    selectors = [
        "ul li a", "ol li a",
        ".list a", ".news a", ".notice a",
        "table tr td a",
        ".item a", "[class*='list'] a",
    ]

    found_links = []
    for selector in selectors:
        links = soup.select(selector)
        if len(links) >= 3:  # 至少3条才认为是通知列表
            found_links = links
            break

    if not found_links:
        # 策略2: 查找所有合理的链接
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if len(text) > 5 and len(text) < 200:
                found_links.append(a)

    for a in found_links:
        title = a.get_text(strip=True)
        href = a.get("href", "")
        if not title or len(title) < 3:
            continue
        # 跳过导航链接
        if title in ("首页", "返回", "更多", "下一页", "上一页"):
            continue

        # 尝试获取日期
        parent = a.parent
        date_str = ""
        if parent:
            date_el = parent.select_one(
                ".date, .time, [class*='date'], [class*='time'], em, span"
            )
            if date_el and date_el != a:
                date_text = date_el.get_text(strip=True)
                if re.match(r'[\d\-/\.]+', date_text):
                    date_str = date_text

        notices.append(Notice(
            id=href or hashlib.md5(title.encode()).hexdigest()[:12],
            title=title,
            source=source_name,
            url=href,
            date=date_str,
        ))

    return notices


async def fetch_notice_detail(notice: Notice) -> str:
    """获取通知详情内容"""
    if not notice.url:
        return ""

    cookies_list = load_cookies()
    if not cookies_list:
        return ""

    cookie_dict = cookies_to_httpx(cookies_list)

    # 补全 URL
    url = notice.url
    if url.startswith("/"):
        url = "https://webapp.bupt.edu.cn" + url

    if not url.startswith("http"):
        return ""

    vpn_url = encode_webvpn_url(url)

    try:
        async with httpx.AsyncClient(
            cookies=cookie_dict,
            follow_redirects=True,
            verify=False,
            timeout=30,
        ) as client:
            resp = await client.get(vpn_url)
            if resp.status_code != 200:
                return ""

            soup = BeautifulSoup(resp.text, "lxml")

            # 查找正文内容
            for selector in [
                ".content", ".article-content", ".detail-content",
                ".news-content", "#content", ".text-content",
                "article", ".entry-content",
            ]:
                content_el = soup.select_one(selector)
                if content_el:
                    return content_el.get_text(separator="\n", strip=True)

            # Fallback: 取 body 文本
            body = soup.select_one("body")
            if body:
                text = body.get_text(separator="\n", strip=True)
                # 限制长度
                return text[:2000]

    except Exception as e:
        logger.error(f"获取通知详情失败: {e}")

    return ""
