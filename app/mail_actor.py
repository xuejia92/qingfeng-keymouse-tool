"""邮件发送步骤：基于标准库 smtplib + email 的封装。

设计要点：

1. QQ 邮箱 SMTP 走 SSL（端口 465），用 smtplib.SMTP_SSL 直连；其它服务器端口
   非 465 时回退到 STARTTLS（SMTP + starttls），尽量兼容。
2. 纯标准库实现（不引 yagmail/requests 等第三方库），保持打包体积。
3. 主题与正文、中文附件文件名均按 RFC 处理（Subject 用 email.header.Header，
   附件文件名用 RFC 2231 元组），避免中文乱码 / 报 UnicodeEncodeError。
4. 失败分类返回 (成功?, 原因)：认证失败、SMTP 错误、网络错误、附件缺失分别
   给出可读提示，由调用方决定是否判步骤失败。
"""
from __future__ import annotations

import os
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_mail(*, host: str, port: int, user: str, auth_code: str,
              to_addrs: list[str], subject: str, content: str,
              attachments: list[str] | None = None,
              timeout: float = 30.0) -> tuple[bool, str]:
    """发送一封邮件，返回 (成功?, 原因)。

    host / port：SMTP 服务器与端口（QQ 邮箱默认 smtp.qq.com:465）。
    user / auth_code：发送人邮箱与授权码。
    to_addrs：收件人邮箱列表（str 列表）。
    subject / content：主题与正文。
    attachments：附件绝对路径列表（可空）。
    """
    host = (host or "").strip()
    user = (user or "").strip()
    auth_code = (auth_code or "").strip()
    to_addrs = [a for a in to_addrs if (a or "").strip()]

    if not host:
        return False, "SMTP 服务器未填写"
    if not user or not auth_code:
        return False, "发送人邮箱或授权码未填写"
    if not to_addrs:
        return False, "收件人邮箱未填写"

    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 465

    # 组装邮件：multipart 承载正文 + 附件
    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = Header(subject or "", "utf-8")
    msg.attach(MIMEText(content or "", "plain", "utf-8"))

    for path in attachments or []:
        path = (path or "").strip()
        if not path:
            continue
        if not os.path.isfile(path):
            return False, f"附件不存在：{path}"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return False, f"读取附件失败：{path}（{e}）"
        part = MIMEBase("application", "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)
        filename = os.path.basename(path) or "attachment"
        # RFC 2231 编码中文/特殊字符文件名，兼容性最好
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", filename))
        msg.attach(part)

    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(user, auth_code)
        server.sendmail(user, to_addrs, msg.as_string())
        return True, f"邮件已发送给 {len(to_addrs)} 个收件人"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP 认证失败：请检查发送人邮箱与授权码（{e.smtp_code} {e.smtp_error}）"
    except smtplib.SMTPException as e:
        return False, f"SMTP 发送失败：{type(e).__name__}: {e}"
    except OSError as e:
        return False, f"网络连接失败：{type(e).__name__}: {e}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
