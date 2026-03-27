"""
邮件推送模块
将通知格式化为邮件并通过 SMTP 发送
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

from astrbot.api import logger

from .portal import Notice


def format_notices_html(notices: list[Notice]) -> str:
    """将通知列表格式化为 HTML 邮件内容"""
    items_html = ""
    for n in notices:
        date_part = f'<span style="color:#888;font-size:12px;">{n.date}</span>' if n.date else ""
        source_part = f'<span style="color:#1890ff;font-size:12px;">[{n.source}]</span>'
        content_part = ""
        if n.content:
            preview = n.content[:200] + ("..." if len(n.content) > 200 else "")
            content_part = f'<p style="color:#666;font-size:13px;margin:4px 0 0 0;">{preview}</p>'

        items_html += f"""
        <div style="padding:12px 0;border-bottom:1px solid #eee;">
            <div>{source_part} {date_part}</div>
            <div style="font-size:15px;margin:4px 0;">
                <a href="{n.url}" style="color:#333;text-decoration:none;">{n.title}</a>
            </div>
            {content_part}
        </div>
        """

    return f"""
    <html>
    <body style="font-family: -apple-system, 'Segoe UI', sans-serif; max-width:600px; margin:0 auto; padding:20px;">
        <h2 style="color:#1890ff; border-bottom:2px solid #1890ff; padding-bottom:8px;">
            📢 北邮通知更新
        </h2>
        <p style="color:#888;">共 {len(notices)} 条新通知</p>
        {items_html}
        <p style="color:#aaa;font-size:12px;margin-top:20px;text-align:center;">
            —— 由 AstrBot 北邮通知推送插件自动发送 ——
        </p>
    </body>
    </html>
    """


def format_notices_text(notices: list[Notice]) -> str:
    """将通知列表格式化为纯文本（含详情内容）"""
    lines = [f"📢 北邮通知更新 (共 {len(notices)} 条)\n"]
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
    发送通知邮件

    Args:
        notices: 通知列表
        smtp_server: SMTP 服务器
        smtp_port: SMTP 端口
        smtp_user: 发件人邮箱
        smtp_password: SMTP 密码/授权码
        to_addr: 收件人邮箱
    """
    if not notices:
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = Header(
        f"北邮通知更新 ({len(notices)} 条新通知)", "utf-8"
    )

    text_content = format_notices_text(notices)
    html_content = format_notices_html(notices)

    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            server.starttls()

        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_addr], msg.as_string())
        server.quit()
        logger.info(f"通知邮件已发送至 {to_addr}")
    except Exception as e:
        logger.error(f"发送邮件失败: {e}")
        raise
