# 北邮通知推送插件 (astrbot_plugin_bupt_notice)

AstrBot 插件 —— 自动爬取北京邮电大学信息门户通知，通过 QQ 和邮件推送。

## 功能

- **WebVPN 扫码登录**：通过 QQ 消息发送登录二维码，微信/企业微信扫码完成认证
- **自动爬取通知**：定期检查通知公告、校内新闻和信息门户消息
- **QQ 推送**：新通知自动发送到 QQ（通过 NapCat）
- **邮件推送**：可选，将新通知推送到指定邮箱
- **手动查看**：随时查看最新通知和详情

## 安装

1. 将本文件夹复制到 AstrBot 的 `data/plugins/` 目录下
2. 安装依赖：
   ```bash
   pip install httpx beautifulsoup4 lxml pycryptodome playwright
   playwright install chromium
   ```
3. 重启 AstrBot

## 指令

| 指令 | 别名 | 说明 |
|------|------|------|
| `/bupt login` | `/bupt 登录` | 扫码登录 WebVPN |
| `/bupt status` | `/bupt 状态` | 查看登录和订阅状态 |
| `/bupt check` | `/bupt 查看` `/bupt 通知` | 手动查看新通知 |
| `/bupt latest` | `/bupt 最新` `/bupt 最新通知` | 获取最新一条校内通知及详情 |
| `/bupt detail <序号>` | `/bupt 详情` | 查看通知详情 |
| `/bupt subscribe` | `/bupt 订阅` | 订阅自动推送 |
| `/bupt unsubscribe` | `/bupt 取消订阅` | 取消自动推送 |
| `/bupt help` | `/bupt 帮助` | 显示帮助信息 |

## 配置

在 AstrBot 管理面板中配置以下选项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `check_interval` | 检查间隔（分钟） | 30 |
| `login_timeout` | 扫码超时（秒） | 120 |
| `email_enabled` | 启用邮件推送 | false |
| `smtp_server` | SMTP 服务器 | smtp.qq.com |
| `smtp_port` | SMTP 端口 | 465 |
| `smtp_user` | 发件邮箱 | - |
| `smtp_password` | SMTP 密码/授权码 | - |
| `email_to` | 收件邮箱 | - |
| `portal_url` | 自定义门户 URL | https://my.bupt.edu.cn/ |
| `max_notices` | 每次最多推送条数 | 10 |

## 使用流程

1. **首次使用**：发送 `/bupt login`，插件会把 WebVPN 登录二维码发给你
2. **扫码**：用绑定了学校企业微信的微信客户端扫描二维码
3. **订阅**：发送 `/bupt subscribe` 开启自动推送
4. **查看**：发送 `/bupt check` 手动查看通知

## 注意事项

- WebVPN 会话有时效，过期后需要重新 `/bupt login`
- 登录需要使用**绑定了学校企业微信的微信客户端**扫码
- 首次运行会自动安装 Playwright Chromium 浏览器（约 150MB）
- 如在服务器上部署，确保有足够磁盘空间
