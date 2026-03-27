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

# 北邮常用通知源
NOTICE_SOURCES = {
    "my_tzgg": {
        "name": "校内通知",
        "url": "http://my.bupt.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1154",
    },
    "webapp_tzgg": {
        "name": "通知公告(webapp)",
        "url": "https://webapp.bupt.edu.cn/extensions/wap/news/get-list.html?p=1&type=tzgg",
        "type": "tzgg",
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
        # 爬取所有通知源
        for source_key, source_info in NOTICE_SOURCES.items():
            try:
                if source_key == "my_tzgg":
                    notices = await _fetch_my_bupt_notices(
                        client, source_info["url"], source_info["name"]
                    )
                else:
                    notices = await _fetch_webapp_notices(
                        client, source_info["url"], source_info["name"]
                    )
                all_notices.extend(notices)
            except Exception as e:
                logger.error(f"获取 {source_info['name']} 失败: {e}")

        # 如果指定了自定义 URL
        if portal_url and portal_url not in [s["url"] for s in NOTICE_SOURCES.values()]:
            try:
                notices = await _fetch_my_bupt_notices(client, portal_url, "自定义源")
                all_notices.extend(notices)
            except Exception as e:
                logger.error(f"获取自定义源失败: {e}")

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
        # 优先从信息门户校内通知获取
        try:
            notices = await _fetch_my_bupt_notices(
                client,
                NOTICE_SOURCES["my_tzgg"]["url"],
                NOTICE_SOURCES["my_tzgg"]["name"],
            )
            if notices:
                return notices[0]
        except Exception as e:
            logger.error(f"获取最新通知失败: {e}")

        # 回退到 webapp
        try:
            notices = await _fetch_webapp_notices(
                client,
                NOTICE_SOURCES["webapp_tzgg"]["url"],
                NOTICE_SOURCES["webapp_tzgg"]["name"],
            )
            if notices:
                return notices[0]
        except Exception as e:
            logger.error(f"获取 webapp 最新通知失败: {e}")

    return None


async def _fetch_my_bupt_notices(
    client: httpx.AsyncClient, url: str, source_name: str
) -> list[Notice]:
    """
    爬取 my.bupt.edu.cn 的 list.jsp 通知页面（服务端渲染 HTML）
    URL 格式: http://my.bupt.edu.cn/list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1154
    """
    vpn_url = encode_webvpn_url(url)
    logger.info(f"正在访问 {source_name}: {vpn_url}")

    resp = await client.get(vpn_url)
    logger.info(
        f"{source_name} 响应: status={resp.status_code}, "
        f"content-type={resp.headers.get('content-type', 'unknown')}, "
        f"body_len={len(resp.text)}"
    )

    if resp.status_code != 200:
        logger.warning(f"访问 {source_name} 返回 {resp.status_code}")
        if "扫码登录" in resp.text or "do-login" in resp.text:
            logger.warning("检测到未授权，可能需要重新登录")
        return []

    html = resp.text
    logger.debug(f"{source_name} 响应前800字符: {html[:800]}")

    soup = BeautifulSoup(html, "lxml")
    notices = []

    # my.bupt.edu.cn 的通知列表通常是 <li> 列表或 <table> 格式
    # 自适应多种布局

    # 策略1: 查找包含通知链接的列表项（最常见）
    # 典型结构: <li><a href="...">标题</a><span>日期</span></li>
    candidate_links = []

    # 尝试多种选择器
    for selector in [
        # 常见的通知列表选择器
        ".list-item a", ".news_list li a", ".notice_list li a",
        ".listleft li a", ".listright li a",
        "ul.list li a", "ul.news li a",
        # 通用 CMS 列表
        ".column-news-list li a", ".wp_article_list li a",
        "div.list ul li a", "div.content ul li a",
        # 更宽泛的选择器
        "li a[href*='view']", "li a[href*='detail']",
        "li a[href*='urltype']", "li a[href*='content']",
    ]:
        links = soup.select(selector)
        if links and len(links) >= 2:
            candidate_links = links
            logger.info(f"{source_name} 选择器 '{selector}' 匹配到 {len(links)} 条")
            break

    # 策略2: 如果没找到，查找所有 <li> 下的 <a>
    if not candidate_links:
        all_lis = soup.find_all("li")
        for li in all_lis:
            a = li.find("a", href=True)
            if a:
                text = a.get_text(strip=True)
                if text and 5 < len(text) < 200:
                    candidate_links.append(a)
        if candidate_links:
            logger.info(f"{source_name} 从所有 <li><a> 中找到 {len(candidate_links)} 条")

    # 策略3: 查找表格中的链接
    if not candidate_links:
        for a in soup.select("table a[href]"):
            text = a.get_text(strip=True)
            if text and 5 < len(text) < 200:
                candidate_links.append(a)
        if candidate_links:
            logger.info(f"{source_name} 从 <table><a> 中找到 {len(candidate_links)} 条")

    # 策略4: 最宽泛 - 页面所有合理链接
    if not candidate_links:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            # 过滤掉导航和无关链接
            if not text or len(text) < 5 or len(text) > 200:
                continue
            if text in ("首页", "返回", "更多", "下一页", "上一页", "登录"):
                continue
            if any(x in href for x in ["javascript:", "#", "login", "logout"]):
                continue
            candidate_links.append(a)
        if candidate_links:
            logger.info(f"{source_name} 从所有 <a> 中找到 {len(candidate_links)} 条")

    if not candidate_links:
        logger.warning(f"{source_name} 未找到任何通知链接。页面标题: {soup.title.string if soup.title else 'N/A'}")
        # 输出页面结构帮助诊断
        body = soup.find("body")
        if body:
            tags = [tag.name for tag in body.find_all(recursive=False)]
            logger.debug(f"{source_name} body 直接子元素: {tags[:20]}")
        return []

    for a in candidate_links:
        title = a.get_text(strip=True)
        href = a.get("href", "")

        if not title or len(title) < 3:
            continue

        # 补全 URL
        full_url = href
        if href.startswith("/"):
            full_url = "http://my.bupt.edu.cn" + href
        elif not href.startswith("http"):
            full_url = "http://my.bupt.edu.cn/" + href

        # 获取日期（通常在同级或父级的 span/td 中）
        date_str = _extract_date_near(a)

        notices.append(Notice(
            id=href or hashlib.md5(title.encode()).hexdigest()[:12],
            title=title,
            source=source_name,
            url=full_url,
            date=date_str,
        ))

    logger.info(f"{source_name} 解析完成，获取 {len(notices)} 条通知")
    return notices


def _extract_date_near(element) -> str:
    """尝试从元素附近提取日期文本"""
    date_pattern = re.compile(r'\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}')

    # 检查同级兄弟元素
    for sibling in element.next_siblings:
        if hasattr(sibling, 'get_text'):
            text = sibling.get_text(strip=True)
            match = date_pattern.search(text)
            if match:
                return match.group()

    # 检查父元素中的其他文本
    parent = element.parent
    if parent:
        # 查找 span/td/em 等日期容器
        for tag in parent.find_all(['span', 'td', 'em', 'time', 'div']):
            if tag != element and tag not in element.parents:
                text = tag.get_text(strip=True)
                match = date_pattern.search(text)
                if match:
                    return match.group()

        # 检查父元素的纯文本
        text = parent.get_text(strip=True)
        match = date_pattern.search(text)
        if match:
            return match.group()

    return ""


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
        # 根据 source 和 URL 特征判断域名
        if "my.bupt.edu.cn" in (notice.url or "") or notice.source == "校内通知":
            url = "http://my.bupt.edu.cn" + url
        else:
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
                # my.bupt.edu.cn 的 CMS 选择器
                "#vsb_content", ".v_news_content", ".wp_articlecontent",
                ".winstyle_articlecontent", "#articleContent",
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
