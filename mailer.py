"""
邮件推送模块
将通知格式化为邮件并通过 SMTP 发送，支持附件和内联图片
"""
import hashlib
import os
import re
import smtplib
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import Header
from email import encoders

import httpx

from astrbot.api import logger

from .portal import Notice
from .webvpn import encode_webvpn_url, cookies_to_httpx, load_cookies


def format_notices_html(notices: list[Notice]) -> str:
    """将通知列表格式化为 HTML 邮件内容（含详情）"""
    items_html = ""
    for i, n in enumerate(notices, 1):
        date_part = f'<span style="color:#888;font-size:12px;">{n.date}</span>' if n.date else ""
        source_part = f'<span style="color:#1890ff;font-size:12px;">[{n.source}]</span>'
        author_part = f'<span style="color:#52c41a;font-size:12px;">✍️ {n.author}</span>' if n.author else ""

        content_html = ""
        if n.content_html:
            # 使用原始 HTML 内容（保留图片和格式，img src 已在发送时替换为 cid）
            content_html = f'<div style="color:#444;font-size:14px;margin:8px 0;line-height:1.6;">{n.content_html}</div>'
        elif n.content:
            # 回退：纯文本转 HTML
            escaped = n.content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            escaped = escaped.replace("\n", "<br>")
            content_html = f'<div style="color:#444;font-size:14px;margin:8px 0;line-height:1.6;">{escaped}</div>'
        else:
            content_html = '<p style="color:#999;font-size:13px;">（未获取到详情内容）</p>'

        # 附件列表
        attach_html = ""
        if n.attachments:
            attach_items = ""
            for att in n.attachments:
                attach_items += f'<li style="margin:4px 0;"><a href="{att["url"]}" style="color:#1890ff;">{att["name"]}</a></li>'
            attach_html = f"""
            <div style="margin:8px 0;padding:8px;background:#f6f8fa;border-radius:4px;">
                <strong style="font-size:13px;">📎 附件 ({len(n.attachments)} 个):</strong>
                <ul style="margin:4px 0;padding-left:20px;">{attach_items}</ul>
            </div>
            """

        items_html += f"""
        <div style="padding:16px 0;border-bottom:2px solid #eee;">
            <div style="margin-bottom:4px;">{source_part} {author_part} {date_part}</div>
            <h3 style="font-size:16px;margin:4px 0;color:#333;">{i}. {n.title}</h3>
            {content_html}
            {attach_html}
        </div>
        """

    return f"""
    <html>
    <body style="font-family: -apple-system, 'Segoe UI', sans-serif; max-width:700px; margin:0 auto; padding:20px;">
        {items_html}
        <p style="color:#aaa;font-size:12px;margin-top:20px;text-align:center;">
            —— 由 AstrBot 北邮通知推送插件自动发送 ——
        </p>
    </body>
    </html>
    """


def format_notices_text(notices: list[Notice]) -> str:
    """将通知列表格式化为纯文本（含详情内容）"""
    lines = []
    for i, n in enumerate(notices, 1):
        date_part = f" ({n.date})" if n.date else ""
        lines.append(f"━━━━━━━━━━━━━━━━━━━")
        lines.append(f"{i}. [{n.source}] {n.title}{date_part}")
        if n.content:
            # 显示完整内容（限制 800 字符避免 QQ 消息过长）
            content_text = n.content.strip()
            if len(content_text) > 800:
                content_text = content_text[:800] + "...(内容过长已截断)"
            lines.append(f"{content_text}")
        else:
            lines.append("（未获取到详情内容）")
        lines.append("")
    return "\n".join(lines)


async def send_email(
    notices: list[Notice],
    smtp_server: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    to_addr: str,
):
    """
    发送通知邮件（含详情内容、内联图片和附件）

    Args:
        notices: 通知列表（需已填充 content 和 attachments）
        smtp_server: SMTP 服务器
        smtp_port: SMTP 端口
        smtp_user: 发件人邮箱
        smtp_password: SMTP 密码/授权码
        to_addr: 收件人邮箱
    """
    if not notices:
        return

    if not smtp_user or not smtp_password:
        raise ValueError("SMTP 发件配置不完整，请配置 smtp_user 和 smtp_password")
    if not to_addr:
        raise ValueError("收件邮箱地址未配置，请配置 email_to")

    # 下载内联图片并将 content_html 中的 img src 替换为 cid: 引用
    inline_images = await _process_inline_images(notices)

    # 使用 mixed 类型以支持附件
    msg = MIMEMultipart("mixed")
    msg["From"] = smtp_user
    msg["To"] = to_addr
    if len(notices) == 1:
        subject = notices[0].title
    else:
        subject = f"{notices[0].title} 等 {len(notices)} 条通知"
    msg["Subject"] = Header(subject, "utf-8")

    # 正文部分
    text_content = format_notices_text(notices)
    html_content = format_notices_html(notices)

    if inline_images:
        # 使用 multipart/related 嵌入内联图片
        related_part = MIMEMultipart("related")
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(text_content, "plain", "utf-8"))
        body_part.attach(MIMEText(html_content, "html", "utf-8"))
        related_part.attach(body_part)

        for cid, img_data, content_type in inline_images:
            maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("image", "png")
            img_part = MIMEBase(maintype, subtype)
            img_part.set_payload(img_data)
            encoders.encode_base64(img_part)
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline")
            related_part.attach(img_part)

        msg.attach(related_part)
        logger.info(f"已嵌入 {len(inline_images)} 张内联图片")
    else:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(text_content, "plain", "utf-8"))
        body_part.attach(MIMEText(html_content, "html", "utf-8"))
        msg.attach(body_part)

    # 下载并添加附件
    all_attachments = []
    for n in notices:
        if n.attachments:
            all_attachments.extend(n.attachments)

    if all_attachments:
        logger.info(f"准备下载 {len(all_attachments)} 个附件...")
        downloaded = await _download_attachments(all_attachments)
        for filename, file_data in downloaded:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(file_data)
            encoders.encode_base64(part)
            # RFC 2231 编码中文文件名
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=("utf-8", "", filename),
            )
            msg.attach(part)
        logger.info(f"已添加 {len(downloaded)}/{len(all_attachments)} 个附件到邮件")

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_addr], msg.as_string())
        server.quit()
        logger.info(f"通知邮件已发送至 {to_addr}（含 {len(inline_images)} 张图片、{len(all_attachments)} 个附件）")
    except Exception as e:
        logger.error(f"发送邮件失败: {e}")
        raise


async def _process_inline_images(
    notices: list[Notice],
) -> list[tuple[str, bytes, str]]:
    """
    下载通知 HTML 内容中的图片，替换 img src 为 cid: 引用。

    直接修改 notice.content_html 中的 img src。

    Returns:
        [(cid, image_data, content_type), ...]
    """
    # 收集所有图片 URL → CID 映射
    img_map: dict[str, str] = {}  # url -> cid

    for notice in notices:
        if not notice.content_html:
            continue
        for match in re.finditer(r'<img\s[^>]*src=["\']([^"\']+)["\']', notice.content_html, re.IGNORECASE):
            src = match.group(1)
            if src.startswith(("http://", "https://")) and src not in img_map:
                cid = f"img_{hashlib.md5(src.encode()).hexdigest()[:12]}"
                img_map[src] = cid

    if not img_map:
        return []

    logger.info(f"准备下载 {len(img_map)} 张内联图片...")

    results = []
    cookies_list = load_cookies()
    if not cookies_list:
        return results

    cookie_dict = cookies_to_httpx(cookies_list)

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        follow_redirects=True,
        verify=False,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        },
    ) as client:
        for url, cid in img_map.items():
            try:
                vpn_url = encode_webvpn_url(url)
                resp = await client.get(vpn_url)

                if resp.status_code != 200:
                    logger.warning(f"内联图片下载失败 ({url}): HTTP {resp.status_code}")
                    continue

                content_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()
                if not content_type.startswith("image/"):
                    logger.warning(f"内联图片类型异常 ({url}): {content_type}")
                    continue

                # 限制单张图片 5MB
                if len(resp.content) > 5 * 1024 * 1024:
                    logger.warning(f"内联图片过大 ({url}): {len(resp.content)} bytes，跳过")
                    continue

                results.append((cid, resp.content, content_type))

                # 替换所有通知中该 URL 为 cid 引用
                for notice in notices:
                    if notice.content_html:
                        notice.content_html = notice.content_html.replace(url, f"cid:{cid}")

                logger.debug(f"内联图片已下载: {url} -> cid:{cid} ({len(resp.content)} bytes)")

            except Exception as e:
                logger.warning(f"内联图片下载异常 ({url}): {e}")

    logger.info(f"内联图片下载完成: {len(results)}/{len(img_map)}")
    return results


async def _download_attachments(
    attachments: list[dict],
    max_size: int = 20 * 1024 * 1024,  # 单个附件最大 20MB
) -> list[tuple[str, bytes]]:
    """
    通过 WebVPN 下载附件文件。

    Returns:
        [(filename, file_bytes), ...]
    """
    results = []
    cookies_list = load_cookies()
    if not cookies_list:
        return results

    cookie_dict = cookies_to_httpx(cookies_list)

    async with httpx.AsyncClient(
        cookies=cookie_dict,
        follow_redirects=True,
        verify=False,
        timeout=60,
    ) as client:
        for att in attachments:
            name = att.get("name", "attachment")
            url = att.get("url", "")
            if not url:
                continue

            try:
                vpn_url = encode_webvpn_url(url)
                resp = await client.get(vpn_url)

                if resp.status_code != 200:
                    logger.warning(f"附件下载失败 ({name}): HTTP {resp.status_code}")
                    continue

                data = resp.content
                if len(data) > max_size:
                    logger.warning(f"附件 {name} 过大 ({len(data)} bytes)，跳过")
                    continue

                # 从响应头提取真实文件名
                cd = resp.headers.get("content-disposition", "")
                if "filename" in cd:
                    import re
                    # filename*=UTF-8''xxx 或 filename="xxx"
                    match = re.search(r"filename\*?=['\"]?(?:UTF-8''|utf-8'')?([^'\";]+)", cd)
                    if match:
                        from urllib.parse import unquote
                        name = unquote(match.group(1))

                results.append((name, data))
                logger.info(f"附件已下载: {name} ({len(data)} bytes)")

            except Exception as e:
                logger.warning(f"附件下载异常 ({name}): {e}")

    return results
