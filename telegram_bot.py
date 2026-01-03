"""
Telegram机器人 - 完整版 (修复静音后重复提醒问题)
"""
import asyncio
from typing import Optional, TYPE_CHECKING, Dict
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, Message
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from loguru import logger

from config import (
    user_manager, AlertProfile, AlertMode, NotifyChannel,
    PRESET_CONFIGS, UserConfig, TIMEZONE_PRESETS
)
from notifier import MultiUserNotifier
from models import MarketType

if TYPE_CHECKING:
    from main import HawkEyeSystem


class TelegramBot:
    """多用户Telegram机器人"""
    
    def __init__(self, token: str, notifier: MultiUserNotifier):
        self.token = token
        self.notifier = notifier
        self.app: Optional[Application] = None
        self.system: Optional['HawkEyeSystem'] = None
        
        # 临时静音记录: {user_id: {symbol: unmute_time}}
        self.muted_symbols: Dict[str, Dict[str, datetime]] = {}
    
    def set_system(self, system: 'HawkEyeSystem'):
        self.system = system
    
    async def start(self):
        """启动"""
        self.app = Application.builder().token(self.token).build()
        
        commands = [
            ("start", self._cmd_start),
            ("help", self._cmd_help),
            ("menu", self._cmd_menu),
            ("status", self._cmd_status),
            ("config", self._cmd_config),
            ("profile", self._cmd_profile),
            ("mode", self._cmd_mode),
            ("watch", self._cmd_watch),
            ("whitelist", self._cmd_whitelist),
            ("blacklist", self._cmd_blacklist),
            ("email", self._cmd_email),
            ("night", self._cmd_night),
            ("timezone", self._cmd_timezone),
            ("tz", self._cmd_timezone),
            ("confirm", self._cmd_confirm),
            ("pending", self._cmd_pending),
            ("minvol", self._cmd_minvol),
            ("filter", self._cmd_minvol),
            ("test", self._cmd_test),
            # 排行榜命令
            ("top", self._cmd_top),
            ("rank", self._cmd_top),
            ("gainers", self._cmd_gainers),
            ("losers", self._cmd_losers),
            ("volume", self._cmd_volume),
            ("spread", self._cmd_spread),
            ("funding", self._cmd_funding),
            ("price", self._cmd_price),
            ("info", self._cmd_info),
            # 管理员
            ("admin", self._cmd_admin),
            ("users", self._cmd_users),
            ("broadcast", self._cmd_broadcast),
        ]
        
        for cmd, handler in commands:
            self.app.add_handler(CommandHandler(cmd, handler))
        
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        
        await self.app.initialize()
        await self.app.start()
        await self._set_commands()
        await self.app.updater.start_polling(drop_pending_updates=True)
        
        # 启动静音清理任务
        asyncio.create_task(self._mute_cleanup_loop())
        
        logger.info("Telegram机器人已启动")
    
    async def stop(self):
        """停止机器人"""
        if self.app:
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
                if self.app.running:
                    await self.app.stop()
                    await self.app.shutdown()
            except Exception as e:
                logger.error(f"停止机器人错误: {e}")

    async def _set_commands(self):
        commands = [
            BotCommand("menu", "🎛️ 控制面板"),
            BotCommand("status", "📊 系统状态"),
            BotCommand("top", "📊 排行榜"),
            BotCommand("gainers", "🟢 涨幅榜"),
            BotCommand("losers", "🔴 跌幅榜"),
            BotCommand("spread", "📐 差价榜"),
            BotCommand("price", "💰 查询价格"),
            BotCommand("pending", "🔔 待确认报警"),
            BotCommand("confirm", "✅ 确认报警"),
            BotCommand("night", "🌙 夜间模式"),
            BotCommand("watch", "👁️ 监控设置"),
            BotCommand("minvol", "💎 成交额筛选"),
            BotCommand("whitelist", "✅ 白名单"),
            BotCommand("blacklist", "🚫 黑名单"),
            BotCommand("timezone", "🌍 时区设置"),
            BotCommand("config", "⚙️ 配置"),
            BotCommand("help", "❓ 帮助"),
        ]
        await self.app.bot.set_my_commands(commands)
    
    def _get_user(self, update: Update) -> UserConfig:
        user = update.effective_user
        chat = update.effective_chat
        user_id = str(user.id)
        chat_id = str(chat.id) if chat else user_id
        
        user_config = user_manager.get_or_create_user(
            user_id,
            user.username or user.first_name or "",
            chat_id
        )
        
        # 🔧 用户能发消息说明没有屏蔽机器人，自动恢复活跃状态
        if not user_config.is_active:
            user_manager.update_user(user_id, is_active=True)
            user_config = user_manager.get_user(user_id)
            logger.info(f"用户自动恢复活跃: {user_id}")
        
        # 更新 chat_id（可能变化）
        if chat_id != user_config.chat_id:
            user_manager.update_user(user_id, chat_id=chat_id)
            user_config = user_manager.get_user(user_id)
        
        return user_config
    
    def _format_volume(self, v: float) -> str:
        """格式化成交额"""
        if v >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        elif v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        elif v >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.2f}"
    
    def _format_price(self, p: float) -> str:
        """格式化价格"""
        if p >= 1000:
            return f"${p:,.2f}"
        elif p >= 1:
            return f"${p:.4f}"
        elif p >= 0.0001:
            return f"${p:.6f}"
        else:
            return f"${p:.8f}"
    
    def _mute_symbol_for_user(self, user_id: str, symbol: str, minutes: int) -> int:
        """
        静音代币的统一方法
        返回被移除的待处理报警数量
        """
        user_id = str(user_id)
        symbol = symbol.upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        # 1. 添加到黑名单
        user_manager.add_to_blacklist(user_id, [symbol])
        
        # 2. 立即移除该代币的所有待处理报警 - 关键修复！
        removed_count = self.notifier.remove_alerts_for_symbol(user_id, symbol)
        
        # 3. 清除报警引擎中该代币的冷却记录（可选）
        if self.system and hasattr(self.system, 'alert_engine'):
            self.system.alert_engine.clear_cooldowns(user_id=user_id, symbol=symbol)
        
        # 4. 记录自动解除时间
        if user_id not in self.muted_symbols:
            self.muted_symbols[user_id] = {}
        unmute_time = datetime.now() + timedelta(minutes=minutes)
        self.muted_symbols[user_id][symbol] = unmute_time
        
        logger.info(f"静音代币: {symbol} for {user_id}, 移除 {removed_count} 个待处理报警, {minutes}分钟后解除")
        
        return removed_count
    
    def _unmute_symbol_for_user(self, user_id: str, symbol: str):
        """取消静音的统一方法"""
        user_id = str(user_id)
        symbol = symbol.upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        # 从黑名单移除
        user_manager.remove_from_blacklist(user_id, [symbol])
        
        # 清除定时记录
        if user_id in self.muted_symbols:
            self.muted_symbols[user_id].pop(symbol, None)
        
        logger.info(f"取消静音: {symbol} for {user_id}")
    
    async def _mute_cleanup_loop(self):
        """定期清理过期的静音并发送恢复通知"""
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                
                for user_id in list(self.muted_symbols.keys()):
                    for symbol in list(self.muted_symbols[user_id].keys()):
                        if self.muted_symbols[user_id][symbol] <= now:
                            # 从黑名单移除
                            user_manager.remove_from_blacklist(user_id, [symbol])
                            del self.muted_symbols[user_id][symbol]
                            
                            name = symbol.replace('USDT', '')
                            logger.info(f"自动取消静音: {symbol} for {user_id}")
                            
                            # 发送恢复通知
                            user_config = user_manager.get_user(user_id)
                            if user_config and user_config.is_active:
                                try:
                                    await self.notifier.send_message(
                                        user_config.chat_id,
                                        f"🔔 <b>{name} 静音已到期</b>\n\n"
                                        f"已恢复该代币的报警通知\n"
                                        f"⏰ {user_config.get_local_time_str()}"
                                    )
                                except Exception as e:
                                    logger.error(f"发送静音恢复通知失败: {e}")
                    
                    if not self.muted_symbols[user_id]:
                        del self.muted_symbols[user_id]
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"静音清理错误: {e}")
    
    # ================== 确认报警命令 ==================
    
    async def _cmd_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """确认报警: /confirm [ID|all]"""
        user_config = self._get_user(update)
        args = context.args
        
        if not args:
            pending = self.notifier.get_user_pending(user_config.user_id)
            
            if not pending:
                await update.message.reply_text("✅ 没有待确认的报警")
                return
            
            text = f"🔔 <b>待确认报警 ({len(pending)})</b>\n\n"
            for alert_id, alert in list(pending.items())[:10]:
                text += f"• <code>{alert_id}</code> {alert.symbol} (已发{alert.sent_count}次)\n"
            
            if len(pending) > 10:
                text += f"\n... 还有 {len(pending) - 10} 个"
            
            keyboard = [
                [InlineKeyboardButton("✅ 确认全部", callback_data="confirm_all_alerts")],
            ]
            
            for alert_id, alert in list(pending.items())[:5]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"确认 {alert.symbol.replace('USDT', '')} ({alert_id})", 
                        callback_data=f"confirm_alert_{alert_id}"
                    )
                ])
            
            text += "\n\n💡 /confirm all 确认全部"
            
            await update.message.reply_text(
                text, 
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
            return
        
        if args[0].lower() == "all":
            count = self.notifier.confirm_all_alerts(user_config.user_id)
            await update.message.reply_text(f"✅ 已确认全部报警 ({count} 个)")
            return
        
        alert_id = args[0]
        if self.notifier.confirm_alert(user_config.user_id, alert_id):
            pending = self.notifier.get_pending_count(user_config.user_id)
            await update.message.reply_text(
                f"✅ 报警 <code>{alert_id}</code> 已确认\n"
                f"剩余待确认: {pending} 个",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(f"❌ 未找到报警 {alert_id}")
    
    async def _cmd_pending(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看待确认报警"""
        user_config = self._get_user(update)
        pending = self.notifier.get_user_pending(user_config.user_id)
        
        if not pending:
            await update.message.reply_text("✅ 没有待确认的报警")
            return
        
        text = f"🔔 <b>待确认报警 ({len(pending)})</b>\n\n"
        
        for alert_id, alert in list(pending.items())[:10]:
            text += f"• <code>{alert_id}</code>\n"
            text += f"  {alert.symbol} | {alert.message[:25]}...\n"
            text += f"  已发送 {alert.sent_count} 次\n\n"
        
        if len(pending) > 10:
            text += f"... 还有 {len(pending) - 10} 个\n"
        
        keyboard = [
            [InlineKeyboardButton("✅ 确认全部", callback_data="confirm_all_alerts")],
        ]
        
        for alert_id, alert in list(pending.items())[:3]:
            name = alert.symbol.replace('USDT', '')
            keyboard.append([
                InlineKeyboardButton(
                    f"✅ 确认 {name}", 
                    callback_data=f"confirm_alert_{alert_id}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("◀️ 返回", callback_data="back_menu")])
        
        text += "\n💡 点击按钮确认或输入 /confirm all"
        
        await update.message.reply_text(
            text, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # ================== 排行榜命令 ==================
    
    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """排行榜菜单"""
        user_config = self._get_user(update)
        
        keyboard = [
            # 现货
            [InlineKeyboardButton("━━━ 📈 现货 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("🟢 涨幅榜", callback_data="rank_gainers_spot"),
                InlineKeyboardButton("🔴 跌幅榜", callback_data="rank_losers_spot"),
            ],
            [
                InlineKeyboardButton("💰 成交额榜", callback_data="rank_volume_spot"),
            ],
            # 合约
            [InlineKeyboardButton("━━━ 📊 合约 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("🟢 涨幅榜", callback_data="rank_gainers_futures"),
                InlineKeyboardButton("🔴 跌幅榜", callback_data="rank_losers_futures"),
            ],
            [
                InlineKeyboardButton("💰 成交额榜", callback_data="rank_volume_futures"),
            ],
            # 合约特有
            [InlineKeyboardButton("━━━ 📐 期现数据 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("📐 差价榜", callback_data="rank_spread"),
            ],
            [
                InlineKeyboardButton("📈 费率(正)", callback_data="rank_funding_pos"),
                InlineKeyboardButton("📉 费率(负)", callback_data="rank_funding_neg"),
            ],
        ]
        
        local_time = user_config.get_local_time_str()
        
        await update.message.reply_text(
            f"📊 <b>实时排行榜</b>\n\n"
            f"选择要查看的排行:\n\n"
            f"⏰ {local_time}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_gainers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        await self._show_gainers(update.message, user_config, MarketType.SPOT)
    
    async def _cmd_losers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        await self._show_losers(update.message, user_config, MarketType.SPOT)
    
    async def _cmd_volume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        await self._show_volume_rank(update.message, user_config, MarketType.SPOT)
    
    async def _cmd_spread(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        await self._show_spread_rank(update.message, user_config)
    
    async def _cmd_funding(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        await self._show_funding_rank(update.message, user_config)
    
    async def _cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        args = context.args
        
        if not args:
            await update.message.reply_text(
                "用法: /price <代币>\n"
                "例如: /price BTC\n"
                "或: /price BTCUSDT"
            )
            return
        
        symbol = args[0].upper()
        if not symbol.endswith('USDT'):
            symbol += 'USDT'
        
        await self._show_token_info(update.message, user_config, symbol)
    
    async def _cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._cmd_price(update, context)
    
    async def _show_gainers(self, message: Message, user_config: UserConfig, 
                            market: MarketType, edit: bool = False):
        if not self.system:
            return
        
        gainers = self.system.binance.get_top_gainers(15, market)
        market_name = "现货" if market == MarketType.SPOT else "合约"
        market_icon = "📈" if market == MarketType.SPOT else "📊"
        
        text = f"🟢 <b>{market_icon} {market_name}涨幅榜 TOP 15</b>\n\n"
        
        for i, (symbol, price, change, volume) in enumerate(gainers, 1):
            name = symbol.replace('USDT', '')
            text += f"{i}. <b>{name}</b>\n"
            text += f"   💰 {self._format_price(price)} | 📈 +{change:.2f}%\n"
            text += f"   📊 {self._format_volume(volume)}\n\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"rank_gainers_{market.value}")]]
        
        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    async def _show_losers(self, message: Message, user_config: UserConfig,
                           market: MarketType, edit: bool = False):
        if not self.system:
            return
        
        losers = self.system.binance.get_top_losers(15, market)
        market_name = "现货" if market == MarketType.SPOT else "合约"
        market_icon = "📈" if market == MarketType.SPOT else "📊"
        
        text = f"🔴 <b>{market_icon} {market_name}跌幅榜 TOP 15</b>\n\n"
        
        for i, (symbol, price, change, volume) in enumerate(losers, 1):
            name = symbol.replace('USDT', '')
            text += f"{i}. <b>{name}</b>\n"
            text += f"   💰 {self._format_price(price)} | 📉 {change:.2f}%\n"
            text += f"   📊 {self._format_volume(volume)}\n\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"rank_losers_{market.value}")]]
        
        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    async def _show_volume_rank(self, message: Message, user_config: UserConfig, 
                                market: MarketType = MarketType.SPOT, edit: bool = False):
        if not self.system:
            return
        
        items = self.system.binance.get_top_volume(15, market)
        market_name = "现货" if market == MarketType.SPOT else "合约"
        market_icon = "📈" if market == MarketType.SPOT else "📊"
        
        text = f"💰 <b>{market_icon} {market_name} 24H成交额榜 TOP 15</b>\n\n"
        
        for i, (symbol, price, change, volume) in enumerate(items, 1):
            name = symbol.replace('USDT', '')
            emoji = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
            text += f"{i}. <b>{name}</b>\n"
            text += f"   💰 {self._format_price(price)} | {emoji} {change:+.2f}%\n"
            text += f"   📊 {self._format_volume(volume)}\n\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        callback = f"rank_volume_{market.value}"
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=callback)]]
        
        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    async def _show_spread_rank(self, message: Message, user_config: UserConfig, edit: bool = False):
        if not self.system:
            return
        
        spreads = self.system.binance.get_top_spreads(15)
        
        text = "📐 <b>现货合约差价榜 TOP 15</b>\n\n"
        
        for i, (symbol, spot, futures, spread, funding) in enumerate(spreads, 1):
            name = symbol.replace('USDT', '')
            spread_emoji = "🔺" if spread > 0 else "🔻"
            text += f"{i}. <b>{name}</b>\n"
            text += f"   现货: {self._format_price(spot)}\n"
            text += f"   合约: {self._format_price(futures)}\n"
            text += f"   {spread_emoji} 差价: {spread:+.2f}% | 费率: {funding:.4f}%\n\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data="rank_spread")]]
        
        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    async def _show_funding_rank(self, message: Message, user_config: UserConfig, 
                                  positive: bool = True, edit: bool = False):
        if not self.system:
            return
        
        items = self.system.binance.get_top_funding_rates(15, positive)
        
        title = "资金费率最高" if positive else "资金费率最低"
        emoji = "📈" if positive else "📉"
        
        text = f"{emoji} <b>{title} TOP 15</b>\n\n"
        
        for i, (symbol, rate, price) in enumerate(items, 1):
            name = symbol.replace('USDT', '')
            text += f"{i}. <b>{name}</b>\n"
            text += f"   💰 {self._format_price(price)}\n"
            text += f"   📊 费率: {rate:+.4f}%\n\n"
        
        text += f"\n💡 正费率=多付空, 负费率=空付多"
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        callback = "rank_funding_pos" if positive else "rank_funding_neg"
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=callback)]]
        
        if edit:
            await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    async def _show_token_info(self, message: Message, user_config: UserConfig, symbol: str):
        if not self.system:
            return
        
        spot_info = self.system.binance.get_token_info(symbol, MarketType.SPOT)
        futures_info = self.system.binance.get_token_info(symbol, MarketType.FUTURES)
        
        if not spot_info and not futures_info:
            await message.reply_text(f"❌ 未找到代币: {symbol}")
            return
        
        name = symbol.replace('USDT', '')
        text = f"💎 <b>{name} / USDT</b>\n\n"
        
        if spot_info:
            change_emoji = "📈" if spot_info.price_change_percent_24h > 0 else "📉"
            text += f"<b>📈 现货</b>\n"
            text += f"价格: {self._format_price(spot_info.price)}\n"
            text += f"24h: {change_emoji} {spot_info.price_change_percent_24h:+.2f}%\n"
            text += f"最高: {self._format_price(spot_info.high_24h)}\n"
            text += f"最低: {self._format_price(spot_info.low_24h)}\n"
            text += f"成交额: {spot_info.volume_display}\n"
            text += f"成交笔: {spot_info.trades_24h:,}\n\n"
        
        if futures_info:
            funding = self.system.binance.funding_rates.get(symbol, 0)
            text += f"<b>📊 合约</b>\n"
            text += f"价格: {self._format_price(futures_info.price)}\n"
            text += f"资金费率: {funding:+.4f}%\n"
            
            if spot_info:
                spread = ((futures_info.price - spot_info.price) / spot_info.price) * 100
                text += f"差价: {spread:+.2f}%\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"info_{symbol}")]]
        
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    # ================== 时区命令 ==================
    
    async def _cmd_timezone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        args = context.args
        
        if args:
            try:
                offset = int(args[0])
                if -12 <= offset <= 14:
                    user_manager.set_timezone(user_config.user_id, offset)
                    user_config = user_manager.get_user(user_config.user_id)
                    await update.message.reply_text(
                        f"✅ 时区已设置为 UTC{offset:+d}\n"
                        f"当前时间: {user_config.get_local_time_str()}"
                    )
                    return
            except ValueError:
                pass
        
        keyboard = []
        row = []
        for name, offset in TIMEZONE_PRESETS.items():
            btn = InlineKeyboardButton(
                f"{'✅' if user_config.timezone_offset == offset else ''}{name}",
                callback_data=f"tz_{offset}_{name}"
            )
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([InlineKeyboardButton("◀️ 返回", callback_data="back_menu")])
        
        await update.message.reply_text(
            f"🌍 <b>时区设置</b>\n\n"
            f"当前时区: <b>{user_config.timezone_name}</b> (UTC{user_config.timezone_offset:+d})\n"
            f"当前时间: {user_config.get_local_time_str()}\n\n"
            f"选择你的时区:\n\n"
            f"💡 也可以直接输入: <code>/timezone 8</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # ================== 夜间模式命令 ==================
    
    def _get_night_keyboard(self, user_config):
        """夜间模式键盘 - 紧急配置"""
        night = user_config.alert_mode.night
        
        return [
            [InlineKeyboardButton(
                f"{'🔴 关闭' if night.enabled else '🟢 开启'} 夜间模式", 
                callback_data="toggle_night"
            )],
            [
                InlineKeyboardButton("⏰ 22:00-07:00", callback_data="night_time_22_07"),
                InlineKeyboardButton("⏰ 23:00-08:00", callback_data="night_time_23_08"),
            ],
            [
                InlineKeyboardButton("⏰ 00:00-09:00", callback_data="night_time_00_09"),
            ],
            # 更短的间隔选项
            [
                InlineKeyboardButton("🔥10秒", callback_data="night_interval_10"),
                InlineKeyboardButton("15秒", callback_data="night_interval_15"),
                InlineKeyboardButton("30秒", callback_data="night_interval_30"),
            ],
            # 更多的重复次数
            [
                InlineKeyboardButton("20次", callback_data="night_max_20"),
                InlineKeyboardButton("🔥30次", callback_data="night_max_30"),
                InlineKeyboardButton("50次", callback_data="night_max_50"),
            ],
            [InlineKeyboardButton(
                f"{'✅' if night.night_add_email else '⬜'} 夜间加邮件通知", 
                callback_data="toggle_night_email"
            )],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
    
    def _get_mode_keyboard(self, user_config):
        """报警模式键盘"""
        mode = user_config.alert_mode.mode
        repeat = user_config.alert_mode.repeat
        
        return [
            [InlineKeyboardButton(f"{'✅' if mode == AlertMode.SINGLE else '⬜'} 📢 单次报警", callback_data="mode_single")],
            [InlineKeyboardButton(f"{'✅' if mode == AlertMode.REPEAT else '⬜'} 🔁 重复提醒(紧急)", callback_data="mode_repeat")],
            # 重复间隔快捷设置
            [
                InlineKeyboardButton("🔥10秒", callback_data="repeat_interval_10"),
                InlineKeyboardButton("15秒", callback_data="repeat_interval_15"),
                InlineKeyboardButton("30秒", callback_data="repeat_interval_30"),
            ],
            # 重复次数快捷设置
            [
                InlineKeyboardButton("20次", callback_data="repeat_max_20"),
                InlineKeyboardButton("🔥30次", callback_data="repeat_max_30"),
                InlineKeyboardButton("50次", callback_data="repeat_max_50"),
            ],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]

    async def _cmd_night(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """夜间模式设置"""
        user_config = self._get_user(update)
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        night = user_config.alert_mode.night
        
        keyboard = self._get_night_keyboard(user_config)
        
        await update.message.reply_text(
            f"🌙 <b>夜间模式设置</b>\n\n"
            f"<b>当前状态:</b>\n"
            f"• 夜间模式: {'✅ 已开启' if night.enabled else '❌ 未开启'}\n"
            f"• 当前时段: {'🌙 夜间' if is_night else '☀️ 日间'}\n"
            f"• 生效模式: <b>{effective_mode.value}</b>\n\n"
            f"<b>夜间时段:</b> {night.night_start} - {night.night_end}\n"
            f"<b>重复间隔:</b> {night.night_interval_seconds} 秒\n"
            f"<b>最大重复:</b> {night.night_max_repeats} 次\n"
            f"<b>夜间加邮件:</b> {'✅' if night.night_add_email else '❌'}\n\n"
            f"💡 夜间模式开启后，在夜间时段会自动切换为<b>重复提醒</b>模式，确保不错过重要行情\n\n"
            f"⏰ {user_config.get_local_time_str()}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # ================== 其他命令 ==================
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat = update.effective_chat
        user_id = str(user.id)
        
        # 获取或创建用户
        user_config = user_manager.get_or_create_user(
            user_id,
            user.username or user.first_name or "",
            str(chat.id) if chat else user_id
        )
        
        # 如果用户之前被标记为不活跃，现在重新激活
        was_inactive = not user_config.is_active
        if was_inactive:
            user_manager.update_user(user_id, is_active=True)
            user_config = user_manager.get_user(user_id)
            logger.info(f"用户重新激活: {user_id} ({user_config.username})")
        
        # 同时更新 chat_id
        if str(chat.id) != user_config.chat_id:
            user_manager.update_user(user_id, chat_id=str(chat.id))
        
        # 欢迎消息
        reactivate_msg = "\n\n🔔 <b>已重新激活通知！</b>" if was_inactive else ""
        
        # 加入群组按钮
        keyboard = [
            [InlineKeyboardButton("📢 加入交流群", url="https://t.me/+mMYvl04GeTIwODdl")],
        ]
        
        await update.message.reply_text(
            f"🦅 <b>欢迎使用鹰眼监控系统 v1.3</b>\n\n"
            f"你好 <b>{user_config.username or '用户'}</b>！{reactivate_msg}\n\n"
            "📋 <b>快速开始:</b>\n"
            "• /menu - 控制面板\n"
            "• /status - 系统状态\n"
            "• /test - 测试报警\n"
            "• /top - 排行榜\n"
            "• /price BTC - 查询价格\n"
            "• /night - 夜间模式\n"
            "• /timezone - 设置时区\n"
            "• /pending - 待确认报警\n"
            "• /help - 帮助\n\n"
            "✨ <b>新功能:</b>\n"
            "• ⚡ 升级穿透 - 级别升级立即报警\n"
            "• 🌙 夜间模式 - 自动重复提醒实现紧急唤醒",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # 加入群组按钮
        keyboard = [
            [InlineKeyboardButton("📢 加入交流群", url="https://t.me/+mMYvl04GeTIwODdl")],
        ]
        
        help_text = """
    🦅 <b>鹰眼监控系统 v1.3 - 帮助</b>
    
    <b>📊 排行榜</b>
    /top - 排行榜菜单
    /gainers - 涨幅榜
    /losers - 跌幅榜
    /volume - 成交额榜
    /spread - 差价榜
    /funding - 资金费率
    /price BTC - 查询价格
    
    <b>🔔 报警管理</b>
    /pending - 待确认报警
    /confirm - 确认报警列表
    /confirm all - 确认全部
    
    <b>👁️ 监控设置</b>
    /watch - 监控模式
    /whitelist add BTC ETH - 白名单
    /blacklist add SHIB - 黑名单
    
    <b>⚙️ 报警设置</b>
    /profile - 灵敏度
    /mode - 报警模式
    /night - 夜间模式 (自动重复提醒)
    /email xxx@email.com - 邮件
    
    <b>🌍 时区</b>
    /timezone - 时区选择
    /tz 8 - 直接设置 UTC+8
    
    <b>✨ 新功能</b>
    • ⚡ 升级穿透 - 同级别过滤，升级立即报警
    • 🌙 夜间模式 - 自动重复提醒实现紧急唤醒
    """
        await update.message.reply_text(
            help_text, 
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        keyboard = self._get_main_menu_keyboard()
        pending = self.notifier.get_pending_count(user_config.user_id)
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        
        await update.message.reply_text(
            f"🦅 <b>鹰眼控制面板</b>\n\n"
            f"<b>监控:</b> {user_config.watch_mode}\n"
            f"<b>灵敏度:</b> {user_config.profile.value}\n"
            f"<b>报警模式:</b> {effective_mode.value} {'🌙' if is_night else ''}\n"
            f"<b>夜间模式:</b> {'✅' if user_config.alert_mode.night.enabled else '❌'}\n"
            f"<b>时区:</b> {user_config.timezone_name}\n"
            f"<b>待确认:</b> {pending}\n\n"
            f"⏰ {user_config.get_local_time_str()}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        
        spot_count = len(self.system.binance.spot_symbols) if self.system else 0
        futures_count = len(self.system.binance.futures_symbols) if self.system else 0
        pending = self.notifier.get_pending_count(user_config.user_id)
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        
        engine_stats = {}
        if self.system and hasattr(self.system, 'alert_engine'):
            engine_stats = self.system.alert_engine.get_stats()
        
        # 添加用户统计
        all_users = user_manager.get_all_users()
        total_users = len(all_users)
        active_users = len([u for u in all_users if u.is_active])
        
        text = f"""
    📊 <b>系统状态</b>
    
    <b>运行状态:</b> ✅ 正常
    <b>现货:</b> {spot_count} 个
    <b>合约:</b> {futures_count} 个
    <b>用户:</b> {total_users} 人 (活跃: {active_users})
    
    <b>你的配置:</b>
    • 时区: {user_config.timezone_name} (UTC{user_config.timezone_offset:+d})
    • 当前: {"🌙 夜间" if is_night else "☀️ 日间"}
    • 生效模式: {effective_mode.value}
    • 夜间模式: {'✅' if user_config.alert_mode.night.enabled else '❌'}
    • 监控: {user_config.watch_mode}
    • 白名单: {len(user_config.whitelist)} 个
    • 黑名单: {len(user_config.blacklist)} 个
    • 待确认: {pending} 个
    
    ⏰ {user_config.get_local_time_str()}
    """
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    
    async def _cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        channels = [c.value for c in user_config.notify_channels]
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        night = user_config.alert_mode.night
        
        text = f"""
⚙️ <b>当前配置</b>

<b>🌍 时区:</b> {user_config.timezone_name} (UTC{user_config.timezone_offset:+d})

<b>🎯 灵敏度:</b> {user_config.profile.value}
• 1分钟: ±{user_config.price.short_1m_pump}%
• 5分钟: ±{user_config.price.mid_5m_pump}%
• 冷却: {user_config.cooldown_seconds}秒

<b>👁️ 监控:</b> {user_config.watch_mode}
• 白名单: {len(user_config.whitelist)} 个
• 黑名单: {len(user_config.blacklist)} 个

<b>🔔 报警模式:</b>
• 日间模式: {user_config.alert_mode.mode.value}
• 夜间模式: {'✅ 已开启' if night.enabled else '❌ 未开启'}
• 当前生效: {effective_mode.value} {'🌙' if is_night else '☀️'}

<b>🌙 夜间配置:</b>
• 时段: {night.night_start} - {night.night_end}
• 间隔: {night.night_interval_seconds}秒
• 重复: {night.night_max_repeats}次
• 加邮件: {'✅' if night.night_add_email else '❌'}

<b>📧 通知:</b>
• 邮件: {"✅" if user_config.email.enabled else "❌"}
• 渠道: {channels}

⏰ {user_config.get_local_time_str()}
"""
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    
    async def _cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        keyboard = self._get_watch_keyboard(user_config)
        await update.message.reply_text(
            self._get_watch_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_whitelist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        args = context.args or []
        
        if not args:
            await update.message.reply_text(
                self._get_list_text(user_config, "whitelist"),
                parse_mode=ParseMode.HTML
            )
            return
        
        action = args[0].lower()
        symbols = self._parse_symbols(args[1:])
        
        if action == 'add' and symbols:
            user_manager.add_to_whitelist(user_config.user_id, symbols)
            await update.message.reply_text(f"✅ 已添加: {', '.join(symbols)}")
        elif action in ('del', 'remove', 'rm') and symbols:
            user_manager.remove_from_whitelist(user_config.user_id, symbols)
            await update.message.reply_text(f"✅ 已移除: {', '.join(symbols)}")
        elif action == 'clear':
            user_manager.update_user(user_config.user_id, whitelist=[])
            await update.message.reply_text("✅ 白名单已清空")
        else:
            await update.message.reply_text(
                "用法:\n"
                "<code>/whitelist add BTC ETH SOL</code>\n"
                "<code>/whitelist del BTC</code>\n"
                "<code>/whitelist clear</code>",
                parse_mode=ParseMode.HTML
            )
    
    async def _cmd_blacklist(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        args = context.args or []
        
        if not args:
            await update.message.reply_text(
                self._get_list_text(user_config, "blacklist"),
                parse_mode=ParseMode.HTML
            )
            return
        
        action = args[0].lower()
        symbols = self._parse_symbols(args[1:])
        
        if action == 'add' and symbols:
            # 使用统一的静音方法（永久静音）
            for symbol in symbols:
                user_manager.add_to_blacklist(user_config.user_id, [symbol])
                # 也要移除待处理报警
                self.notifier.remove_alerts_for_symbol(user_config.user_id, symbol)
            await update.message.reply_text(f"✅ 已添加到黑名单: {', '.join(symbols)}")
        elif action in ('del', 'remove', 'rm') and symbols:
            user_manager.remove_from_blacklist(user_config.user_id, symbols)
            # 清除临时静音记录
            for symbol in symbols:
                if user_config.user_id in self.muted_symbols:
                    self.muted_symbols[user_config.user_id].pop(symbol, None)
            await update.message.reply_text(f"✅ 已移除: {', '.join(symbols)}")
        elif action == 'clear':
            user_manager.update_user(user_config.user_id, blacklist=[])
            self.muted_symbols.pop(user_config.user_id, None)
            await update.message.reply_text("✅ 黑名单已清空")
        else:
            await update.message.reply_text(
                "用法:\n"
                "<code>/blacklist add SHIB DOGE</code>\n"
                "<code>/blacklist del SHIB</code>\n"
                "<code>/blacklist clear</code>",
                parse_mode=ParseMode.HTML
            )
    
    async def _cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        keyboard = self._get_profile_keyboard(user_config)
        await update.message.reply_text(
            self._get_profile_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        keyboard = self._get_mode_keyboard(user_config)
        await update.message.reply_text(
            self._get_mode_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_email(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        args = context.args
        
        if args:
            action = args[0].lower()
            
            if action == "on":
                user_config.email.enabled = True
                if NotifyChannel.EMAIL not in user_config.notify_channels:
                    user_config.notify_channels.append(NotifyChannel.EMAIL)
                user_manager._save()
                await update.message.reply_text("✅ 邮件通知已启用")
                return
            
            elif action == "off":
                user_config.email.enabled = False
                if NotifyChannel.EMAIL in user_config.notify_channels:
                    user_config.notify_channels.remove(NotifyChannel.EMAIL)
                user_manager._save()
                await update.message.reply_text("✅ 邮件通知已禁用")
                return
            
            elif '@' in args[0]:
                email_addr = args[0]
                user_config.email.to_addresses = [email_addr]
                user_config.email.enabled = True
                if NotifyChannel.EMAIL not in user_config.notify_channels:
                    user_config.notify_channels.append(NotifyChannel.EMAIL)
                user_manager._save()
                await update.message.reply_text(
                    f"✅ 邮箱已设置: {email_addr}\n"
                    f"✅ 邮件通知已启用"
                )
                return
        
        keyboard = self._get_email_keyboard(user_config)
        emails = ', '.join(user_config.email.to_addresses) or '未设置'
        channels = [c.value for c in user_config.notify_channels]
        
        await update.message.reply_text(
            f"📧 <b>邮件设置</b>\n\n"
            f"状态: {'✅ 已启用' if user_config.email.enabled else '❌ 未启用'}\n"
            f"邮箱: {emails}\n"
            f"通知渠道: {channels}\n\n"
            f"命令:\n"
            f"<code>/email on</code> - 启用\n"
            f"<code>/email off</code> - 禁用\n"
            f"<code>/email xxx@email.com</code> - 设置邮箱",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from models import Alert, AlertType, AlertLevel, MarketType
        
        user_config = self._get_user(update)
        
        btc_info = None
        if self.system:
            btc_info = self.system.binance.get_token_info("BTCUSDT", MarketType.SPOT)
        
        alert = Alert(
            alert_type=AlertType.PRICE_PUMP,
            level=AlertLevel.WARNING,
            symbol="BTCUSDT",
            market_type=MarketType.SPOT,
            message="测试报警 - 5分钟涨幅 5.00%",
            data={
                'price': btc_info.price if btc_info else 50000,
                'change_percent': 5.0,
                'high_24h': btc_info.high_24h if btc_info else 51000,
                'low_24h': btc_info.low_24h if btc_info else 49000,
                'volume_24h': btc_info.quote_volume_24h if btc_info else 1000000000,
            }
        )
        
        await self.notifier.send_alert_to_user(alert, user_config)
        await update.message.reply_text("✅ 测试报警已发送")
    
    async def _cmd_minvol(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """成交额筛选设置: /minvol [金额]"""
        user_config = self._get_user(update)
        args = context.args
        
        if args:
            arg = args[0].upper()
            
            # 处理关闭
            if arg in ('OFF', '0', 'NO', 'DISABLE', '关闭'):
                user_manager.set_volume_filter(user_config.user_id, False, 0)
                await update.message.reply_text("✅ 成交额筛选已关闭\n现在会监控所有代币")
                return
            
            # 处理开启（不带金额）
            if arg in ('ON', 'YES', 'ENABLE', '开启'):
                if user_config.min_volume_24h > 0:
                    user_manager.set_volume_filter(user_config.user_id, True)
                    await update.message.reply_text(
                        f"✅ 成交额筛选已开启\n"
                        f"最低成交额: {user_config.get_volume_filter_display()}"
                    )
                else:
                    await update.message.reply_text("❌ 请先设置金额，例如: /minvol 10M")
                return
            
            # 解析金额
            try:
                value = self._parse_volume_value(arg)
                if value > 0:
                    user_manager.set_volume_filter(user_config.user_id, True, value)
                    user_config = user_manager.get_user(user_config.user_id)
                    await update.message.reply_text(
                        f"✅ 成交额筛选已设置\n\n"
                        f"最低24h成交额: <b>{user_config.get_volume_filter_display()}</b>\n\n"
                        f"💡 只有成交额达标的代币才会触发报警",
                        parse_mode=ParseMode.HTML
                    )
                    return
            except:
                pass
            
            await update.message.reply_text(
                "❌ 无效的金额格式\n\n"
                "示例:\n"
                "<code>/minvol 10M</code> - 1000万USDT\n"
                "<code>/minvol 100M</code> - 1亿USDT\n"
                "<code>/minvol 1B</code> - 10亿USDT\n"
                "<code>/minvol 5000000</code> - 500万USDT\n"
                "<code>/minvol off</code> - 关闭筛选",
                parse_mode=ParseMode.HTML
            )
            return
        
        # 显示当前设置和选项菜单
        keyboard = self._get_volume_filter_keyboard(user_config)
        
        await update.message.reply_text(
            f"💎 <b>24H成交额筛选</b>\n\n"
            f"<b>当前状态:</b> {'✅ 已开启' if user_config.volume_filter_enabled else '❌ 未开启'}\n"
            f"<b>最低成交额:</b> {user_config.get_volume_filter_display()}\n\n"
            f"💡 开启后，只有24小时成交额达到设定值的代币才会触发报警\n"
            f"适合过滤小币种，专注主流币\n\n"
            f"<b>快捷设置:</b>\n"
            f"<code>/minvol 10M</code> - 1000万USDT\n"
            f"<code>/minvol 100M</code> - 1亿USDT\n"
            f"<code>/minvol off</code> - 关闭筛选",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    def _parse_volume_value(self, text: str) -> float:
        """解析成交额值，支持 K/M/B 后缀"""
        text = text.upper().strip()
        
        multipliers = {
            'K': 1_000,
            'M': 1_000_000,
            'B': 1_000_000_000,
        }
        
        for suffix, mult in multipliers.items():
            if text.endswith(suffix):
                return float(text[:-1]) * mult
        
        return float(text)
    
    def _get_volume_filter_keyboard(self, user_config):
        """成交额筛选键盘"""
        current = user_config.min_volume_24h
        enabled = user_config.volume_filter_enabled
        
        def check(val):
            return "✅ " if enabled and abs(current - val) < val * 0.01 else ""
        
        return [
            # 开关按钮
            [InlineKeyboardButton(
                f"{'🔴 关闭筛选' if enabled else '🟢 开启筛选'}",
                callback_data="toggle_volume_filter"
            )],
            # 常用档位 - 小额
            [
                InlineKeyboardButton(f"{check(1_000_000)}$1M", callback_data="minvol_1000000"),
                InlineKeyboardButton(f"{check(5_000_000)}$5M", callback_data="minvol_5000000"),
                InlineKeyboardButton(f"{check(10_000_000)}$10M", callback_data="minvol_10000000"),
            ],
            # 常用档位 - 中额
            [
                InlineKeyboardButton(f"{check(50_000_000)}$50M", callback_data="minvol_50000000"),
                InlineKeyboardButton(f"{check(100_000_000)}$100M", callback_data="minvol_100000000"),
                InlineKeyboardButton(f"{check(500_000_000)}$500M", callback_data="minvol_500000000"),
            ],
            # 常用档位 - 大额
            [
                InlineKeyboardButton(f"{check(1_000_000_000)}$1B", callback_data="minvol_1000000000"),
                InlineKeyboardButton(f"{check(5_000_000_000)}$5B", callback_data="minvol_5000000000"),
            ],
            [InlineKeyboardButton("◀️ 返回监控类型", callback_data="menu_switches")],
        ]
    
    async def _show_volume_filter_menu(self, message, user_config):
        """显示成交额筛选菜单"""
        keyboard = self._get_volume_filter_keyboard(user_config)
        
        await message.edit_text(
            f"💎 <b>24H成交额筛选</b>\n\n"
            f"<b>当前状态:</b> {'✅ 已开启' if user_config.volume_filter_enabled else '❌ 未开启'}\n"
            f"<b>最低成交额:</b> {user_config.get_volume_filter_display()}\n\n"
            f"💡 开启后，只有24小时成交额达到设定值的代币才会触发报警\n"
            f"适合过滤小币种，专注主流币\n\n"
            f"命令设置: <code>/minvol 10M</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # ================== 管理员命令 ==================
    async def _cmd_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        if not user_manager.is_admin(user_config.user_id):
            await update.message.reply_text("❌ 无权限")
            return
        
        users = user_manager.get_all_users()
        active = len([u for u in users if u.is_active])
        
        engine_stats = {}
        if self.system and hasattr(self.system, 'alert_engine'):
            engine_stats = self.system.alert_engine.get_stats()
        
        await update.message.reply_text(
            f"👑 <b>管理员面板</b>\n\n"
            f"<b>用户:</b> {len(users)} (活跃: {active})\n\n"
            f"<b>报警统计:</b>\n"
            f"• 总报警: {engine_stats.get('total_alerts', 0)}\n"
            f"• ⚡ 升级穿透: {engine_stats.get('escalation_count', 0)}\n"
            f"• 活跃冷却: {engine_stats.get('active_cooldowns', 0)}\n\n"
            f"/users - 用户列表\n"
            f"/broadcast 消息 - 广播",
            parse_mode=ParseMode.HTML
        )
    
    async def _cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        if not user_manager.is_admin(user_config.user_id):
            await update.message.reply_text("❌ 无权限")
            return
        
        users = user_manager.get_all_users()
        text = "👥 <b>用户列表</b>\n\n"
        
        for u in users[:20]:
            status = "✅" if u.is_active else "❌"
            admin = "👑" if u.is_admin else ""
            tz = f"UTC{u.timezone_offset:+d}"
            night = "🌙" if u.alert_mode.night.enabled else ""
            text += f"{status}{admin}{night} {u.username or u.user_id[:8]} ({tz})\n"
        
        if len(users) > 20:
            text += f"\n... 共 {len(users)} 个"
        
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    
    async def _cmd_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_config = self._get_user(update)
        if not user_manager.is_admin(user_config.user_id):
            await update.message.reply_text("❌ 无权限")
            return
        
        message = ' '.join(context.args or [])
        if not message:
            await update.message.reply_text("用法: /broadcast <消息>")
            return
        
        await self.notifier.broadcast(f"📢 <b>系统公告</b>\n\n{message}")
        await update.message.reply_text("✅ 广播已发送")
    
    # ================== 回调处理 ==================
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        
        user_config = self._get_user(update)
        data = query.data
        message = query.message
        
        try:
            # 先回应callback，避免超时（只调用一次）
            await query.answer()
            
            # ========== 确认报警 ==========
            if data.startswith("confirm_alert_"):
                alert_id = data.replace("confirm_alert_", "")
                if self.notifier.confirm_alert(user_config.user_id, alert_id):
                    pending = self.notifier.get_pending_count(user_config.user_id)
                    await query.edit_message_text(
                        f"✅ <b>报警已确认</b>\n\n"
                        f"报警ID: <code>{alert_id}</code>\n"
                        f"确认时间: {user_config.get_local_time_str()}\n\n"
                        f"📋 剩余待确认: {pending} 个",
                        parse_mode=ParseMode.HTML
                    )
                return
            
            if data == "confirm_all_alerts":
                count = self.notifier.confirm_all_alerts(user_config.user_id)
                await query.edit_message_text(
                    f"✅ <b>已确认全部报警</b>\n\n"
                    f"确认数量: {count} 个\n"
                    f"时间: {user_config.get_local_time_str()}",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # ========== 成交额筛选 ==========
            if data == "toggle_volume_filter":
                user_config.volume_filter_enabled = not user_config.volume_filter_enabled
                user_manager._save()
                user_config = user_manager.get_user(user_config.user_id)
                # 返回成交额筛选菜单
                await self._show_volume_filter_menu(message, user_config)
                return
            
            if data.startswith("minvol_"):
                value = float(data.replace("minvol_", ""))
                user_manager.set_volume_filter(user_config.user_id, True, value)
                user_config = user_manager.get_user(user_config.user_id)
                # 返回成交额筛选菜单
                await self._show_volume_filter_menu(message, user_config)
                return
            
            if data == "menu_volume_filter":
                await self._show_volume_filter_menu(message, user_config)
                return
            
            # ========== 静音代币 ==========
            if data.startswith("mute_symbol_"):
                parts = data.replace("mute_symbol_", "").rsplit("_", 1)
                symbol = parts[0]
                minutes = int(parts[1]) if len(parts) > 1 else 60
                name = symbol.replace('USDT', '')
                
                # 刷新用户配置
                user_config = user_manager.get_user(user_config.user_id)
                
                # 标准化symbol
                if not symbol.endswith('USDT'):
                    symbol += 'USDT'
                
                # 检查是否已经静音
                if symbol in user_config.blacklist:
                    # 已经静音 - 显示当前状态和取消选项
                    unmute_time = None
                    if user_config.user_id in self.muted_symbols:
                        unmute_time = self.muted_symbols[user_config.user_id].get(symbol)
                    
                    keyboard = [
                        [InlineKeyboardButton("🔊 取消静音", callback_data=f"unmute_symbol_{symbol}")],
                        [
                            InlineKeyboardButton("⏰ +1小时", callback_data=f"extend_mute_{symbol}_60"),
                            InlineKeyboardButton("⏰ +24小时", callback_data=f"extend_mute_{symbol}_1440"),
                        ],
                        [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
                    ]
                    
                    if unmute_time and unmute_time > datetime.now():
                        remaining = (unmute_time - datetime.now()).total_seconds()
                        remaining_hours = int(remaining / 3600)
                        remaining_min = int((remaining % 3600) / 60)
                        if remaining_hours > 0:
                            time_str = f"{remaining_hours}小时{remaining_min}分钟"
                        else:
                            time_str = f"{remaining_min}分钟"
                        
                        # 转换为用户时区
                        unmute_time_local = user_config.get_local_time(unmute_time)
                        
                        await query.edit_message_text(
                            f"🔇 <b>{name} 已在静音中</b>\n\n"
                            f"⏰ 剩余时间: <b>{time_str}</b>\n"
                            f"解除时间: {unmute_time_local.strftime('%H:%M:%S')}\n\n"
                            f"💡 静音期间不会收到该代币的任何报警",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        # 永久黑名单（非临时静音）
                        await query.edit_message_text(
                            f"🔇 <b>{name} 已在黑名单中</b>\n\n"
                            f"该代币不会收到任何报警\n\n"
                            f"💡 点击下方按钮取消静音\n"
                            f"或使用: /blacklist del {name}",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode=ParseMode.HTML
                        )
                    return
                
                # 执行静音 - 使用统一方法
                removed_count = self._mute_symbol_for_user(user_config.user_id, symbol, minutes)
                unmute_time = self.muted_symbols[user_config.user_id][symbol]
                
                # 转换为用户时区
                unmute_time_local = user_config.get_local_time(unmute_time)
                
                # 格式化时长显示
                if minutes >= 60:
                    duration_str = f"{minutes // 60} 小时"
                    if minutes % 60 > 0:
                        duration_str += f" {minutes % 60} 分钟"
                else:
                    duration_str = f"{minutes} 分钟"
                
                keyboard = [
                    [InlineKeyboardButton("🔊 立即取消静音", callback_data=f"unmute_symbol_{symbol}")],
                    [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
                ]
                
                removed_text = f"\n✅ 已停止 {removed_count} 个待处理提醒" if removed_count > 0 else ""
                
                await query.edit_message_text(
                    f"🔇 <b>{name} 已静音</b>\n\n"
                    f"⏰ 时长: {duration_str}\n"
                    f"解除时间: {unmute_time_local.strftime('%H:%M:%S')}{removed_text}\n\n"
                    f"• 静音期间不会收到该代币的报警\n"
                    f"• 到期后自动恢复并通知你",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                return
            
            # ========== 取消静音 ==========
            if data.startswith("unmute_symbol_"):
                symbol = data.replace("unmute_symbol_", "")
                name = symbol.replace('USDT', '')
                
                # 使用统一方法取消静音
                self._unmute_symbol_for_user(user_config.user_id, symbol)
                
                keyboard = [[InlineKeyboardButton("◀️ 返回", callback_data="back_menu")]]
                
                await query.edit_message_text(
                    f"🔊 <b>{name} 已取消静音</b>\n\n"
                    f"✅ 现在会正常接收该代币的报警\n"
                    f"时间: {user_config.get_local_time_str()}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                return
            
            # ========== 延长静音 ==========
            if data.startswith("extend_mute_"):
                parts = data.replace("extend_mute_", "").rsplit("_", 1)
                symbol = parts[0]
                minutes = int(parts[1]) if len(parts) > 1 else 60
                name = symbol.replace('USDT', '')
                
                # 标准化symbol
                if not symbol.endswith('USDT'):
                    symbol += 'USDT'
                
                # 延长静音时间
                if user_config.user_id not in self.muted_symbols:
                    self.muted_symbols[user_config.user_id] = {}
                
                current_time = self.muted_symbols[user_config.user_id].get(symbol, datetime.now())
                if current_time < datetime.now():
                    current_time = datetime.now()
                
                new_unmute_time = current_time + timedelta(minutes=minutes)
                self.muted_symbols[user_config.user_id][symbol] = new_unmute_time
                
                # 确保在黑名单中
                if symbol not in user_config.blacklist:
                    user_manager.add_to_blacklist(user_config.user_id, [symbol])
                
                # 转换为用户时区
                new_unmute_time_local = user_config.get_local_time(new_unmute_time)
                
                # 格式化延长时间
                if minutes >= 60:
                    extend_str = f"+{minutes // 60} 小时"
                else:
                    extend_str = f"+{minutes} 分钟"
                
                keyboard = [
                    [InlineKeyboardButton("🔊 取消静音", callback_data=f"unmute_symbol_{symbol}")],
                    [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
                ]
                
                await query.edit_message_text(
                    f"🔇 <b>{name} 静音已延长</b>\n\n"
                    f"⏰ 新的解除时间: {new_unmute_time_local.strftime('%H:%M:%S')}\n"
                    f"延长: {extend_str}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
                return
            
            # ========== 返回主菜单 ==========
            if data == "back_menu":
                await self._show_main_menu(message, user_config)
                return
            
            # ========== 时区设置 ==========
            if data.startswith("tz_"):
                parts = data.split("_", 2)
                offset = int(parts[1])
                name = parts[2] if len(parts) > 2 else f"UTC{offset:+d}"
                user_manager.set_timezone(user_config.user_id, offset, name)
                user_config = user_manager.get_user(user_config.user_id)
                await query.edit_message_text(
                    f"✅ 时区已设置为 <b>{name}</b>\n\n"
                    f"当前时间: {user_config.get_local_time_str()}",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # ========== 夜间模式 ==========
            if data == "toggle_night":
                night = user_config.alert_mode.night
                user_manager.set_night_mode(user_config.user_id, not night.enabled)
                user_config = user_manager.get_user(user_config.user_id)
                status = "开启" if user_config.alert_mode.night.enabled else "关闭"
                await query.edit_message_text(
                    f"✅ 夜间模式已{status}\n\n"
                    f"💡 夜间时段 ({user_config.alert_mode.night.night_start}-{user_config.alert_mode.night.night_end}) "
                    f"将自动使用重复提醒模式",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # 重复模式间隔设置
            if data.startswith("repeat_interval_"):
                interval = int(data.replace("repeat_interval_", ""))
                user_config.alert_mode.repeat.interval_seconds = interval
                user_manager._save()
                await query.edit_message_text(f"✅ 重复间隔: {interval} 秒")
                return
            
            # 重复模式次数设置
            if data.startswith("repeat_max_"):
                count = int(data.replace("repeat_max_", ""))
                user_config.alert_mode.repeat.max_repeats = count
                user_manager._save()
                await query.edit_message_text(f"✅ 最大重复: {count} 次")
                return
            
            if data.startswith("night_time_"):
                parts = data.replace("night_time_", "").split("_")
                if len(parts) == 2:
                    start = f"{parts[0]}:00"
                    end = f"{parts[1]}:00"
                    user_manager.set_night_time(user_config.user_id, start, end)
                    await query.edit_message_text(f"✅ 夜间时段: {start} - {end}")
                return
            
            if data.startswith("night_interval_"):
                interval = int(data.replace("night_interval_", ""))
                user_config.alert_mode.night.night_interval_seconds = interval
                user_manager._save()
                await query.edit_message_text(f"✅ 夜间重复间隔: {interval} 秒")
                return
            
            if data.startswith("night_max_"):
                count = int(data.replace("night_max_", ""))
                user_config.alert_mode.night.night_max_repeats = count
                user_manager._save()
                await query.edit_message_text(f"✅ 夜间最大重复: {count} 次")
                return
            
            if data == "toggle_night_email":
                user_config.alert_mode.night.night_add_email = not user_config.alert_mode.night.night_add_email
                user_manager._save()
                status = "开启" if user_config.alert_mode.night.night_add_email else "关闭"
                await query.edit_message_text(f"✅ 夜间自动加邮件: {status}")
                return
            
            # ========== 排行榜 ==========
            if data.startswith("rank_"):
                await self._handle_rank_callback(query, message, user_config, data)
                return
            
            # ========== 代币信息刷新 ==========
            if data.startswith("info_"):
                symbol = data.replace("info_", "")
                await self._show_token_info_edit(message, user_config, symbol)
                return
            
            # ========== 监控模式 ==========
            if data.startswith("watch_"):
                mode = data.replace("watch_", "")
                user_manager.set_watch_mode(user_config.user_id, mode)
                await query.edit_message_text(f"✅ 监控模式: <b>{mode}</b>", parse_mode=ParseMode.HTML)
                return
            
            # ========== 灵敏度 ==========
            if data.startswith("profile_"):
                profile = AlertProfile(data.replace("profile_", ""))
                user_manager.set_profile(user_config.user_id, profile)
                await query.edit_message_text(f"✅ 灵敏度: <b>{profile.value}</b>", parse_mode=ParseMode.HTML)
                return
            
            # ========== 报警模式 ==========
            if data.startswith("mode_"):
                mode = AlertMode(data.replace("mode_", ""))
                user_manager.set_alert_mode(user_config.user_id, mode)
                await query.edit_message_text(f"✅ 日间报警模式: <b>{mode.value}</b>", parse_mode=ParseMode.HTML)
                return
            
            # ========== 邮件 ==========
            if data == "toggle_email":
                user_config.email.enabled = not user_config.email.enabled
                if user_config.email.enabled:
                    if NotifyChannel.EMAIL not in user_config.notify_channels:
                        user_config.notify_channels.append(NotifyChannel.EMAIL)
                else:
                    if NotifyChannel.EMAIL in user_config.notify_channels:
                        user_config.notify_channels.remove(NotifyChannel.EMAIL)
                user_manager._save()
                status = "开启" if user_config.email.enabled else "关闭"
                await query.edit_message_text(f"✅ 邮件通知已{status}")
                return
            
            # ========== 开关 ==========
            if data.startswith("toggle_"):
                await self._handle_toggle(query, message, user_config, data)
                return
            
            # ========== 清空列表 ==========
            if data == "clear_whitelist":
                user_manager.update_user(user_config.user_id, whitelist=[])
                await query.edit_message_text("✅ 白名单已清空")
                return
            if data == "clear_blacklist":
                user_manager.update_user(user_config.user_id, blacklist=[])
                self.muted_symbols.pop(user_config.user_id, None)
                await query.edit_message_text("✅ 黑名单已清空")
                return
            
            # ========== 菜单导航 ==========
            await self._handle_menu_navigation(query, message, user_config, data)
            
        except BadRequest as e:
            error_msg = str(e)
            if "Message is not modified" in error_msg:
                logger.debug(f"消息未变化，忽略: {data}")
            elif "message to edit not found" in error_msg.lower():
                logger.debug(f"消息已删除: {data}")
            else:
                logger.error(f"回调处理错误 (BadRequest): {e}")
                
        except Exception as e:
            logger.error(f"回调处理错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
            try:
                await query.answer("操作失败", show_alert=True)
            except:
                pass
    
    async def _handle_rank_callback(self, query, message, user_config, data):
        if data == "rank_gainers_spot":
            await self._show_gainers(message, user_config, MarketType.SPOT, edit=True)
        elif data == "rank_gainers_futures":
            await self._show_gainers(message, user_config, MarketType.FUTURES, edit=True)
        elif data == "rank_losers_spot":
            await self._show_losers(message, user_config, MarketType.SPOT, edit=True)
        elif data == "rank_losers_futures":
            await self._show_losers(message, user_config, MarketType.FUTURES, edit=True)
        elif data == "rank_volume_spot":
            await self._show_volume_rank(message, user_config, MarketType.SPOT, edit=True)
        elif data == "rank_volume_futures":
            await self._show_volume_rank(message, user_config, MarketType.FUTURES, edit=True)
        elif data == "rank_spread":
            await self._show_spread_rank(message, user_config, edit=True)
        elif data == "rank_funding_pos":
            await self._show_funding_rank(message, user_config, positive=True, edit=True)
        elif data == "rank_funding_neg":
            await self._show_funding_rank(message, user_config, positive=False, edit=True)
        elif data == "noop":
            # 分隔线按钮，不做任何操作
            pass
    
    async def _handle_toggle(self, query, message, user_config, data):
        toggles = {
            "toggle_spot": ("enable_spot", "现货报警"),
            "toggle_futures": ("enable_futures", "合约报警"),
            "toggle_spread": ("enable_spread", "差价报警"),
            "toggle_volume": ("enable_volume", "成交量报警"),
            "toggle_funding": ("enable_funding", "资金费率报警"),
            "toggle_big_order": ("enable_big_order", "巨量挂单报警"),  # 新增
        }
        
        if data in toggles:
            attr, name = toggles[data]
            current = getattr(user_config, attr)
            setattr(user_config, attr, not current)
            user_manager._save()
            user_config = user_manager.get_user(user_config.user_id)
            await self._show_switches_menu(message, user_config)
    
    async def _handle_menu_navigation(self, query, message, user_config, data):
        if data == "menu_watch":
            await self._show_watch_menu(message, user_config)
        elif data == "menu_profile":
            await self._show_profile_menu(message, user_config)
        elif data == "menu_mode":
            await self._show_mode_menu(message, user_config)
        elif data == "menu_night":
            await self._show_night_menu(message, user_config)
        elif data == "menu_email":
            await self._show_email_menu(message, user_config)
        elif data == "menu_switches":
            await self._show_switches_menu(message, user_config)
        elif data == "menu_timezone":
            await self._show_timezone_menu(message, user_config)
        elif data == "menu_whitelist":
            await self._show_list_menu(message, user_config, "whitelist")
        elif data == "menu_blacklist":
            await self._show_list_menu(message, user_config, "blacklist")
        elif data == "menu_rank":
            await self._show_rank_menu(message, user_config)
        elif data == "menu_pending":
            await self._show_pending_menu(message, user_config)
    
    async def _show_token_info_edit(self, message, user_config, symbol):
        if not self.system:
            return
        
        spot_info = self.system.binance.get_token_info(symbol, MarketType.SPOT)
        futures_info = self.system.binance.get_token_info(symbol, MarketType.FUTURES)
        
        if not spot_info and not futures_info:
            await message.edit_text(f"❌ 未找到: {symbol}")
            return
        
        name = symbol.replace('USDT', '')
        text = f"💎 <b>{name} / USDT</b>\n\n"
        
        if spot_info:
            change_emoji = "📈" if spot_info.price_change_percent_24h > 0 else "📉"
            text += f"<b>📈 现货</b>\n"
            text += f"价格: {self._format_price(spot_info.price)}\n"
            text += f"24h: {change_emoji} {spot_info.price_change_percent_24h:+.2f}%\n"
            text += f"最高: {self._format_price(spot_info.high_24h)}\n"
            text += f"最低: {self._format_price(spot_info.low_24h)}\n"
            text += f"成交额: {spot_info.volume_display}\n\n"
        
        if futures_info:
            funding = self.system.binance.funding_rates.get(symbol, 0)
            text += f"<b>📊 合约</b>\n"
            text += f"价格: {self._format_price(futures_info.price)}\n"
            text += f"费率: {funding:+.4f}%\n"
            
            if spot_info:
                spread = ((futures_info.price - spot_info.price) / spot_info.price) * 100
                text += f"差价: {spread:+.2f}%\n"
        
        text += f"\n⏰ {user_config.get_local_time_str()}"
        
        keyboard = [[InlineKeyboardButton("🔄 刷新", callback_data=f"info_{symbol}")]]
        
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    # ================== 菜单显示 ==================
    
    async def _show_main_menu(self, message, user_config):
        keyboard = self._get_main_menu_keyboard()
        pending = self.notifier.get_pending_count(user_config.user_id)
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        
        await message.edit_text(
            f"🦅 <b>鹰眼控制面板</b>\n\n"
            f"<b>监控:</b> {user_config.watch_mode}\n"
            f"<b>灵敏度:</b> {user_config.profile.value}\n"
            f"<b>报警模式:</b> {effective_mode.value} {'🌙' if is_night else ''}\n"
            f"<b>夜间模式:</b> {'✅' if user_config.alert_mode.night.enabled else '❌'}\n"
            f"<b>时区:</b> {user_config.timezone_name}\n"
            f"<b>待确认:</b> {pending}\n\n"
            f"⏰ {user_config.get_local_time_str()}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_watch_menu(self, message, user_config):
        keyboard = self._get_watch_keyboard(user_config)
        await message.edit_text(
            self._get_watch_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_profile_menu(self, message, user_config):
        keyboard = self._get_profile_keyboard(user_config)
        await message.edit_text(
            self._get_profile_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_mode_menu(self, message, user_config):
        keyboard = self._get_mode_keyboard(user_config)
        await message.edit_text(
            self._get_mode_text(user_config),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_night_menu(self, message, user_config):
        keyboard = self._get_night_keyboard(user_config)
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        night = user_config.alert_mode.night
        
        await message.edit_text(
            f"🌙 <b>夜间模式设置</b>\n\n"
            f"<b>当前状态:</b>\n"
            f"• 夜间模式: {'✅ 已开启' if night.enabled else '❌ 未开启'}\n"
            f"• 当前时段: {'🌙 夜间' if is_night else '☀️ 日间'}\n"
            f"• 生效模式: <b>{effective_mode.value}</b>\n\n"
            f"<b>夜间时段:</b> {night.night_start} - {night.night_end}\n"
            f"<b>重复间隔:</b> {night.night_interval_seconds} 秒\n"
            f"<b>最大重复:</b> {night.night_max_repeats} 次\n"
            f"<b>夜间加邮件:</b> {'✅' if night.night_add_email else '❌'}\n\n"
            f"💡 夜间模式开启后，在夜间时段会自动切换为<b>重复提醒</b>模式\n\n"
            f"⏰ {user_config.get_local_time_str()}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_email_menu(self, message, user_config):
        keyboard = self._get_email_keyboard(user_config)
        emails = ', '.join(user_config.email.to_addresses) or '未设置'
        channels = [c.value for c in user_config.notify_channels]
        
        await message.edit_text(
            f"📧 <b>邮件设置</b>\n\n"
            f"状态: {'✅ 已启用' if user_config.email.enabled else '❌ 未启用'}\n"
            f"邮箱: {emails}\n"
            f"通知渠道: {channels}\n\n"
            f"设置: <code>/email your@email.com</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_timezone_menu(self, message, user_config):
        keyboard = []
        row = []
        for name, offset in TIMEZONE_PRESETS.items():
            check = "✅" if user_config.timezone_offset == offset else ""
            btn = InlineKeyboardButton(f"{check}{name}", callback_data=f"tz_{offset}_{name}")
            row.append(btn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("◀️ 返回", callback_data="back_menu")])
        
        await message.edit_text(
            f"🌍 <b>时区设置</b>\n\n"
            f"当前: <b>{user_config.timezone_name}</b>\n"
            f"时间: {user_config.get_local_time_str()}\n\n"
            f"选择时区或输入: <code>/tz 8</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_switches_menu(self, message, user_config):
        keyboard = [
            [InlineKeyboardButton(f"{'✅' if user_config.enable_spot else '⬜'} 现货报警", callback_data="toggle_spot")],
            [InlineKeyboardButton(f"{'✅' if user_config.enable_futures else '⬜'} 合约报警", callback_data="toggle_futures")],
            [InlineKeyboardButton(f"{'✅' if user_config.enable_spread else '⬜'} 差价报警", callback_data="toggle_spread")],
            [InlineKeyboardButton(f"{'✅' if user_config.enable_volume else '⬜'} 成交量异动", callback_data="toggle_volume")],
            [InlineKeyboardButton(f"{'✅' if user_config.enable_funding else '⬜'} 资金费率", callback_data="toggle_funding")],
            [InlineKeyboardButton(f"{'✅' if user_config.enable_big_order else '⬜'} 🐋 巨量挂单", callback_data="toggle_big_order")],
            [InlineKeyboardButton(
                f"💎 成交额筛选: {user_config.get_volume_filter_display()}", 
                callback_data="menu_volume_filter"
            )],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
        
        await message.edit_text(
            f"🎚️ <b>监控类型设置</b>\n\n"
            f"<b>报警开关:</b>\n"
            f"• 现货: {'✅' if user_config.enable_spot else '❌'}\n"
            f"• 合约: {'✅' if user_config.enable_futures else '❌'}\n"
            f"• 差价: {'✅' if user_config.enable_spread else '❌'}\n"
            f"• 成交量异动: {'✅' if user_config.enable_volume else '❌'}\n"
            f"• 资金费率: {'✅' if user_config.enable_funding else '❌'}\n"
            f"• 🐋 巨量挂单: {'✅' if user_config.enable_big_order else '❌'}\n\n"
            f"<b>成交额筛选:</b> {user_config.get_volume_filter_display()}\n"
            f"{'💡 已开启，仅监控大成交额代币' if user_config.volume_filter_enabled else '💡 未开启，监控所有代币'}\n\n"
            f"<b>💡 巨量挂单说明:</b>\n"
            f"检测订单簿中的超大额买/卖挂单\n"
            f"• 小市值币: ≥$500K 或占24h成交额20%\n"
            f"• 大市值币: ≥$5M 或占24h成交额5%",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_volume_filter_menu(self, message, user_config):
        """显示成交额筛选菜单"""
        keyboard = self._get_volume_filter_keyboard(user_config)
        
        await message.edit_text(
            f"💎 <b>24H成交额筛选</b>\n\n"
            f"<b>当前状态:</b> {'✅ 已开启' if user_config.volume_filter_enabled else '❌ 未开启'}\n"
            f"<b>最低成交额:</b> {user_config.get_volume_filter_display()}\n\n"
            f"💡 开启后，只有24小时成交额达到设定值的代币才会触发报警\n"
            f"适合过滤小币种，专注主流币",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_list_menu(self, message, user_config, list_type):
        items = user_config.whitelist if list_type == "whitelist" else user_config.blacklist
        title = "✅ 白名单" if list_type == "whitelist" else "🚫 黑名单"
        clear_data = f"clear_{list_type}"
        cmd = list_type
        
        items_text = ', '.join(items[:20]) if items else "空"
        if len(items) > 20:
            items_text += f" (+{len(items)-20})"
        
        keyboard = [
            [InlineKeyboardButton("🗑️ 清空", callback_data=clear_data)],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
        
        await message.edit_text(
            f"{title}\n\n{items_text}\n\n"
            f"<code>/{cmd} add BTC ETH</code>\n"
            f"<code>/{cmd} del BTC</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_rank_menu(self, message, user_config):
        keyboard = [
            # 现货
            [InlineKeyboardButton("━━━ 📈 现货 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("🟢 涨幅", callback_data="rank_gainers_spot"),
                InlineKeyboardButton("🔴 跌幅", callback_data="rank_losers_spot"),
                InlineKeyboardButton("💰 成交额", callback_data="rank_volume_spot"),
            ],
            # 合约
            [InlineKeyboardButton("━━━ 📊 合约 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("🟢 涨幅", callback_data="rank_gainers_futures"),
                InlineKeyboardButton("🔴 跌幅", callback_data="rank_losers_futures"),
                InlineKeyboardButton("💰 成交额", callback_data="rank_volume_futures"),
            ],
            # 期现数据
            [InlineKeyboardButton("━━━ 📐 期现 ━━━", callback_data="noop")],
            [
                InlineKeyboardButton("📐 差价", callback_data="rank_spread"),
                InlineKeyboardButton("📈 费率+", callback_data="rank_funding_pos"),
                InlineKeyboardButton("📉 费率-", callback_data="rank_funding_neg"),
            ],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
        
        await message.edit_text(
            f"📊 <b>实时排行榜</b>\n\n"
            f"📈 现货 - Binance现货市场\n"
            f"📊 合约 - Binance U本位合约\n"
            f"📐 期现 - 现货合约对比数据\n\n"
            f"⏰ {user_config.get_local_time_str()}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    async def _show_pending_menu(self, message, user_config):
        """显示待确认报警菜单"""
        pending = self.notifier.get_user_pending(user_config.user_id)
        
        if not pending:
            keyboard = [[InlineKeyboardButton("◀️ 返回", callback_data="back_menu")]]
            await message.edit_text(
                "✅ 没有待确认的报警",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
            return
        
        text = f"🔔 <b>待确认报警 ({len(pending)})</b>\n\n"
        
        for alert_id, alert in list(pending.items())[:5]:
            text += f"• <code>{alert_id}</code> {alert.symbol}\n"
            text += f"  {alert.message[:20]}... (已发{alert.sent_count}次)\n\n"
        
        if len(pending) > 5:
            text += f"... 还有 {len(pending) - 5} 个\n"
        
        keyboard = [
            [InlineKeyboardButton("✅ 确认全部", callback_data="confirm_all_alerts")],
        ]
        
        for alert_id, alert in list(pending.items())[:3]:
            name = alert.symbol.replace('USDT', '')
            keyboard.append([
                InlineKeyboardButton(
                    f"✅ 确认 {name}", 
                    callback_data=f"confirm_alert_{alert_id}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("◀️ 返回", callback_data="back_menu")])
        
        await message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # ================== 辅助方法 ==================
    
    def _parse_symbols(self, args):
        symbols = []
        for s in args:
            s = s.upper().strip()
            if not s.endswith('USDT'):
                s += 'USDT'
            symbols.append(s)
        return symbols
    
    def _get_main_menu_keyboard(self):
        return [
            [
                InlineKeyboardButton("📊 排行榜", callback_data="menu_rank"),
                InlineKeyboardButton("🔔 待确认", callback_data="menu_pending"),
            ],
            [
                InlineKeyboardButton("🌍 时区", callback_data="menu_timezone"),
                InlineKeyboardButton("👁️ 监控", callback_data="menu_watch"),
            ],
            [
                InlineKeyboardButton("🎯 灵敏度", callback_data="menu_profile"),
                InlineKeyboardButton("🔔 模式", callback_data="menu_mode"),
            ],
            [
                InlineKeyboardButton("✅ 白名单", callback_data="menu_whitelist"),
                InlineKeyboardButton("🚫 黑名单", callback_data="menu_blacklist"),
            ],
            [
                InlineKeyboardButton("🌙 夜间模式", callback_data="menu_night"),
                InlineKeyboardButton("📧 邮件", callback_data="menu_email"),
            ],
            [
                InlineKeyboardButton("⚙️️ 监控类型", callback_data="menu_switches"),
            ],
        ]
    
    def _get_watch_keyboard(self, user_config):
        return [
            [InlineKeyboardButton(f"{'✅' if user_config.watch_mode == 'all' else '⬜'} 全部", callback_data="watch_all")],
            [InlineKeyboardButton(f"{'✅' if user_config.watch_mode == 'whitelist' else '⬜'} 仅白名单", callback_data="watch_whitelist")],
            [InlineKeyboardButton(f"{'✅' if user_config.watch_mode == 'blacklist' else '⬜'} 排除黑名单", callback_data="watch_blacklist")],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
    
    def _get_profile_keyboard(self, user_config):
        return [
            [InlineKeyboardButton(f"{'✅' if user_config.profile == AlertProfile.CONSERVATIVE else '⬜'} 🟢 保守", callback_data="profile_conservative")],
            [InlineKeyboardButton(f"{'✅' if user_config.profile == AlertProfile.MODERATE else '⬜'} 🟡 适中", callback_data="profile_moderate")],
            [InlineKeyboardButton(f"{'✅' if user_config.profile == AlertProfile.AGGRESSIVE else '⬜'} 🔴 激进", callback_data="profile_aggressive")],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
    
    def _get_email_keyboard(self, user_config):
        return [
            [InlineKeyboardButton(f"{'🔴 关闭' if user_config.email.enabled else '🟢 开启'} 邮件", callback_data="toggle_email")],
            [InlineKeyboardButton("◀️ 返回", callback_data="back_menu")],
        ]
    
    def _get_watch_text(self, user_config):
        return f"""
👁️ <b>监控设置</b>

当前: <b>{user_config.watch_mode}</b>

• 全部 - 监控所有代币
• 仅白名单 - 只监控 {len(user_config.whitelist)} 个
• 排除黑名单 - 排除 {len(user_config.blacklist)} 个
"""
    
    def _get_profile_text(self, user_config):
        return f"""
🎯 <b>灵敏度设置</b>

当前: <b>{user_config.profile.value}</b>

🟢 <b>保守</b> - ±10% (适合长持)
• 报警较少，不打扰
• 冷却时间 10分钟

🟡 <b>适中</b> - ±6% (平衡) ⭐推荐
• 报警适中
• 冷却时间 5分钟

🔴 <b>激进</b> - ±3.5% (活跃交易)
• 报警频繁
• 冷却时间 2分钟

💡 所有配置都支持<b>升级穿透</b>：级别升级时立即报警
"""
    
    def _get_mode_text(self, user_config):
        is_night = user_config.is_night_time()
        effective_mode = user_config.get_effective_mode()
        night = user_config.alert_mode.night
        
        return f"""
🔔 <b>报警模式设置</b>

<b>日间模式:</b> {user_config.alert_mode.mode.value}
<b>当前生效:</b> {effective_mode.value} {'🌙' if is_night else '☀️'}

📢 <b>单次报警</b>
• 每个报警只发送一次
• 适合经常看手机的用户

🔁 <b>重复提醒</b>
• 每隔一段时间重复发送
• 直到你确认收到为止
• 适合需要确保不错过的场景

💡 开启<b>夜间模式</b>后，在夜间时段会自动切换为重复提醒
夜间模式: {'✅ 已开启' if night.enabled else '❌ 未开启'}
"""
    
    def _get_list_text(self, user_config, list_type):
        items = user_config.whitelist if list_type == "whitelist" else user_config.blacklist
        title = "✅ 白名单" if list_type == "whitelist" else "🚫 黑名单"
        cmd = list_type
        
        items_text = ', '.join(items) if items else "空"
        
        return f"""
{title}

{items_text}

<code>/{cmd} add BTC ETH</code>
<code>/{cmd} del BTC</code>
<code>/{cmd} clear</code>
"""