"""
数据模型 - 优化版（内存优化 + 消息格式优化）
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from enum import Enum
from collections import deque
import uuid


class MarketType(Enum):
    SPOT = "spot"
    FUTURES = "futures"


class AlertType(Enum):
    PRICE_PUMP = "price_pump"
    PRICE_DUMP = "price_dump"
    SPREAD_HIGH = "spread_high"
    SPREAD_LOW = "spread_low"
    VOLUME_SPIKE = "volume_spike"
    FUNDING_HIGH = "funding_high"
    FUNDING_LOW = "funding_low"
    # 新增：巨量挂单
    BIG_BID_ORDER = "big_bid_order"      # 买单挂巨量
    BIG_ASK_ORDER = "big_ask_order"      # 卖单挂巨量


class AlertLevel(Enum):
    INFO = ("ℹ️", 1)
    WARNING = ("⚠️", 2)
    CRITICAL = ("🚨", 3)
    EXTREME = ("🔥", 4)
    
    @property
    def emoji(self):
        return self.value[0]
    
    @property
    def priority(self):
        return self.value[1]


class AlertStatus(Enum):
    PENDING = "pending"
    SENT = "sent"
    CONFIRMED = "confirmed"


@dataclass
class TokenInfo:
    """代币信息"""
    symbol: str
    base_asset: str = ""
    quote_asset: str = "USDT"
    price: float = 0.0
    price_change_24h: float = 0.0
    price_change_percent_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h: float = 0.0
    quote_volume_24h: float = 0.0
    trades_24h: int = 0
    last_update: datetime = field(default_factory=datetime.now)
    
    @property
    def volume_display(self) -> str:
        v = self.quote_volume_24h
        if v >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        elif v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        elif v >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.2f}"


@dataclass
class TickerData:
    """行情数据"""
    symbol: str
    price: float
    price_change_1m: float = 0.0
    price_change_5m: float = 0.0
    price_change_15m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    volume_24h: float = 0.0
    quote_volume_24h: float = 0.0
    volume_change_ratio: float = 1.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    market_type: MarketType = MarketType.SPOT


@dataclass
class SpreadData:
    """差价数据"""
    symbol: str
    spot_price: float
    futures_price: float
    spread_percent: float
    funding_rate: float = 0.0
    next_funding_time: Optional[datetime] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OrderBookData:
    """订单簿数据 - 用于巨量挂单检测"""
    symbol: str
    # 买单 (bid) - 价格从高到低
    bids: List[tuple] = field(default_factory=list)  # [(price, quantity), ...]
    # 卖单 (ask) - 价格从低到高  
    asks: List[tuple] = field(default_factory=list)  # [(price, quantity), ...]
    # 最大单笔挂单
    max_bid_order: float = 0.0  # 最大买单金额 (USDT)
    max_ask_order: float = 0.0  # 最大卖单金额 (USDT)
    max_bid_price: float = 0.0  # 最大买单价格
    max_ask_price: float = 0.0  # 最大卖单价格
    # 统计
    total_bid_value: float = 0.0  # 买盘总金额
    total_ask_value: float = 0.0  # 卖盘总金额
    bid_ask_ratio: float = 1.0    # 买卖比
    timestamp: datetime = field(default_factory=datetime.now)
    market_type: MarketType = MarketType.SPOT


@dataclass
class Alert:
    """报警消息"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    alert_type: AlertType = AlertType.PRICE_PUMP
    level: AlertLevel = AlertLevel.INFO
    symbol: str = ""
    market_type: MarketType = MarketType.SPOT
    message: str = ""
    data: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    target_user_id: str = ""
    status: AlertStatus = AlertStatus.PENDING
    sent_count: int = 0
    last_sent: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    
    def to_telegram_message(self, prefix: str = "", tz_offset: int = 8) -> str:
        """生成Telegram消息 - 优化格式"""
        
        name = self.symbol.replace('USDT', '')
        market = "现货" if self.market_type == MarketType.SPOT else "合约"
        
        # 级别图标
        level_icons = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🚨",
            AlertLevel.EXTREME: "🔥",
        }
        
        # 类型图标
        type_icons = {
            AlertType.PRICE_PUMP: "📈",
            AlertType.PRICE_DUMP: "📉",
            AlertType.VOLUME_SPIKE: "📊",
            AlertType.SPREAD_HIGH: "⬆️",
            AlertType.SPREAD_LOW: "⬇️",
            AlertType.FUNDING_HIGH: "💰",
            AlertType.FUNDING_LOW: "💸",
            AlertType.BIG_BID_ORDER: "🟢",
            AlertType.BIG_ASK_ORDER: "🔴",
        }
        
        icon = level_icons.get(self.level, "📢")
        type_icon = type_icons.get(self.alert_type, "📢")
        
        price = self.data.get('price', 0)
        change_24h = self.data.get('change_24h', 0)
        volume_24h = self.data.get('volume_24h', 0)
        high_24h = self.data.get('high_24h', 0)
        low_24h = self.data.get('low_24h', 0)
        
        # 计算价格在24h范围内的位置
        position_bar = ""
        if high_24h > 0 and low_24h > 0 and price > 0:
            range_24h = high_24h - low_24h
            if range_24h > 0:
                position = (price - low_24h) / range_24h * 100
                position_bar = self._make_position_bar(position)
        
        # 时间处理
        try:
            if self.timestamp.tzinfo is None:
                local_time = self.timestamp + timedelta(hours=tz_offset)
            else:
                user_tz = timezone(timedelta(hours=tz_offset))
                local_time = self.timestamp.astimezone(user_tz)
            time_str = local_time.strftime("%H:%M:%S")
        except Exception:
            time_str = datetime.now().strftime("%H:%M:%S")
        
        # 24h涨跌颜色
        change_icon = "🟢" if change_24h > 0 else "🔴" if change_24h < 0 else "⚪"
        
        # 构建消息
        lines = [
            f"{prefix}{icon} <b>{type_icon} {name}</b> · {market}",
            f"",
            f"▸ {self.message}",
            f"▸ ${self._fmt_price(price)}",
        ]
        
        if position_bar:
            lines.append(f"▸ {position_bar}")
        
        # 巨量挂单特殊信息
        if self.alert_type in (AlertType.BIG_BID_ORDER, AlertType.BIG_ASK_ORDER):
            order_value = self.data.get('order_value', 0)
            order_price = self.data.get('order_price', 0)
            price_diff = self.data.get('price_diff_percent', 0)
            bid_ask_ratio = self.data.get('bid_ask_ratio', 1)
            
            order_type = "买单" if self.alert_type == AlertType.BIG_BID_ORDER else "卖单"
            lines.extend([
                f"─────────────────",
                f"💎 巨量{order_type}: <b>{self._fmt_volume(order_value)}</b>",
                f"📍 挂单价: {self._fmt_price(order_price)} ({price_diff:+.2f}%)",
                f"⚖️ 买卖比: {bid_ask_ratio:.2f}",
            ])
        
        lines.extend([
            f"─────────────────",
            f"{change_icon} 24H: <b>{change_24h:+.2f}%</b>",
            f"📈 H: {self._fmt_price(high_24h)}  📉 L: {self._fmt_price(low_24h)}",
            f"💎 Vol: {self._fmt_volume(volume_24h)}",
            f"",
            f"⏰ {time_str}",
        ])
        
        return '\n'.join(lines)
    
    def _make_position_bar(self, position: float) -> str:
        """生成位置条 - 显示当前价格在24h范围内的位置"""
        total_blocks = 10
        filled = int(position / 100 * total_blocks)
        filled = max(0, min(total_blocks, filled))
        
        bar = "▓" * filled + "░" * (total_blocks - filled)
        return f"L {bar} H ({position:.0f}%)"

    def _fmt_price(self, price: float) -> str:
        """格式化价格"""
        if price == 0:
            return "0"
        elif price >= 10000:
            return f"{price:,.0f}"
        elif price >= 1000:
            return f"{price:,.2f}"
        elif price >= 1:
            return f"{price:.4f}"
        elif price >= 0.0001:
            return f"{price:.6f}"
        else:
            return f"{price:.8f}"
    
    def _fmt_volume(self, v: float) -> str:
        """格式化成交额"""
        if v >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        elif v >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        elif v >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.0f}"
    
    def to_email_subject(self) -> str:
        level_prefix = "🔥紧急 " if self.level.priority >= 3 else ""
        escalation = "⚡升级 " if self.data.get('is_escalation') else ""
        return f"{level_prefix}{escalation}[鹰眼报警] {self.symbol} - {self.message[:30]}"
    
    def to_email_html(self, prefix: str = "", user_tz_offset: int = 8) -> str:
        user_tz = timezone(timedelta(hours=user_tz_offset))
        
        try:
            if self.timestamp.tzinfo is None:
                local_time = self.timestamp.replace(tzinfo=timezone.utc).astimezone(user_tz)
            else:
                local_time = self.timestamp.astimezone(user_tz)
        except:
            local_time = datetime.now(user_tz)
        
        color = "#28a745" if self.alert_type == AlertType.PRICE_PUMP else "#dc3545"
        escalation_banner = ""
        if self.data.get('is_escalation'):
            escalation_banner = '<div style="background: #ff9800; color: white; padding: 10px; text-align: center;"><b>⚡ 级别升级 - 穿透冷却</b></div>'
        
        html = f"""
        <div style="font-family: Arial; max-width: 600px; margin: 0 auto;">
            <div style="background: {color}; color: white; padding: 20px; text-align: center;">
                <h1>{prefix}🦅 鹰眼报警</h1>
                <h2>{self.symbol}</h2>
            </div>
            {escalation_banner}
            <div style="padding: 20px; background: #f8f9fa;">
                <p><strong>报警ID:</strong> {self.id}</p>
                <p><strong>类型:</strong> {self.alert_type.value}</p>
                <p><strong>级别:</strong> {self.level.emoji} {self.level.name}</p>
                <p><strong>详情:</strong> {self.message}</p>
        """
        
        if 'price' in self.data:
            html += f"<p><strong>价格:</strong> ${self.data['price']:.6f}</p>"
        if 'change_percent' in self.data:
            html += f"<p><strong>涨跌幅:</strong> {self.data['change_percent']:+.2f}%</p>"
        if 'volume_24h' in self.data:
            html += f"<p><strong>24h成交额:</strong> ${self.data['volume_24h']:,.0f}</p>"
        if 'order_value' in self.data:
            html += f"<p><strong>挂单金额:</strong> ${self.data['order_value']:,.0f}</p>"
        
        html += f"""
                <hr>
                <p style="color: #666;">时间: {local_time.strftime('%Y-%m-%d %H:%M:%S')} (UTC{user_tz_offset:+d})</p>
            </div>
        </div>
        """
        return html


@dataclass
class PriceHistory:
    """价格历史 - 使用deque优化内存"""
    symbol: str
    market_type: MarketType
    # 使用deque自动限制大小，避免无限增长
    # maxlen=720: 假设每5秒一条数据，保存1小时 = 720条
    prices: deque = field(default_factory=lambda: deque(maxlen=720))
    volumes: deque = field(default_factory=lambda: deque(maxlen=720))
    
    def add(self, price: float, volume: float = 0):
        """添加价格和成交量数据"""
        now = datetime.now()
        self.prices.append((now, price))
        self.volumes.append((now, volume))
        # deque会自动移除超出maxlen的旧数据，无需手动清理
    
    def get_change(self, minutes: int) -> Optional[float]:
        """获取指定分钟数内的涨跌幅"""
        if len(self.prices) < 2:
            return None
        
        cutoff = datetime.now().timestamp() - minutes * 60
        current = self.prices[-1][1]
        
        # 查找cutoff时间点之前的最后一个价格
        old_price = None
        for t, p in self.prices:
            if t.timestamp() <= cutoff:
                old_price = p
            else:
                break
        
        # 如果没有足够历史数据，使用最早的价格
        if old_price is None:
            old_price = self.prices[0][1]
        
        if old_price and old_price > 0:
            return ((current - old_price) / old_price) * 100
        
        return None
    
    def get_volume_ratio(self, minutes: int = 5) -> float:
        """获取成交量变化比率"""
        if len(self.volumes) < 10:
            return 1.0
        
        cutoff = datetime.now().timestamp() - minutes * 60
        recent = []
        older = []
        
        for t, v in self.volumes:
            if t.timestamp() > cutoff:
                recent.append(v)
            else:
                older.append(v)
        
        if not recent or not older:
            return 1.0
        
        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older)
        
        if avg_older > 0:
            return avg_recent / avg_older
        
        return 1.0
    
    def get_price_range(self, minutes: int = 60) -> tuple:
        """获取指定时间内的价格范围 (min, max)"""
        if not self.prices:
            return (0, 0)
        
        cutoff = datetime.now().timestamp() - minutes * 60
        prices_in_range = [p for t, p in self.prices if t.timestamp() > cutoff]
        
        if not prices_in_range:
            prices_in_range = [p for _, p in self.prices]
        
        return (min(prices_in_range), max(prices_in_range))