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
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        await browser.close()
        await pw.stop()
    except Exception:
        logger.info("首次运行，正在安装 Playwright Chromium 浏览器...")
        proc = await asyncio.create_subprocess_exec(
            "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
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
            logger.info("登录成功，cookies 已保存")
            return qr_image_path, cookies
        else:
            logger.warning("扫码超时或登录失败")
            return qr_image_path, None

    except Exception as e:
        logger.error(f"登录过程出错: {e}")
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

    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        current_url = page.url

        # 检查 URL 是否变化（登录成功后通常会跳转）
        if current_url != initial_url:
            # 检查是否跳转到了已认证的页面
            for pattern in LOGIN_DONE_PATTERNS:
                if pattern.search(current_url):
                    await asyncio.sleep(2)  # 等待 cookie 完全设置
                    cookies = await context.cookies()
                    return cookies

            # URL 变化了但不匹配已知模式，检查是否还在登录页
            if "/login" not in current_url.lower() and "auth" not in current_url.lower():
                await asyncio.sleep(2)
                cookies = await context.cookies()
                return cookies

        # 检查页面上是否出现了已登录的标志
        try:
            logged_in = await page.evaluate("""() => {
                // 检查是否还有二维码（如果消失了说明在跳转）
                const qr = document.querySelector('img[src^="data:image"]');
                const loginForm = document.querySelector('.login-form, .login-box, #login');
                return (!qr && !loginForm);
            }""")
            if logged_in:
                await asyncio.sleep(2)
                cookies = await context.cookies()
                if cookies:
                    return cookies
        except Exception:
            # 页面可能正在跳转，忽略错误
            pass

    return None


async def check_session_valid(cookies: list[dict]) -> bool:
    """检查已保存的 cookies 是否仍然有效"""
    import httpx

    cookie_dict = {}
    for c in cookies:
        if 'bupt.edu.cn' in c.get('domain', ''):
            cookie_dict[c['name']] = c['value']

    if not cookie_dict:
        return False

    try:
        async with httpx.AsyncClient(
            cookies=cookie_dict,
            follow_redirects=True,
            verify=False,
            timeout=15,
        ) as client:
            resp = await client.get(WEBVPN_BASE + "/")
            # 如果返回登录页，说明 session 已过期
            if "扫码登录" in resp.text or "login" in resp.url.path.lower():
                return False
            return True
    except Exception:
        return False
