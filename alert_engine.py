"""
报警引擎 - 多用户版本（性能优化版 + 巨量挂单检测）
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, List, Tuple
from collections import defaultdict
from loguru import logger

from models import (
    TickerData, SpreadData, OrderBookData, Alert, AlertType, AlertLevel,
    MarketType, AlertStatus
)
from config import user_manager, UserConfig


class AlertEngine:
    """多用户报警引擎 - 支持报警升级穿透 + 巨量挂单"""
    
    def __init__(self):
        # 每用户的冷却记录: {user_id: {symbol: {alert_type: (last_time, last_level)}}}
        self.cooldowns: Dict[str, Dict[str, Dict[AlertType, Tuple[datetime, AlertLevel]]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        
        # 报警回调
        self.on_alert: Optional[Callable] = None
        
        # Binance客户端引用
        self.binance = None
        
        # 统计
        self.total_alerts = 0
        self.escalation_count = 0
        self.big_order_alerts = 0
        
        # 用户缓存
        self._cached_users: List[UserConfig] = []
        self._users_cache_time: Optional[datetime] = None
        self._cache_ttl = 30
        
        # 处理统计
        self._check_count = 0
        self._last_stats_time = datetime.now()
        
        # 触发报警的symbol列表，用于后续检查订单簿
        self._triggered_symbols: Dict[str, datetime] = {}
    
    def _get_cached_users(self) -> List[UserConfig]:
        """获取缓存的活跃用户列表"""
        now = datetime.now()
        
        if (self._users_cache_time is None or 
            (now - self._users_cache_time).total_seconds() > self._cache_ttl):
            self._cached_users = user_manager.get_active_users()
            self._users_cache_time = now
            logger.debug(f"刷新用户缓存: {len(self._cached_users)} 个活跃用户")
        
        return self._cached_users
    
    def invalidate_user_cache(self):
        """使用户缓存失效"""
        self._users_cache_time = None
    
    def _check_cooldown_and_escalation(
        self, 
        user_id: str, 
        symbol: str, 
        alert_type: AlertType,
        current_level: AlertLevel,
        cooldown_seconds: int
    ) -> Tuple[bool, bool]:
        """检查冷却和升级状态"""
        if user_id not in self.cooldowns:
            return True, False
        
        if symbol not in self.cooldowns[user_id]:
            return True, False
        
        if alert_type not in self.cooldowns[user_id][symbol]:
            return True, False
        
        last_time, last_level = self.cooldowns[user_id][symbol][alert_type]
        in_cooldown = datetime.now() - last_time < timedelta(seconds=cooldown_seconds)
        
        if not in_cooldown:
            return True, False
        
        if current_level.priority > last_level.priority:
            logger.info(
                f"🚨 升级穿透: {symbol} {alert_type.value} "
                f"{last_level.name}({last_level.priority}) -> {current_level.name}({current_level.priority})"
            )
            self.escalation_count += 1
            return True, True
        
        return False, False
    
    def _set_cooldown(self, user_id: str, symbol: str, alert_type: AlertType, 
                      level: AlertLevel):
        """设置冷却"""
        self.cooldowns[user_id][symbol][alert_type] = (datetime.now(), level)
    
    def _get_price_level(self, change: float) -> AlertLevel:
        """根据涨跌幅获取报警级别"""
        abs_change = abs(change)
        if abs_change >= 20:
            return AlertLevel.EXTREME
        elif abs_change >= 10:
            return AlertLevel.CRITICAL
        elif abs_change >= 5:
            return AlertLevel.WARNING
        return AlertLevel.INFO
    
    def _get_spread_level(self, spread_percent: float) -> AlertLevel:
        """根据差价获取报警级别"""
        abs_spread = abs(spread_percent)
        if abs_spread >= 5:
            return AlertLevel.EXTREME
        elif abs_spread >= 3:
            return AlertLevel.CRITICAL
        elif abs_spread >= 1.5:
            return AlertLevel.WARNING
        return AlertLevel.INFO
    
    def _get_funding_level(self, funding_rate: float) -> AlertLevel:
        """根据资金费率获取报警级别"""
        abs_rate = abs(funding_rate)
        if abs_rate >= 0.5:
            return AlertLevel.EXTREME
        elif abs_rate >= 0.3:
            return AlertLevel.CRITICAL
        elif abs_rate >= 0.1:
            return AlertLevel.WARNING
        return AlertLevel.INFO
    
    def _get_volume_level(self, ratio: float) -> AlertLevel:
        """根据成交量倍数获取报警级别"""
        if ratio >= 50:
            return AlertLevel.EXTREME
        elif ratio >= 20:
            return AlertLevel.CRITICAL
        elif ratio >= 10:
            return AlertLevel.WARNING
        return AlertLevel.INFO
    
    def _get_big_order_level(self, order_value: float, volume_24h: float) -> AlertLevel:
        """根据巨量挂单获取报警级别（阈值提高10倍）"""
        if volume_24h <= 0:
            ratio = 0
        else:
            ratio = (order_value / volume_24h) * 100
        
        # 根据占比判断级别（阈值提高10倍）
        if ratio >= 50 or order_value >= 50_000_000:  # 占50%以上或超过5000万
            return AlertLevel.EXTREME
        elif ratio >= 20 or order_value >= 20_000_000:  # 占20%以上或超过2000万
            return AlertLevel.CRITICAL
        elif ratio >= 10 or order_value >= 5_000_000:  # 占10%以上或超过500万
            return AlertLevel.WARNING
        return AlertLevel.INFO
    
    async def check_ticker_for_all_users(self, ticker: TickerData):
        """检查行情并为所有用户生成报警"""
        self._check_count += 1
        
        now = datetime.now()
        if (now - self._last_stats_time).total_seconds() >= 60:
            users = self._get_cached_users()
            logger.info(
                f"📊 报警引擎: 检查={self._check_count}次/分, "
                f"用户={len(users)}, 报警={self.total_alerts}, "
                f"巨量挂单={self.big_order_alerts}"
            )
            self._check_count = 0
            self._last_stats_time = now
        
        users = self._get_cached_users()
        
        if not users:
            return
        
        tasks = [
            self._check_ticker_for_user(ticker, user) 
            for user in users
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 如果有用户触发了报警，记录symbol用于后续订单簿检查
        for result in results:
            if result is True:  # 触发了报警
                self._triggered_symbols[ticker.symbol] = now
                # 触发订单簿检查
                if self.binance:
                    self.binance.queue_orderbook_check(ticker.symbol, ticker.market_type)
                break
    
    async def _check_ticker_for_user(self, ticker: TickerData, user_config: UserConfig) -> bool:
        """为单个用户检查行情，返回是否触发了报警"""
        triggered = False
        
        try:
            if not user_config.should_monitor(ticker.symbol):
                return False
            
            if ticker.market_type == MarketType.SPOT and not user_config.enable_spot:
                return False
            if ticker.market_type == MarketType.FUTURES and not user_config.enable_futures:
                return False
            
            if not user_config.should_monitor_by_volume(ticker.quote_volume_24h):
                return False
            
            price_config = user_config.price
            user_id = user_config.user_id
            
            pump_alerts = []
            dump_alerts = []
            
            checks = [
                (ticker.price_change_1m, price_config.short_1m_pump, price_config.short_1m_dump, "1分钟"),
                (ticker.price_change_5m, price_config.mid_5m_pump, price_config.mid_5m_dump, "5分钟"),
                (ticker.price_change_15m, price_config.long_15m_pump, price_config.long_15m_dump, "15分钟"),
                (ticker.price_change_1h, price_config.hourly_pump, price_config.hourly_dump, "1小时"),
            ]
            
            for change, pump_threshold, dump_threshold, period in checks:
                if change >= pump_threshold:
                    level = self._get_price_level(change)
                    pump_alerts.append((change, period, level))
                elif change <= dump_threshold:
                    level = self._get_price_level(change)
                    dump_alerts.append((change, period, level))
            
            if pump_alerts:
                pump_alerts.sort(key=lambda x: x[2].priority, reverse=True)
                change, period, level = pump_alerts[0]
                
                should_send, is_escalation = self._check_cooldown_and_escalation(
                    user_id, ticker.symbol, AlertType.PRICE_PUMP, level, 
                    user_config.cooldown_seconds
                )
                
                if should_send:
                    self._set_cooldown(user_id, ticker.symbol, AlertType.PRICE_PUMP, level)
                    await self._create_price_alert(
                        ticker, user_config, AlertType.PRICE_PUMP, 
                        change, period, level, is_escalation
                    )
                    triggered = True
            
            if dump_alerts:
                dump_alerts.sort(key=lambda x: x[2].priority, reverse=True)
                change, period, level = dump_alerts[0]
                
                should_send, is_escalation = self._check_cooldown_and_escalation(
                    user_id, ticker.symbol, AlertType.PRICE_DUMP, level,
                    user_config.cooldown_seconds
                )
                
                if should_send:
                    self._set_cooldown(user_id, ticker.symbol, AlertType.PRICE_DUMP, level)
                    await self._create_price_alert(
                        ticker, user_config, AlertType.PRICE_DUMP,
                        change, period, level, is_escalation
                    )
                    triggered = True
            
            if user_config.enable_volume:
                if ticker.volume_change_ratio >= user_config.volume.spike_ratio:
                    level = self._get_volume_level(ticker.volume_change_ratio)
                    
                    should_send, is_escalation = self._check_cooldown_and_escalation(
                        user_id, ticker.symbol, AlertType.VOLUME_SPIKE, level,
                        user_config.cooldown_seconds
                    )
                    
                    if should_send:
                        self._set_cooldown(user_id, ticker.symbol, AlertType.VOLUME_SPIKE, level)
                        await self._create_volume_alert(ticker, user_config, level, is_escalation)
                        triggered = True
            
            return triggered
        
        except Exception as e:
            logger.error(f"检查用户 {user_config.user_id} 报警失败: {e}")
            return False
    
    async def check_orderbook_for_all_users(self, orderbook: OrderBookData):
        """检查订单簿并为所有用户生成巨量挂单报警"""
        users = self._get_cached_users()
        
        if not users:
            return
        
        tasks = [
            self._check_orderbook_for_user(orderbook, user)
            for user in users
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _check_orderbook_for_user(self, orderbook: OrderBookData, user_config: UserConfig):
        """为单个用户检查订单簿"""
        try:
            if not user_config.enable_big_order:
                return
            
            if not user_config.should_monitor(orderbook.symbol):
                return
            
            if orderbook.market_type == MarketType.SPOT and not user_config.enable_spot:
                return
            if orderbook.market_type == MarketType.FUTURES and not user_config.enable_futures:
                return
            
            # 获取24h成交额
            volume_24h = 0
            if orderbook.market_type == MarketType.SPOT:
                data_24h = self._get_24h_data(orderbook.symbol)
                volume_24h = data_24h.get('quote_volume', 0)
            else:
                if self.binance:
                    futures_24h = self.binance.futures_24h.get(orderbook.symbol, {})
                    volume_24h = futures_24h.get('quote_volume', 0)
            
            if not user_config.should_monitor_by_volume(volume_24h):
                return
            
            big_order_config = user_config.big_order
            user_id = user_config.user_id
            
            # 获取当前价格
            if orderbook.market_type == MarketType.SPOT:
                current_price = self.binance.spot_prices.get(orderbook.symbol, 0) if self.binance else 0
            else:
                current_price = self.binance.futures_prices.get(orderbook.symbol, 0) if self.binance else 0
            
            if current_price <= 0:
                return
            
            # 检查买单巨量
            if orderbook.max_bid_order > 0:
                if big_order_config.is_big_order(orderbook.max_bid_order, volume_24h):
                    # 检查价格偏离
                    price_diff = ((current_price - orderbook.max_bid_price) / current_price) * 100
                    
                    if abs(price_diff) <= big_order_config.max_price_deviation:
                        level = self._get_big_order_level(orderbook.max_bid_order, volume_24h)
                        
                        should_send, is_escalation = self._check_cooldown_and_escalation(
                            user_id, orderbook.symbol, AlertType.BIG_BID_ORDER, level,
                            user_config.cooldown_seconds
                        )
                        
                        if should_send:
                            self._set_cooldown(user_id, orderbook.symbol, AlertType.BIG_BID_ORDER, level)
                            await self._create_big_order_alert(
                                orderbook, user_config, AlertType.BIG_BID_ORDER,
                                orderbook.max_bid_order, orderbook.max_bid_price,
                                current_price, volume_24h, level, is_escalation
                            )
            
            # 检查卖单巨量
            if orderbook.max_ask_order > 0:
                if big_order_config.is_big_order(orderbook.max_ask_order, volume_24h):
                    price_diff = ((orderbook.max_ask_price - current_price) / current_price) * 100
                    
                    if abs(price_diff) <= big_order_config.max_price_deviation:
                        level = self._get_big_order_level(orderbook.max_ask_order, volume_24h)
                        
                        should_send, is_escalation = self._check_cooldown_and_escalation(
                            user_id, orderbook.symbol, AlertType.BIG_ASK_ORDER, level,
                            user_config.cooldown_seconds
                        )
                        
                        if should_send:
                            self._set_cooldown(user_id, orderbook.symbol, AlertType.BIG_ASK_ORDER, level)
                            await self._create_big_order_alert(
                                orderbook, user_config, AlertType.BIG_ASK_ORDER,
                                orderbook.max_ask_order, orderbook.max_ask_price,
                                current_price, volume_24h, level, is_escalation
                            )
        
        except Exception as e:
            logger.error(f"检查用户 {user_config.user_id} 订单簿报警失败: {e}")
    
    async def check_spread_for_all_users(self, spread: SpreadData):
        """检查差价并为所有用户生成报警"""
        users = self._get_cached_users()
        
        tasks = [
            self._check_spread_for_user(spread, user) 
            for user in users
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _check_spread_for_user(self, spread: SpreadData, user_config: UserConfig):
        """为单个用户检查差价"""
        try:
            if not user_config.should_monitor(spread.symbol):
                return
            
            if not user_config.enable_spread:
                return
            
            spot_24h = self._get_24h_data(spread.symbol)
            volume_24h = spot_24h.get('quote_volume', 0)
            if not user_config.should_monitor_by_volume(volume_24h):
                return
            
            user_id = user_config.user_id
            spread_config = user_config.spread
            
            if abs(spread.spread_percent) >= spread_config.spot_futures:
                alert_type = AlertType.SPREAD_HIGH if spread.spread_percent > 0 else AlertType.SPREAD_LOW
                level = self._get_spread_level(spread.spread_percent)
                
                should_send, is_escalation = self._check_cooldown_and_escalation(
                    user_id, spread.symbol, alert_type, level,
                    user_config.cooldown_seconds
                )
                
                if should_send:
                    self._set_cooldown(user_id, spread.symbol, alert_type, level)
                    await self._create_spread_alert(spread, user_config, alert_type, level, is_escalation)
            
            if user_config.enable_funding:
                if spread.funding_rate >= spread_config.funding_high:
                    level = self._get_funding_level(spread.funding_rate)
                    
                    should_send, is_escalation = self._check_cooldown_and_escalation(
                        user_id, spread.symbol, AlertType.FUNDING_HIGH, level,
                        user_config.cooldown_seconds
                    )
                    
                    if should_send:
                        self._set_cooldown(user_id, spread.symbol, AlertType.FUNDING_HIGH, level)
                        await self._create_funding_alert(spread, user_config, AlertType.FUNDING_HIGH, level, is_escalation)
                
                elif spread.funding_rate <= spread_config.funding_low:
                    level = self._get_funding_level(spread.funding_rate)
                    
                    should_send, is_escalation = self._check_cooldown_and_escalation(
                        user_id, spread.symbol, AlertType.FUNDING_LOW, level,
                        user_config.cooldown_seconds
                    )
                    
                    if should_send:
                        self._set_cooldown(user_id, spread.symbol, AlertType.FUNDING_LOW, level)
                        await self._create_funding_alert(spread, user_config, AlertType.FUNDING_LOW, level, is_escalation)
        
        except Exception as e:
            logger.error(f"检查用户 {user_config.user_id} 差价报警失败: {e}")
    
    def _get_24h_data(self, symbol: str) -> dict:
        """获取24h数据"""
        if self.binance:
            return self.binance.spot_24h.get(symbol, {})
        return {}
    
    async def _create_price_alert(self, ticker: TickerData, user_config: UserConfig,
                                   alert_type: AlertType, change: float, period: str,
                                   level: AlertLevel, is_escalation: bool = False):
        """创建价格报警"""
        direction = "暴涨" if alert_type == AlertType.PRICE_PUMP else "暴跌"
        escalation_prefix = "⚡升级 " if is_escalation else ""
        
        alert = Alert(
            alert_type=alert_type,
            level=level,
            symbol=ticker.symbol,
            market_type=ticker.market_type,
            message=f"{escalation_prefix}{period}内{direction} {change:+.2f}%",
            target_user_id=user_config.user_id,
            data={
                'price': ticker.price,
                'change_percent': change,
                'period': period,
                'is_escalation': is_escalation,
                'high_24h': ticker.high_24h,
                'low_24h': ticker.low_24h,
                'volume_24h': ticker.quote_volume_24h,
                'change_24h': ticker.price_change_24h,
            }
        )
        
        await self._emit(alert, user_config)
    
    async def _create_volume_alert(self, ticker: TickerData, user_config: UserConfig,
                                    level: AlertLevel, is_escalation: bool = False):
        """创建成交量报警"""
        escalation_prefix = "⚡升级 " if is_escalation else ""
        
        alert = Alert(
            alert_type=AlertType.VOLUME_SPIKE,
            level=level,
            symbol=ticker.symbol,
            market_type=ticker.market_type,
            message=f"{escalation_prefix}成交量暴增 {ticker.volume_change_ratio:.1f}倍",
            target_user_id=user_config.user_id,
            data={
                'price': ticker.price,
                'volume_ratio': ticker.volume_change_ratio,
                'is_escalation': is_escalation,
                'high_24h': ticker.high_24h,
                'low_24h': ticker.low_24h,
                'volume_24h': ticker.quote_volume_24h,
                'change_24h': ticker.price_change_24h,
            }
        )
        
        await self._emit(alert, user_config)
    
    async def _create_spread_alert(self, spread: SpreadData, user_config: UserConfig,
                                    alert_type: AlertType, level: AlertLevel,
                                    is_escalation: bool = False):
        """创建差价报警"""
        escalation_prefix = "⚡升级 " if is_escalation else ""
        
        if alert_type == AlertType.SPREAD_HIGH:
            message = f"{escalation_prefix}合约溢价 {spread.spread_percent:+.2f}%"
        else:
            message = f"{escalation_prefix}现货溢价 {abs(spread.spread_percent):.2f}%"
        
        spot_24h = self._get_24h_data(spread.symbol)
        
        alert = Alert(
            alert_type=alert_type,
            level=level,
            symbol=spread.symbol,
            market_type=MarketType.FUTURES,
            message=message,
            target_user_id=user_config.user_id,
            data={
                'price': spread.futures_price,
                'spot_price': spread.spot_price,
                'futures_price': spread.futures_price,
                'spread_percent': spread.spread_percent,
                'funding_rate': spread.funding_rate,
                'is_escalation': is_escalation,
                'high_24h': spot_24h.get('high', 0),
                'low_24h': spot_24h.get('low', 0),
                'volume_24h': spot_24h.get('quote_volume', 0),
                'change_24h': spot_24h.get('change_percent', 0),
            }
        )
        
        await self._emit(alert, user_config)
    
    async def _create_funding_alert(self, spread: SpreadData, user_config: UserConfig,
                                     alert_type: AlertType, level: AlertLevel,
                                     is_escalation: bool = False):
        """创建资金费率报警"""
        escalation_prefix = "⚡升级 " if is_escalation else ""
        
        if alert_type == AlertType.FUNDING_HIGH:
            message = f"{escalation_prefix}资金费率过高 {spread.funding_rate:.4f}%"
        else:
            message = f"{escalation_prefix}资金费率过低 {spread.funding_rate:.4f}%"
        
        spot_24h = self._get_24h_data(spread.symbol)
        
        alert = Alert(
            alert_type=alert_type,
            level=level,
            symbol=spread.symbol,
            market_type=MarketType.FUTURES,
            message=message,
            target_user_id=user_config.user_id,
            data={
                'price': spread.futures_price,
                'spot_price': spread.spot_price,
                'futures_price': spread.futures_price,
                'spread_percent': spread.spread_percent,
                'funding_rate': spread.funding_rate,
                'is_escalation': is_escalation,
                'high_24h': spot_24h.get('high', 0),
                'low_24h': spot_24h.get('low', 0),
                'volume_24h': spot_24h.get('quote_volume', 0),
                'change_24h': spot_24h.get('change_percent', 0),
            }
        )
        
        await self._emit(alert, user_config)
    
    async def _create_big_order_alert(self, orderbook: OrderBookData, user_config: UserConfig,
                                       alert_type: AlertType, order_value: float,
                                       order_price: float, current_price: float,
                                       volume_24h: float, level: AlertLevel,
                                       is_escalation: bool = False):
        """创建巨量挂单报警"""
        escalation_prefix = "⚡升级 " if is_escalation else ""
        
        order_type = "买单" if alert_type == AlertType.BIG_BID_ORDER else "卖单"
        price_diff = ((order_price - current_price) / current_price) * 100
        
        # 格式化金额
        if order_value >= 1_000_000:
            value_str = f"${order_value/1_000_000:.2f}M"
        elif order_value >= 1_000:
            value_str = f"${order_value/1_000:.1f}K"
        else:
            value_str = f"${order_value:.0f}"
        
        message = f"{escalation_prefix}巨量{order_type} {value_str}"
        
        spot_24h = self._get_24h_data(orderbook.symbol)
        
        alert = Alert(
            alert_type=alert_type,
            level=level,
            symbol=orderbook.symbol,
            market_type=orderbook.market_type,
            message=message,
            target_user_id=user_config.user_id,
            data={
                'price': current_price,
                'order_value': order_value,
                'order_price': order_price,
                'price_diff_percent': price_diff,
                'bid_ask_ratio': orderbook.bid_ask_ratio,
                'total_bid_value': orderbook.total_bid_value,
                'total_ask_value': orderbook.total_ask_value,
                'is_escalation': is_escalation,
                'high_24h': spot_24h.get('high', 0),
                'low_24h': spot_24h.get('low', 0),
                'volume_24h': volume_24h,
                'change_24h': spot_24h.get('change_percent', 0),
            }
        )
        
        self.big_order_alerts += 1
        await self._emit(alert, user_config)
    
    async def _emit(self, alert: Alert, user_config: UserConfig):
        """发送报警"""
        self.total_alerts += 1
        
        escalation_mark = "⚡" if alert.data.get('is_escalation') else ""
        logger.info(
            f"🔔 报警{escalation_mark} [{user_config.user_id}]: "
            f"{alert.symbol} [{alert.level.name}] - {alert.message}"
        )
        
        if self.on_alert:
            try:
                await self.on_alert(alert, user_config)
            except Exception as e:
                logger.error(f"发送报警失败: {e}")
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            'total_alerts': self.total_alerts,
            'escalation_count': self.escalation_count,
            'big_order_alerts': self.big_order_alerts,
            'active_cooldowns': sum(
                len(types) 
                for user_cooldowns in self.cooldowns.values() 
                for types in user_cooldowns.values()
            ),
            'cached_users': len(self._cached_users),
        }
    
    def clear_cooldowns(self, user_id: str = None, symbol: str = None):
        """清除冷却记录"""
        if user_id and symbol:
            if user_id in self.cooldowns and symbol in self.cooldowns[user_id]:
                del self.cooldowns[user_id][symbol]
        elif user_id:
            if user_id in self.cooldowns:
                self.cooldowns[user_id].clear()
        elif symbol:
            for user_cooldowns in self.cooldowns.values():
                if symbol in user_cooldowns:
                    del user_cooldowns[symbol]
        else:
            self.cooldowns.clear()
        
        logger.info(f"冷却已清除: user={user_id}, symbol={symbol}")