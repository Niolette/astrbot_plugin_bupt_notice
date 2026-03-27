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

from .webvpn import encode_webvpn_url, cookies_to_httpx, load_cookies, save_cookies

# CAS 认证凭据，由 main.py 初始化时设置
_cas_credentials: dict[str, str] = {}

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


# ==================== CAS 认证 ====================

def set_cas_credentials(username: str, password: str):
    """由 main.py 初始化时调用，设置 CAS 凭据"""
    _cas_credentials["username"] = username
    _cas_credentials["password"] = password


def _is_cas_login_page(html: str) -> bool:
    """检测 HTML 是否为 CAS 登录页"""
    return (
        "<title>CAS Login" in html
        or "<title>CAS – Central Authentication" in html
        or 'id="cas-login"' in html
        or 'name="execution"' in html and 'name="lt"' in html
    )


async def _authenticate_cas(client: httpx.AsyncClient, cas_resp: httpx.Response) -> bool:
    """
    对 CAS 登录页执行表单登录（标准 Apereo CAS 协议）。
    1. 从 CAS 登录页 HTML 提取隐藏字段 (lt, execution, _eventId)
    2. POST 用户名/密码到 CAS 登录端点
    3. 跟随重定向链完成 CAS SSO（设置 CASTGC → service ticket → 回调）
    4. 将新 cookie 持久化保存
    """
    username = _cas_credentials.get("username", "")
    password = _cas_credentials.get("password", "")

    if not username or not password:
        logger.warning("CAS 凭据未配置（cas_username / cas_password）")
        return False

    html = cas_resp.text
    cas_login_url = str(cas_resp.url)  # WebVPN 代理后的 CAS URL

    soup = BeautifulSoup(html, "lxml")

    # 提取隐藏表单字段
    form = soup.select_one('form#fm1, form#loginForm, form[action*="login"]')
    if not form:
        # 回退：查找任何包含 execution 字段的表单
        form = soup.find("form")

    if not form:
        logger.error("CAS 登录页未找到表单")
        logger.debug(f"CAS 页面前 500 字符: {html[:500]}")
        return False

    # 提取所有隐藏字段
    form_data = {}
    for inp in form.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            form_data[name] = value

    # 诊断：如果隐藏字段很少，尝试提取所有 input
    if len(form_data) < 2:
        logger.info("CAS 隐藏字段不足，尝试提取所有 input 字段...")
        all_inputs = form.find_all("input")
        for inp in all_inputs:
            logger.debug(
                f"  input: name={inp.get('name')}, type={inp.get('type')}, "
                f"id={inp.get('id')}, value={inp.get('value', '')[:50]}"
            )
            # 收集非用户输入的字段（已有的不覆盖）
            name = inp.get("name")
            inp_type = (inp.get("type") or "").lower()
            if name and name not in form_data and inp_type not in ("text", "password", "submit", "button"):
                form_data[name] = inp.get("value", "")

    # 检查页面中是否有 JS 生成的关键字段
    # 北邮 CAS 可能把 execution 放在 <script> 或 <p> 中
    if "execution" not in form_data:
        # 尝试从整个 HTML 中查找
        exec_input = soup.find("input", attrs={"name": "execution"})
        if exec_input:
            form_data["execution"] = exec_input.get("value", "")
            logger.info(f"CAS execution 字段从表单外找到")
        else:
            # 尝试正则匹配
            import re as _re
            exec_match = _re.search(r'name=["\']execution["\']\s+value=["\']([^"\']+)["\']', html)
            if exec_match:
                form_data["execution"] = exec_match.group(1)
                logger.info(f"CAS execution 字段通过正则找到")

    if "lt" not in form_data:
        lt_input = soup.find("input", attrs={"name": "lt"})
        if lt_input:
            form_data["lt"] = lt_input.get("value", "")

    form_data["username"] = username
    form_data["password"] = password
    if "_eventId" not in form_data:
        form_data["_eventId"] = "submit"

    logger.info(f"CAS 表单字段 (key 列表): {sorted(k for k in form_data if k != 'password')}")

    # 确定 POST 目标 URL
    action = form.get("action", "")
    if action:
        if action.startswith("http"):
            post_url = action
        elif action.startswith("/"):
            # 相对路径，拼接 WebVPN 代理的 base URL
            from urllib.parse import urlparse
            parsed = urlparse(cas_login_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            # 对于 WebVPN 代理的路径，需要保留 /https/xxx 前缀
            path_parts = parsed.path.split("/")
            # WebVPN 路径格式: /https/[encrypted_host]/authserver/login
            # 保留到 /https/[encrypted_host] 部分
            if len(path_parts) >= 3:
                vpn_prefix = "/".join(path_parts[:3])
                post_url = f"{base}{vpn_prefix}{action}"
            else:
                post_url = f"{base}{action}"
        else:
            post_url = cas_login_url
    else:
        post_url = cas_login_url

    logger.info(
        f"CAS 表单登录: POST {post_url}, "
        f"字段: {[k for k in form_data if k != 'password']}"
    )

    try:
        resp = await client.post(
            post_url,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": cas_login_url,
            },
        )

        logger.info(
            f"CAS POST 响应: status={resp.status_code}, "
            f"final_url={resp.url}, body_len={len(resp.text)}"
        )

        # 检查是否仍在 CAS 登录页（认证失败）
        if _is_cas_login_page(resp.text):
            # 检查错误信息 —— 多种选择器
            error_soup = BeautifulSoup(resp.text, "lxml")
            error_el = error_soup.select_one(
                ".errors, .alert-danger, #msg, .login-error, "
                '[class*="error"], [class*="Error"], '
                '#errorShow, .error-text, .cas-error, '
                '.login-tips, #showErrorTip, #errorTip'
            )
            error_msg = error_el.get_text(strip=True) if error_el else ""
            if not error_msg:
                # 尝试从 span/div 内容中查找错误文本
                for tag in error_soup.find_all(['span', 'div', 'p']):
                    text = tag.get_text(strip=True)
                    if any(kw in text for kw in [
                        '错误', '失败', '误', 'error', 'Error',
                        '密码', '用户名', '无效', '锁定', '禁用',
                        'invalid', 'Invalid', 'incorrect', 'locked'
                    ]):
                        error_msg = text
                        break
            if not error_msg:
                error_msg = "未知错误"
                # 输出页面中的所有文本帮助诊断
                body = error_soup.find("body")
                if body:
                    body_text = body.get_text(separator=' | ', strip=True)[:500]
                    logger.error(f"CAS 登录页 body 文本: {body_text}")
            logger.error(f"CAS 认证失败: {error_msg}")
            return False

        # 认证成功，持久化更新后的 cookies
        _persist_client_cookies(client)

        logger.info("CAS 认证成功")
        return True

    except Exception as e:
        logger.error(f"CAS 表单登录异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def _persist_client_cookies(client: httpx.AsyncClient):
    """将 httpx client 当前的 cookies 追加保存到本地文件"""
    existing = load_cookies() or []

    # httpx Cookies → Playwright 格式 list[dict]
    new_cookies = []
    for cookie in client.cookies.jar:
        new_cookies.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or "",
            "path": cookie.path or "/",
            "secure": str(cookie.path).startswith("https"),
            "httpOnly": False,
        })

    # 合并：以 (name, domain, path) 为 key，新的覆盖旧的
    cookie_map = {}
    for c in existing:
        key = (c["name"], c.get("domain", ""), c.get("path", "/"))
        cookie_map[key] = c
    for c in new_cookies:
        key = (c["name"], c.get("domain", ""), c.get("path", "/"))
        cookie_map[key] = c

    merged = list(cookie_map.values())
    save_cookies(merged)
    logger.info(f"持久化 cookies: {len(existing)} → {len(merged)} 条")


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
        f"body_len={len(resp.text)}, final_url={resp.url}"
    )

    if resp.status_code != 200:
        logger.warning(f"访问 {source_name} 返回 {resp.status_code}")
        if "扫码登录" in resp.text or "do-login" in resp.text:
            logger.warning("检测到未授权，可能需要重新登录")
        return []

    html = resp.text

    # 检测 CAS 登录页拦截
    if _is_cas_login_page(html):
        logger.info(f"{source_name} 遇到 CAS 登录页，尝试自动 CAS 认证...")
        cas_ok = await _authenticate_cas(client, resp)
        if cas_ok:
            # CAS 认证成功，重新请求原始页面
            resp = await client.get(vpn_url)
            html = resp.text
            logger.info(
                f"{source_name} CAS 认证后重试: status={resp.status_code}, "
                f"body_len={len(html)}, final_url={resp.url}"
            )
            if _is_cas_login_page(html):
                logger.error(f"{source_name} CAS 认证后仍被拦截")
                return []
        else:
            logger.warning(
                f"{source_name} CAS 自动认证失败。"
                "请在插件配置中填写 cas_username 和 cas_password（北邮统一身份认证）"
            )
            return []

    logger.debug(f"{source_name} 响应前800字符: {html[:800]}")

    soup = BeautifulSoup(html, "lxml")
    notices = []

    # =====================================================
    #  my.bupt.edu.cn 信息门户通知列表解析
    #  页面结构: 左侧为导航树(学院/分类)，右侧为通知列表
    #  通知条目特征: 标题链接 + 发布部门 + 日期(YYYY-MM-DD)
    #  需要过滤掉左侧导航的分类链接
    # =====================================================

    date_pattern = re.compile(r'\d{4}-\d{1,2}-\d{1,2}')

    # 策略1: 查找所有包含 urltype 的链接，但要求附近有日期
    all_links = soup.select("a[href*='urltype']")
    logger.info(f"{source_name} 页面中 a[href*='urltype'] 共 {len(all_links)} 个")

    for a in all_links:
        title = a.get_text(strip=True)
        href = a.get("href", "")

        if not title or len(title) < 5:
            continue

        # 查找附近的日期文本 —— 这是区分通知和导航链接的关键
        date_str = _extract_date_near(a)
        if not date_str:
            continue  # 没有日期的大概率是导航链接，跳过

        # 提取发布部门（通常在日期附近）
        author = _extract_author_near(a)

        # 补全 URL
        full_url = href
        if href.startswith("/"):
            full_url = "http://my.bupt.edu.cn" + href
        elif not href.startswith("http"):
            full_url = "http://my.bupt.edu.cn/" + href

        notices.append(Notice(
            id=href or hashlib.md5(title.encode()).hexdigest()[:12],
            title=title,
            source=source_name,
            url=full_url,
            date=date_str,
            author=author,
        ))

    # 如果策略1 没结果，回退: 查找包含日期文本的 <li> 或 <tr>
    if not notices:
        logger.info(f"{source_name} 策略1 未匹配，尝试策略2（按日期文本查找）")
        for container in soup.find_all(['li', 'tr', 'div']):
            text = container.get_text(strip=True)
            date_match = date_pattern.search(text)
            if not date_match:
                continue

            a = container.find("a", href=True)
            if not a:
                continue

            link_text = a.get_text(strip=True)
            if not link_text or len(link_text) < 5:
                continue

            href = a.get("href", "")
            full_url = href
            if href.startswith("/"):
                full_url = "http://my.bupt.edu.cn" + href
            elif not href.startswith("http"):
                full_url = "http://my.bupt.edu.cn/" + href

            # 去重
            if any(n.id == href for n in notices):
                continue

            notices.append(Notice(
                id=href or hashlib.md5(link_text.encode()).hexdigest()[:12],
                title=link_text,
                source=source_name,
                url=full_url,
                date=date_match.group(),
                author=_extract_author_near(a),
            ))

    logger.info(f"{source_name} 解析完成，获取 {len(notices)} 条通知（已过滤无日期的导航链接）")
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


def _extract_author_near(element) -> str:
    """尝试从元素附近提取发布部门/作者"""
    date_pattern = re.compile(r'\d{4}[-/\.]\d{1,2}[-/\.]\d{1,2}')

    # 在兄弟元素和父元素中查找非日期、非标题的短文本
    candidates = []

    # 检查同级兄弟元素
    for sibling in element.next_siblings:
        if hasattr(sibling, 'get_text'):
            text = sibling.get_text(strip=True)
            if text and not date_pattern.match(text) and 2 < len(text) < 50:
                candidates.append(text)

    # 检查父元素中的子元素
    parent = element.parent
    if parent:
        for tag in parent.find_all(['span', 'td', 'em', 'div', 'p']):
            if tag == element or tag in element.parents:
                continue
            text = tag.get_text(strip=True)
            if text and not date_pattern.match(text) and 2 < len(text) < 50:
                if text not in candidates:
                    candidates.append(text)

    # 返回第一个看起来像部门名的文本
    for c in candidates:
        # 排除数字、日期、标题本身
        if date_pattern.search(c):
            continue
        if c == element.get_text(strip=True):
            continue
        return c

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
