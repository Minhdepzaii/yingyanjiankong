"""
通知系统 - 多用户版本（修复连接池问题）
"""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, Set
from loguru import logger

try:
    import aiosmtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    HAS_SMTP = True
except ImportError:
    HAS_SMTP = False
    logger.warning("aiosmtplib 未安装")

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest, TelegramError, TimedOut, NetworkError
from telegram.request import HTTPXRequest

from models import Alert, AlertStatus
from config import user_manager, AlertMode, NotifyChannel, UserConfig


class MultiUserNotifier:
    """多用户通知管理器"""
    
    def __init__(self, telegram_token: str):
        self.telegram_token = telegram_token
        self._bot: Optional[Bot] = None
        
        # 待确认报警: {user_id: {alert_id: Alert}}
        self.pending_alerts: Dict[str, Dict[str, Alert]] = {}
        # 已确认ID: {user_id: set(alert_ids)}
        self.confirmed_ids: Dict[str, Set[str]] = {}
        
        self._running = False
        self._repeat_task = None
        
        # 发送队列和速率限制
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._send_task = None
        self._rate_limit_delay = 0.05  # 50ms between messages (20 msg/sec)
        
        # SMTP配置
        self.smtp_host = os.getenv('SMTP_HOST', '')
        self.smtp_port = int(os.getenv('SMTP_PORT', 587))
        self.smtp_user = os.getenv('SMTP_USER', '')
        self.smtp_password = os.getenv('SMTP_PASSWORD', '')
        
        if self.smtp_host:
            logger.info(f"SMTP: {self.smtp_host}:{self.smtp_port}")
    
    def set_smtp_config(self, host: str, port: int, user: str, password: str):
        self.smtp_host = host
        self.smtp_port = port
        self.smtp_user = user
        self.smtp_password = password
    
    async def start(self):
        self._running = True
        
        # 创建自定义请求对象，增大连接池
        request = HTTPXRequest(
            connection_pool_size=100,        # 连接池大小（默认1）
            read_timeout=30.0,               # 读取超时
            write_timeout=30.0,              # 写入超时
            connect_timeout=30.0,            # 连接超时
            pool_timeout=10.0,               # 连接池等待超时
        )
        
        self._bot = Bot(token=self.telegram_token, request=request)
        
        # 启动发送队列处理器
        self._send_task = asyncio.create_task(self._send_queue_processor())
        
        # 启动重复提醒任务
        self._repeat_task = asyncio.create_task(self._repeat_loop())
        
        logger.info("通知系统已启动 (连接池=100)")
    
    async def stop(self):
        self._running = False
        
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        
        if self._repeat_task:
            self._repeat_task.cancel()
            try:
                await self._repeat_task
            except asyncio.CancelledError:
                pass
        
        # 关闭bot连接
        if self._bot:
            try:
                await self._bot.shutdown()
            except Exception as e:
                logger.error(f"关闭Bot失败: {e}")
    
    async def _send_queue_processor(self):
        """
        发送队列处理器 - 控制发送速率，避免连接池耗尽
        """
        while self._running:
            try:
                # 从队列获取任务
                task = await asyncio.wait_for(
                    self._send_queue.get(), 
                    timeout=1.0
                )
                
                func, args, kwargs = task
                
                # 执行发送，带重试
                for attempt in range(3):
                    try:
                        await func(*args, **kwargs)
                        break
                    except (TimedOut, NetworkError) as e:
                        if attempt < 2:
                            logger.warning(f"发送超时，重试 {attempt + 1}/3: {e}")
                            await asyncio.sleep(1)
                        else:
                            logger.error(f"发送失败（已重试3次）: {e}")
                    except Exception as e:
                        logger.error(f"发送错误: {e}")
                        break
                
                # 速率限制
                await asyncio.sleep(self._rate_limit_delay)
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"发送队列处理错误: {e}")
                await asyncio.sleep(0.1)
    
    async def _queue_send(self, func, *args, **kwargs):
        """将发送任务加入队列"""
        await self._send_queue.put((func, args, kwargs))
    
    async def send_alert_to_user(self, alert: Alert, user_config: UserConfig):
        """发送报警给用户"""
        user_id = user_config.user_id
        alert.target_user_id = user_id
        
        effective_mode = user_config.get_effective_mode()
        is_night = user_config.is_night_time()
        night_prefix = "🌙 " if is_night else ""
        need_confirm = (effective_mode == AlertMode.REPEAT)
        
        if need_confirm:
            success = await self._send_once(alert, user_config, prefix=night_prefix, 
                                  show_confirm_button=True, show_mute_button=True)
            
            if success:
                if user_id not in self.pending_alerts:
                    self.pending_alerts[user_id] = {}
                self.pending_alerts[user_id][alert.id] = alert
                logger.info(f"报警已加入重复队列: {alert.id}")
        else:
            await self._send_once(alert, user_config, prefix=night_prefix,
                                  show_confirm_button=False, show_mute_button=True)
    
    async def _send_once(self, alert: Alert, user_config: UserConfig, 
                         prefix: str = "", show_confirm_button: bool = False,
                         show_mute_button: bool = True) -> bool:
        """发送一次报警，返回是否成功"""
        alert.sent_count += 1
        alert.last_sent = datetime.now()
        alert.status = AlertStatus.SENT
        
        channels = user_config.get_notify_channels()
        success = True
        
        # Telegram
        if NotifyChannel.TELEGRAM in channels or NotifyChannel.ALL in channels:
            tg_success = await self._send_telegram(alert, user_config, prefix, 
                                      show_confirm_button, show_mute_button)
            if not tg_success:
                success = False
        
        # Email
        should_send_email = (
            NotifyChannel.EMAIL in channels or 
            NotifyChannel.ALL in channels
        )
        if should_send_email and user_config.email.to_addresses:
            await self._send_email(alert, user_config, prefix)
        
        return success
    
    async def _send_telegram(self, alert: Alert, user_config: UserConfig, 
                             prefix: str = "", show_confirm_button: bool = False,
                             show_mute_button: bool = True) -> bool:
        """发送Telegram消息，返回是否成功"""
        try:
            message = alert.to_telegram_message(prefix, user_config.timezone_offset)
            
            symbol = alert.symbol
            name = symbol.replace('USDT', '')
            
            buttons = []
            
            if show_confirm_button:
                is_night = user_config.is_night_time()
                repeat_config = user_config.get_repeat_config()
                
                buttons.append([
                    InlineKeyboardButton(
                        "✅ 确认收到", 
                        callback_data=f"confirm_alert_{alert.id}"
                    ),
                ])
                
                if is_night:
                    message += f"\n\n🌙 <i>夜间模式: 每{repeat_config['interval_seconds']}秒重复，最多{repeat_config['max_repeats']}次</i>"
            
            if show_mute_button:
                buttons.append([
                    InlineKeyboardButton(
                        "🔇 静音1小时", 
                        callback_data=f"mute_symbol_{symbol}_60"
                    ),
                    InlineKeyboardButton(
                        "🔇 静音24小时", 
                        callback_data=f"mute_symbol_{symbol}_1440"
                    ),
                ])
            
            buttons.append([
                InlineKeyboardButton(
                    f"📊 查看 {name}", 
                    callback_data=f"info_{symbol}"
                ),
            ])
            
            keyboard = InlineKeyboardMarkup(buttons) if buttons else None
            
            # 直接发送，不走队列（报警优先）
            await self._bot.send_message(
                chat_id=user_config.chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=keyboard,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=10,
            )
            return True
            
        except Forbidden as e:
            logger.warning(f"用户 {user_config.user_id} 已屏蔽机器人，标记为不活跃")
            user_manager.update_user(user_config.user_id, is_active=False)
            return False
            
        except TimedOut as e:
            logger.warning(f"Telegram超时 ({user_config.user_id}): {e}")
            # 超时可能成功，返回True避免重复发送
            return True
            
        except NetworkError as e:
            logger.error(f"Telegram网络错误 ({user_config.user_id}): {e}")
            return False
            
        except BadRequest as e:
            logger.error(f"Telegram BadRequest ({user_config.user_id}): {e}")
            return False
            
        except TelegramError as e:
            logger.error(f"Telegram错误 ({user_config.user_id}): {e}")
            return False
            
        except Exception as e:
            logger.error(f"Telegram发送失败: {e}")
            return False
    
    async def _send_email(self, alert: Alert, user_config: UserConfig, prefix: str = ""):
        """发送邮件"""
        if not HAS_SMTP:
            return
        
        if not self.smtp_host or not self.smtp_user or not self.smtp_password:
            return
        
        if not user_config.email.to_addresses:
            return
        
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = alert.to_email_subject()
            msg['From'] = self.smtp_user
            msg['To'] = ', '.join(user_config.email.to_addresses)
            
            html = alert.to_email_html(prefix, user_config.timezone_offset)
            msg.attach(MIMEText(html, 'html', 'utf-8'))
            
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=587,
                username=self.smtp_user,
                password=self.smtp_password,
                start_tls=True,
                use_tls=False,
                timeout=30,
            )
            
            logger.info(f"📧 邮件成功: {alert.symbol}")
            
        except Exception as e:
            logger.error(f"📧 邮件失败: {e}")
    
    async def _repeat_loop(self):
        """重复报警循环"""
        while self._running:
            try:
                await asyncio.sleep(5)
                
                for user_id, alerts in list(self.pending_alerts.items()):
                    user_config = user_manager.get_user(user_id)
                    if not user_config or not user_config.is_active:
                        self.pending_alerts.pop(user_id, None)
                        continue
                    
                    repeat_config = user_config.get_repeat_config()
                    
                    if not repeat_config['enabled'] and user_config.get_effective_mode() != AlertMode.REPEAT:
                        continue
                    
                    now = datetime.now()
                    interval = timedelta(seconds=repeat_config['interval_seconds'])
                    max_repeats = repeat_config['max_repeats']
                    is_night = user_config.is_night_time()
                    
                    to_remove = []
                    
                    for alert_id, alert in list(alerts.items()):
                        if self._is_confirmed(user_id, alert_id):
                            to_remove.append(alert_id)
                            continue
                        
                        if not user_config.should_monitor(alert.symbol):
                            to_remove.append(alert_id)
                            logger.info(f"代币已被静音，停止重复: {alert.symbol}")
                            continue
                        
                        if alert.sent_count >= max_repeats:
                            to_remove.append(alert_id)
                            continue
                        
                        if alert.last_sent and now - alert.last_sent >= interval:
                            night_prefix = "🌙 " if is_night else ""
                            prefix = f"{night_prefix}🔔 重复 [{alert.sent_count + 1}/{max_repeats}] "
                            
                            success = await self._send_once(
                                alert, 
                                user_config, 
                                prefix,
                                show_confirm_button=True
                            )
                            
                            if not success:
                                to_remove.append(alert_id)
                    
                    for aid in to_remove:
                        alerts.pop(aid, None)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"重复报警错误: {e}")
    
    def _is_confirmed(self, user_id: str, alert_id: str) -> bool:
        return user_id in self.confirmed_ids and alert_id in self.confirmed_ids[user_id]
    
    def confirm_alert(self, user_id: str, alert_id: str) -> bool:
        """确认报警"""
        user_id = str(user_id)
        
        if user_id not in self.confirmed_ids:
            self.confirmed_ids[user_id] = set()
        
        if user_id in self.pending_alerts:
            if alert_id in self.pending_alerts[user_id]:
                self.confirmed_ids[user_id].add(alert_id)
                alert = self.pending_alerts[user_id].pop(alert_id)
                alert.status = AlertStatus.CONFIRMED
                alert.confirmed_at = datetime.now()
                logger.info(f"报警已确认: {alert_id} by {user_id}")
                return True
            
            for aid in list(self.pending_alerts[user_id].keys()):
                if aid.startswith(alert_id) or alert_id in aid:
                    self.confirmed_ids[user_id].add(aid)
                    alert = self.pending_alerts[user_id].pop(aid)
                    alert.status = AlertStatus.CONFIRMED
                    alert.confirmed_at = datetime.now()
                    logger.info(f"报警已确认(模糊): {aid} by {user_id}")
                    return True
        
        self.confirmed_ids[user_id].add(alert_id)
        return True
    
    def confirm_all_alerts(self, user_id: str) -> int:
        """确认所有待处理报警"""
        user_id = str(user_id)
        count = 0
        
        if user_id in self.pending_alerts:
            if user_id not in self.confirmed_ids:
                self.confirmed_ids[user_id] = set()
            
            for alert_id in list(self.pending_alerts[user_id].keys()):
                self.confirmed_ids[user_id].add(alert_id)
                count += 1
            
            self.pending_alerts[user_id].clear()
        
        return count
    
    def remove_alerts_for_symbol(self, user_id: str, symbol: str) -> int:
        """移除指定代币的所有待处理报警"""
        user_id = str(user_id)
        symbol = symbol.upper()
        count = 0
        
        if user_id in self.pending_alerts:
            to_remove = []
            for alert_id, alert in self.pending_alerts[user_id].items():
                alert_symbol = alert.symbol.upper()
                if (alert_symbol == symbol or 
                    alert_symbol == f"{symbol}USDT" or 
                    alert_symbol.replace('USDT', '') == symbol.replace('USDT', '')):
                    to_remove.append(alert_id)
            
            for alert_id in to_remove:
                self.pending_alerts[user_id].pop(alert_id, None)
                if user_id not in self.confirmed_ids:
                    self.confirmed_ids[user_id] = set()
                self.confirmed_ids[user_id].add(alert_id)
                count += 1
            
            if count > 0:
                logger.info(f"已移除 {symbol} 的 {count} 个待处理报警 (user={user_id})")
        
        return count
    
    def get_pending_count(self, user_id: str) -> int:
        return len(self.pending_alerts.get(str(user_id), {}))
    
    def get_user_pending(self, user_id: str) -> Dict[str, Alert]:
        return self.pending_alerts.get(str(user_id), {})
    
    async def send_message(self, chat_id: str, text: str, 
                           reply_markup: InlineKeyboardMarkup = None) -> bool:
        """发送消息，返回是否成功"""
        try:
            await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=10,
            )
            return True
        except Forbidden as e:
            logger.warning(f"用户 {chat_id} 已屏蔽机器人: {e}")
            for user in user_manager.get_all_users():
                if user.chat_id == chat_id:
                    user_manager.update_user(user.user_id, is_active=False)
                    break
            return False
        except TimedOut:
            logger.warning(f"发送消息超时: {chat_id}")
            return True  # 可能已发送
        except Exception as e:
            logger.error(f"消息发送失败: {e}")
            return False
    
    async def broadcast(self, text: str, admin_only: bool = False):
        """广播消息"""
        users = user_manager.get_active_users()
        success_count = 0
        fail_count = 0
        
        for user in users:
            if admin_only and not user.is_admin:
                continue
            
            result = await self.send_message(user.chat_id, text)
            if result:
                success_count += 1
            else:
                fail_count += 1
            
            # 广播时增加延迟，避免触发限制
            await asyncio.sleep(0.1)
        
        logger.info(f"广播完成: 成功 {success_count}, 失败 {fail_count}")
    
    def get_queue_size(self) -> int:
        """获取发送队列大小"""
        return self._send_queue.qsize()
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        total_pending = sum(len(alerts) for alerts in self.pending_alerts.values())
        return {
            'pending_alerts': total_pending,
            'confirmed_users': len(self.confirmed_ids),
            'queue_size': self.get_queue_size(),
        }