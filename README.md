# 北邮通知推送插件 (astrbot_plugin_bupt_notice)

AstrBot 插件 —— 自动爬取北京邮电大学信息门户通知，通过邮件推送。

## 功能

- **WebVPN 扫码登录**：通过 QQ 消息发送登录二维码，微信/企业微信扫码完成认证
- **CAS 自动认证**：配置学号密码后自动完成统一身份认证，无需额外操作
- **自动爬取通知**：定期检查信息门户 (`my.bupt.edu.cn`) 校内通知
- **邮件推送**：新通知自动发送到指定邮箱（含完整正文内容和附件）
- **会话自动续期**：登录过期时自动发送二维码给管理员，扫码后恢复

## 安装

1. 将本插件复制到 AstrBot 的 `data/plugins/` 目录下
2. 安装依赖：
   ```bash
   pip install httpx beautifulsoup4 lxml pycryptodome playwright
   playwright install chromium
   ```
3. 在 AstrBot 管理面板中配置 SMTP 邮件和 CAS 凭据
4. 重启 AstrBot

## 指令

通过 QQ 发送以下指令进行控制（通知内容通过邮件推送，不在 QQ 显示）：

| 指令 | 别名 | 说明 |
|------|------|------|
| `/bupt login` | `/bupt 登录` | 扫码登录 WebVPN |
| `/bupt status` | `/bupt 状态` | 查看登录和推送状态 |
| `/bupt check` | `/bupt 查看` `/bupt 通知` | 手动获取通知并发送邮件 |
| `/bupt latest` | `/bupt 最新` `/bupt 最新通知` | 获取最新一条通知并发送邮件 |
| `/bupt detail <序号>` | `/bupt 详情` | 获取指定通知并发送邮件 |
| `/bupt help` | `/bupt 帮助` | 显示帮助信息 |

## 配置

在 AstrBot 管理面板中配置以下选项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `cas_username` | 北邮统一身份认证用户名（学号） | - |
| `cas_password` | 北邮统一身份认证密码 | - |
| `check_interval` | 检查间隔（分钟） | 30 |
| `login_timeout` | 扫码超时（秒） | 120 |
| `smtp_server` | SMTP 服务器 | smtp.qq.com |
| `smtp_port` | SMTP 端口 | 465 |
| `smtp_user` | 发件邮箱 | - |
| `smtp_password` | SMTP 密码/授权码 | - |
| `email_to` | 收件邮箱 | - |
| `portal_url` | 自定义门户 URL | https://my.bupt.edu.cn/ |
| `max_notices` | 每次最多推送条数 | 10 |
| `admin_umo` | 管理员 QQ UMO（会话过期时自动发二维码） | - |

## 使用流程

1. **配置邮件**：在管理面板填写 SMTP 和收件邮箱
2. **配置 CAS**：填写学号和密码（用于自动登录信息门户）
3. **首次登录**：发送 `/bupt login`，用微信扫描二维码登录 WebVPN
4. **自动运行**：插件会按设定间隔自动检查新通知并发送邮件
5. **手动查看**：发送 `/bupt check` 立即获取通知邮件

## 注意事项

- WebVPN 会话有时效，过期后需要重新扫码。配置 `admin_umo` 后可自动推送二维码
- `admin_umo` 值可在发送 `/bupt login` 时从日志中获取（格式如 `default:FriendMessage:123456`）
- 登录需要使用**绑定了学校企业微信的微信客户端**扫码
- 首次运行会自动安装 Playwright Chromium 浏览器（约 150MB）
- 邮件推送需要正确配置 SMTP，QQ 邮箱需使用授权码而非密码
