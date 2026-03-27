"""
AstrBot 北邮信息门户通知推送插件
功能:
  - WebVPN 扫码登录（二维码通过 QQ 转发）
  - 定期爬取信息门户通知
  - QQ 消息推送 + 邮件推送
"""
import asyncio
import os

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star

from .auth import get_qrcode_and_wait, check_session_valid
from .portal import fetch_notices, fetch_latest_notice, mark_seen, Notice, fetch_notice_detail, set_cas_credentials
from .mailer import send_email, format_notices_text
from .webvpn import load_cookies


class BuptNoticePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.running = True
        self._login_lock = asyncio.Lock()
        self._subscriber_umo: str | None = None
        self._umo_file = os.path.join(os.path.dirname(__file__), "data", "subscriber.txt")

        # 设置 CAS 凭据
        cas_user = self.config.get("cas_username", "")
        cas_pass = self.config.get("cas_password", "")
        if cas_user and cas_pass:
            set_cas_credentials(cas_user, cas_pass)
            logger.info("CAS 凭据已加载")
        else:
            logger.warning("CAS 凭据未配置（cas_username / cas_password），信息门户通知需要 CAS 认证")

        # 加载已保存的订阅者
        self._load_subscriber()

        # 启动定时任务
        interval = self.config.get("check_interval", 30)
        if interval > 0:
            asyncio.create_task(self._periodic_check(interval))

    def _load_subscriber(self):
        """加载订阅者 UMO"""
        if os.path.exists(self._umo_file):
            try:
                with open(self._umo_file, 'r') as f:
                    self._subscriber_umo = f.read().strip() or None
            except OSError:
                pass

    def _save_subscriber(self):
        """保存订阅者 UMO"""
        os.makedirs(os.path.dirname(self._umo_file), exist_ok=True)
        with open(self._umo_file, 'w') as f:
            f.write(self._subscriber_umo or "")

    # ==================== 指令 ====================

    @filter.command_group("bupt")
    def bupt(self):
        """北邮通知推送"""
        pass

    @bupt.command("login", alias={"登录"})
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录 WebVPN，将二维码通过 QQ 发送给你"""
        if self._login_lock.locked():
            yield event.plain_result("⏳ 正在等待扫码中，请勿重复操作")
            return

        async with self._login_lock:
            yield event.plain_result("🔄 正在获取 WebVPN 登录二维码，请稍候...")

            timeout = self.config.get("login_timeout", 120)

            # 获取二维码 — 这里在后台开始，先把二维码发出去
            qr_path, cookies = await self._do_login(event, timeout)

            if cookies:
                yield event.plain_result("✅ 登录成功！已保存会话，可以开始获取通知了。")
            elif qr_path:
                yield event.plain_result("❌ 扫码超时或登录失败，请重试：/bupt login")
            else:
                yield event.plain_result("❌ 获取二维码失败，请检查网络后重试")

    async def _do_login(self, event: AstrMessageEvent, timeout: int):
        """执行登录流程：获取二维码 → 发送 → 等待扫码"""
        target_umo = event.unified_msg_origin
        logger.info(f"登录请求来源 UMO: {target_umo}")
        return await self._do_login_to(target_umo, timeout)

    async def _do_login_to(self, target_umo: str, timeout: int):
        """执行登录流程，将二维码发送到指定 UMO"""
        from .auth import get_qrcode_and_wait as _login

        # 删除旧二维码文件
        qr_path = os.path.join(os.path.dirname(__file__), "data", "qrcode.png")
        if os.path.exists(qr_path):
            os.remove(qr_path)

        # 在后台启动 Playwright 登录
        login_task = asyncio.create_task(_login(timeout))

        # 等待二维码生成（最多 30 秒）
        qr_sent = False
        for _ in range(60):
            await asyncio.sleep(0.5)
            if os.path.exists(qr_path):
                # 检查文件是否写入完成（大小 > 0 且稳定）
                size = os.path.getsize(qr_path)
                await asyncio.sleep(0.5)
                if os.path.getsize(qr_path) == size and size > 0:
                    # 发送二维码给用户
                    chain = MessageChain() \
                        .message("📱 请使用微信/企业微信扫描以下二维码登录 WebVPN：") \
                        .file_image(qr_path)
                    await self.context.send_message(target_umo, chain)
                    qr_sent = True
                    break

        # 等待登录完成
        qr_result_path, cookies = await login_task

        if not qr_sent and qr_result_path:
            # 晚了一步但二维码已生成，说明登录流程已结束
            pass

        return qr_result_path, cookies

    @bupt.command("status", alias={"状态"})
    async def cmd_status(self, event: AstrMessageEvent):
        """检查登录状态"""
        cookies = load_cookies()
        if not cookies:
            yield event.plain_result("❌ 未登录，请使用 /bupt login 登录")
            return

        valid = await check_session_valid(cookies)
        if valid:
            sub_status = "已订阅" if self._subscriber_umo else "未订阅"
            interval = self.config.get("check_interval", 30)
            email_status = "已启用" if self.config.get("email_enabled", False) else "未启用"
            yield event.plain_result(
                f"✅ WebVPN 会话有效\n"
                f"📬 自动推送: {sub_status}\n"
                f"⏱️ 检查间隔: {interval} 分钟\n"
                f"📧 邮件推送: {email_status}"
            )
        else:
            yield event.plain_result("⚠️ 会话已过期，请使用 /bupt login 重新登录")

    @bupt.command("check", alias={"查看", "通知"})
    async def cmd_check(self, event: AstrMessageEvent):
        """手动检查最新通知（含详情内容）"""
        cookies = load_cookies()
        if not cookies:
            yield event.plain_result("❌ 未登录，请先使用 /bupt login 登录")
            return

        yield event.plain_result("🔍 正在检查新通知...")

        portal_url = self.config.get("portal_url", "")
        max_count = self.config.get("max_notices", 10)

        try:
            notices = await fetch_notices(
                portal_url=portal_url or None,
                max_count=max_count,
            )
        except Exception as e:
            logger.error(f"获取通知失败: {e}")
            yield event.plain_result(f"❌ 获取通知失败: {e}")
            return

        if not notices:
            yield event.plain_result("📭 暂无新通知")
            return

        # 获取每条通知的详情内容
        for notice in notices:
            try:
                content = await fetch_notice_detail(notice)
                notice.content = content
            except Exception as e:
                logger.warning(f"获取通知详情失败 ({notice.title}): {e}")

        # 推送通知到 QQ
        text = format_notices_text(notices)
        yield event.plain_result(text)

        # 标记为已推送
        mark_seen(notices)

    @bupt.command("latest", alias={"最新", "最新通知"})
    async def cmd_latest(self, event: AstrMessageEvent):
        """获取最新的一条校内通知"""
        cookies = load_cookies()
        if not cookies:
            yield event.plain_result("❌ 未登录，请先使用 /bupt login 登录")
            return

        yield event.plain_result("🔍 正在获取最新通知...")

        try:
            notice = await fetch_latest_notice()
        except Exception as e:
            logger.error(f"获取最新通知失败: {e}")
            yield event.plain_result(f"❌ 获取失败: {e}")
            return

        if not notice:
            yield event.plain_result("📭 未获取到通知")
            return

        # 获取详情内容
        content = await fetch_notice_detail(notice)

        result = f"📢 最新校内通知\n"
        result += f"━━━━━━━━━━━━━━━━━━━\n"
        result += f"📄 {notice.title}\n"
        if notice.date:
            result += f"📅 {notice.date}\n"
        if notice.source:
            result += f"📂 {notice.source}\n"
        if notice.author:
            result += f"✍️ {notice.author}\n"
        result += f"━━━━━━━━━━━━━━━━━━━\n"
        result += content[:1500] if content else "（无法获取详情内容）"

        yield event.plain_result(result)

    @bupt.command("subscribe", alias={"订阅"})
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅自动通知推送（当前会话）"""
        self._subscriber_umo = event.unified_msg_origin
        self._save_subscriber()
        interval = self.config.get("check_interval", 30)
        yield event.plain_result(
            f"✅ 已订阅自动推送\n"
            f"每 {interval} 分钟检查一次新通知，有更新时将自动推送到本会话。"
        )

    @bupt.command("unsubscribe", alias={"取消订阅"})
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消自动推送"""
        self._subscriber_umo = None
        self._save_subscriber()
        yield event.plain_result("✅ 已取消自动推送")

    @bupt.command("detail", alias={"详情"})
    async def cmd_detail(self, event: AstrMessageEvent, index: int = 1):
        """查看通知详情（传入序号）"""
        # 获取通知列表然后取第 index 条
        portal_url = self.config.get("portal_url", "")
        max_count = self.config.get("max_notices", 10)

        try:
            notices = await fetch_notices(
                portal_url=portal_url or None,
                max_count=max_count,
            )
        except Exception as e:
            yield event.plain_result(f"❌ 获取通知失败: {e}")
            return

        if not notices:
            yield event.plain_result("📭 暂无通知")
            return

        if index < 1 or index > len(notices):
            yield event.plain_result(f"⚠️ 请输入 1-{len(notices)} 之间的序号")
            return

        notice = notices[index - 1]
        content = await fetch_notice_detail(notice)

        result = f"📄 {notice.title}\n"
        if notice.date:
            result += f"📅 {notice.date}\n"
        if notice.source:
            result += f"📂 {notice.source}\n"
        result += f"\n{content[:1500] if content else '（无法获取详情内容）'}"

        yield event.plain_result(result)

    @bupt.command("help", alias={"帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(
            "📢 北邮通知推送插件 帮助\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "/bupt login  - 扫码登录 WebVPN\n"
            "/bupt status - 查看登录和订阅状态\n"
            "/bupt check  - 手动查看新通知\n"
            "/bupt latest - 获取最新一条校内通知\n"
            "/bupt detail <序号> - 查看通知详情\n"
            "/bupt subscribe - 订阅自动推送\n"
            "/bupt unsubscribe - 取消自动推送\n"
            "/bupt help   - 显示本帮助\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "💡 首次使用请先 /bupt login"
        )

    # ==================== 定时任务 ====================

    async def _periodic_check(self, interval_minutes: int):
        """定期检查新通知并推送"""
        await asyncio.sleep(10)  # 启动后等待 10 秒再开始
        logger.info(f"北邮通知定时检查已启动，间隔 {interval_minutes} 分钟")

        while self.running:
            try:
                await asyncio.sleep(interval_minutes * 60)

                if not self.running:
                    break

                # 检查是否有订阅者
                if not self._subscriber_umo:
                    continue

                # 检查 cookies 是否有效
                cookies = load_cookies()
                if not cookies:
                    continue

                valid = await check_session_valid(cookies)
                if not valid:
                    logger.warning("WebVPN 会话已过期，尝试自动重新登录")

                    admin_umo = self.config.get("admin_umo", "")
                    if admin_umo and not self._login_lock.locked():
                        # 自动触发登录，发二维码给管理员
                        async with self._login_lock:
                            chain = MessageChain().message(
                                "⚠️ WebVPN 会话已过期，正在自动获取登录二维码..."
                            )
                            await self.context.send_message(admin_umo, chain)

                            timeout = self.config.get("login_timeout", 120)
                            qr_path, new_cookies = await self._do_login_to(admin_umo, timeout)

                            if new_cookies:
                                chain = MessageChain().message("✅ 自动重新登录成功！")
                                await self.context.send_message(admin_umo, chain)
                            elif qr_path:
                                chain = MessageChain().message("❌ 扫码超时，请手动 /bupt login")
                                await self.context.send_message(admin_umo, chain)
                            else:
                                chain = MessageChain().message("❌ 获取二维码失败，请手动 /bupt login")
                                await self.context.send_message(admin_umo, chain)
                    else:
                        # 无管理员配置或正在登录中，仅发提醒
                        if self._subscriber_umo:
                            chain = MessageChain().message(
                                "⚠️ WebVPN 会话已过期，请使用 /bupt login 重新登录"
                            )
                            await self.context.send_message(self._subscriber_umo, chain)
                    continue

                # 获取新通知
                portal_url = self.config.get("portal_url", "")
                max_count = self.config.get("max_notices", 10)

                notices = await fetch_notices(
                    portal_url=portal_url or None,
                    max_count=max_count,
                )

                if not notices:
                    continue

                logger.info(f"发现 {len(notices)} 条新通知")

                # 获取每条通知的详情内容和附件
                for notice in notices:
                    try:
                        content = await fetch_notice_detail(notice)
                        notice.content = content
                    except Exception as e:
                        logger.warning(f"获取通知详情失败 ({notice.title}): {e}")

                # QQ 推送
                text = format_notices_text(notices)
                chain = MessageChain().message(text)
                await self.context.send_message(self._subscriber_umo, chain)

                # 邮件推送
                if self.config.get("email_enabled", False):
                    try:
                        await send_email(
                            notices=notices,
                            smtp_server=self.config.get("smtp_server", "smtp.qq.com"),
                            smtp_port=self.config.get("smtp_port", 465),
                            smtp_user=self.config.get("smtp_user", ""),
                            smtp_password=self.config.get("smtp_password", ""),
                            to_addr=self.config.get("email_to", ""),
                        )
                    except Exception as e:
                        logger.error(f"邮件推送失败: {e}")

                # 标记为已推送
                mark_seen(notices)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时检查出错: {e}")
                await asyncio.sleep(60)  # 出错后等 1 分钟再重试

    async def terminate(self):
        """插件停用时调用"""
        self.running = False
        logger.info("北邮通知推送插件已停用")
