import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    import aiosmtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = os.getenv('SMTP_HOST')
    smtp_user = os.getenv('SMTP_USER')
    smtp_password = os.getenv('SMTP_PASSWORD')
    email_to = "yitdlin@gmail.com"

    msg = MIMEMultipart('alternative')
    msg['Subject'] = '鹰眼监控 - 邮件测试'
    msg['From'] = smtp_user
    msg['To'] = email_to
    msg.attach(MIMEText("<h1>邮件配置测试成功</h1>", 'html', 'utf-8'))


    print(f"发件服务器: {smtp_host}")
    print(f"发件人: {smtp_user}")
    print(f"收件人: {email_to}\n")


    # 方法1: STARTTLS on 587 (推荐)
    print("方法1: 587 + STARTTLS")
    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=587,
            username=smtp_user,
            password=smtp_password,
            start_tls=True,
            use_tls=False,
        )
        print("成功: 587 + STARTTLS")
        return
    except Exception as e:
        print(f"失败: {e}\n")


    # 方法2: SMTPS on 465
    print("方法2: 465 + SSL/TLS")
    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=465,
            username=smtp_user,
            password=smtp_password,
            use_tls=True,
            start_tls=False,
        )
        print("成功: 465 + SSL/TLS")
        return
    except Exception as e:
        print(f"失败: {e}\n")


    # 方法3: 无加密 on 25 (仅测试连接，不推荐)
    print("方法3: 25 + 无TLS")
    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=25,
            username=smtp_user,
            password=smtp_password,
            start_tls=False,
            use_tls=False,
        )
        print("成功: 25（不加密）")
    except Exception as e:
        print(f"失败: {e}\n")

asyncio.run(test())
