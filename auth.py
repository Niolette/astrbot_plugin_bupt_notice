"""
WebVPN 扫码登录模块
使用 Playwright 打开 WebVPN 登录页，截取二维码，等待用户扫码完成
"""
import asyncio
import base64
import os
import re
import tempfile

from astrbot.api import logger

from .webvpn import WEBVPN_BASE, save_cookies

LOGIN_URL = WEBVPN_BASE + "/"
# 登录成功后页面 URL 不再包含 login 或者会跳转到 portal
LOGIN_DONE_PATTERNS = [
    re.compile(r"/wengine-vpn/index"),
    re.compile(r"/portal"),
    re.compile(r"webvpn\.bupt\.edu\.cn/?$"),
]


async def _ensure_playwright():
    """确保 Playwright 浏览器已安装"""
    import sys
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        await browser.close()
        await pw.stop()
    except Exception:
        logger.info("首次运行，正在安装 Playwright Chromium 浏览器及系统依赖...")
        # 先安装系统依赖（Linux 需要）
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install-deps", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(f"安装系统依赖可能失败（非 root 或非 Debian/Ubuntu）: {stderr.decode()}")
        except Exception as e:
            logger.warning(f"安装系统依赖跳过: {e}")

        # 安装浏览器
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"安装 Chromium 失败: {stderr.decode()}")
            raise RuntimeError(f"Playwright Chromium 安装失败: {stderr.decode()}")
        logger.info("Playwright Chromium 安装完成")


async def get_qrcode_and_wait(timeout: int = 120) -> tuple[str | None, list[dict] | None]:
    """
    打开 WebVPN 登录页，获取二维码图片，等待用户扫码。

    Args:
        timeout: 等待扫码超时时间（秒）

    Returns:
        (qr_image_path, cookies) - 二维码图片路径 和 登录成功后的 cookies
        如果超时或失败，cookies 为 None
    """
    from playwright.async_api import async_playwright

    await _ensure_playwright()

    qr_image_path = os.path.join(
        os.path.dirname(__file__), "data", "qrcode.png"
    )
    os.makedirs(os.path.dirname(qr_image_path), exist_ok=True)

    # 删除旧二维码文件，防止后续误读
    if os.path.exists(qr_image_path):
        os.remove(qr_image_path)

    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        logger.info(f"正在打开 WebVPN 登录页: {LOGIN_URL}")
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        logger.info(f"登录页已加载，当前 URL: {page.url}")

        # 提取二维码图片
        qr_saved = await _extract_qrcode(page, qr_image_path)
        if not qr_saved:
            logger.error("未能从登录页提取二维码")
            return None, None

        logger.info("二维码已提取，等待用户扫码...")

        # 等待登录完成（页面跳转）
        cookies = await _wait_for_login(page, context, timeout)

        if cookies:
            save_cookies(cookies)
            logger.info(f"登录成功，已保存 {len(cookies)} 条 cookies")
            return qr_image_path, cookies
        else:
            logger.warning("扫码超时或登录失败")
            return qr_image_path, None

    except Exception as e:
        logger.error(f"登录过程出错: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None
    finally:
        if browser:
            await browser.close()
        await pw.stop()


async def _extract_qrcode(page, save_path: str) -> bool:
    """从页面中提取二维码图片并保存"""
    # 方法1: 查找 base64 编码的 img 标签
    img_elements = await page.query_selector_all("img")
    for img in img_elements:
        src = await img.get_attribute("src")
        if src and src.startswith("data:image"):
            # 提取 base64 数据
            match = re.match(r"data:image/\w+;base64,(.+)", src)
            if match:
                img_data = base64.b64decode(match.group(1))
                # 检查图片尺寸（QR code 通常是正方形且不太小）
                if len(img_data) > 500:
                    with open(save_path, 'wb') as f:
                        f.write(img_data)
                    return True

    # 方法2: 查找 iframe（企业微信扫码可能在 iframe 中）
    frames = page.frames
    for frame in frames:
        if "work.weixin.qq.com" in (frame.url or ""):
            # 企业微信 iframe，截图整个 iframe
            qr_el = await frame.query_selector("img.qrcode, img#qrcode, .wrp_code img")
            if qr_el:
                await qr_el.screenshot(path=save_path)
                return True

    # 方法3: 查找特定的二维码容器并截图
    for selector in [
        ".qr-code", "#qr-code", ".qrcode", "#qrcode",
        ".login-qr", ".scan-qr", "[class*='qr']",
        "img[alt*='二维码']", "img[alt*='QR']", "img[alt*='Scan']",
    ]:
        el = await page.query_selector(selector)
        if el:
            await el.screenshot(path=save_path)
            return True

    # 方法4: 截取登录区域
    login_area = await page.query_selector(
        ".login-box, .login-form, .login-container, #login, .content-box, .main"
    )
    if login_area:
        await login_area.screenshot(path=save_path)
        return True

    # 方法5: 截取整个页面
    await page.screenshot(path=save_path, full_page=False)
    return True


async def _wait_for_login(page, context, timeout: int) -> list[dict] | None:
    """等待登录完成，轮询检测"""
    poll_interval = 2  # 秒
    elapsed = 0

    initial_url = page.url
    logger.info(f"开始等待扫码，初始 URL: {initial_url}，超时: {timeout}s")

    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        try:
            current_url = page.url
        except Exception:
            # 页面可能正在跳转
            await asyncio.sleep(1)
            continue

        # 检查 URL 是否变化
        if current_url != initial_url:
            logger.info(f"检测到 URL 变化: {current_url}")
            await asyncio.sleep(3)  # 等待跳转和 cookie 设置完成
            cookies = await context.cookies()
            logger.info(f"获取到 {len(cookies)} 条 cookies")
            if _has_vpn_ticket(cookies):
                return cookies

        # 检查是否已有 VPN ticket cookie（有些情况 URL 不变但 cookie 已设置）
        try:
            cookies = await context.cookies()
            if _has_vpn_ticket(cookies):
                logger.info(f"通过 cookie 检测到登录成功（URL 未变化），共 {len(cookies)} 条")
                return cookies
        except Exception:
            pass

        # 检查页面内容是否变化（二维码消失 = 正在处理登录）
        try:
            page_state = await page.evaluate("""() => {
                const body = document.body ? document.body.innerText : '';
                const hasQR = !!document.querySelector('img[src^="data:image"]');
                const hasLogin = body.includes('扫码登录');
                return { hasQR, hasLogin, url: window.location.href, bodyLen: body.length };
            }""")
            if not page_state.get('hasQR') and not page_state.get('hasLogin'):
                logger.info(f"页面内容已变化（二维码/登录框消失），当前: {page_state}")
                await asyncio.sleep(3)
                cookies = await context.cookies()
                if cookies:
                    return cookies
        except Exception:
            # 页面可能在跳转中，也是好信号
            logger.info("页面正在跳转中...")
            await asyncio.sleep(3)
            try:
                cookies = await context.cookies()
                if cookies:
                    return cookies
            except Exception:
                pass

        if elapsed % 10 == 0:
            logger.debug(f"等待扫码中... ({elapsed}/{timeout}s)")

    logger.warning(f"等待扫码超时 ({timeout}s)")
    return None


def _has_vpn_ticket(cookies: list[dict]) -> bool:
    """检查 cookies 中是否包含 WebVPN 认证 ticket"""
    for c in cookies:
        name = c.get('name', '').lower()
        # WebVPN 登录成功后会设置 wengine_vpn_ticket 相关 cookie
        if 'wengine_vpn_ticket' in name or 'vpn_ticket' in name:
            return True
        # 也可能是其他 session cookie
        if 'webvpn' in name and 'token' in name:
            return True
    return False


async def check_session_valid(cookies: list[dict]) -> bool:
    """检查已保存的 cookies 是否仍然有效"""
    import httpx

    cookie_dict = {}
    for c in cookies:
        domain = c.get('domain', '')
        if 'bupt.edu.cn' in domain or domain == '':
            cookie_dict[c['name']] = c['value']

    if not cookie_dict:
        logger.debug("check_session_valid: 无有效 cookies")
        return False

    # 先检查是否有 VPN ticket
    if not _has_vpn_ticket(cookies):
        logger.debug("check_session_valid: 无 VPN ticket cookie")
        return False

    try:
        async with httpx.AsyncClient(
            cookies=cookie_dict,
            follow_redirects=True,
            verify=False,
            timeout=15,
        ) as client:
            resp = await client.get(WEBVPN_BASE + "/")
            logger.debug(f"check_session_valid: 状态码={resp.status_code}, URL={resp.url}")
            # 如果返回登录页，说明 session 已过期
            if "扫码登录" in resp.text or "do-login" in str(resp.url):
                return False
            return True
    except Exception as e:
        logger.debug(f"check_session_valid: 请求出错 {e}")
        return False
