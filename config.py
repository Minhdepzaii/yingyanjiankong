"""
配置管理 - 增强版 (紧急重复提醒 + 巨量挂单)
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from enum import Enum
import json
import os
from datetime import datetime, timezone, timedelta
from loguru import logger


# 常用时区
TIMEZONE_PRESETS = {
    "UTC": 0,
    "北京/香港/台北": 8,
    "东京": 9,
    "新加坡": 8,
    "迪拜": 4,
    "伦敦": 0,
    "巴黎/柏林": 1,
    "莫斯科": 3,
    "纽约": -5,
    "洛杉矶": -8,
    "悉尼": 10,
}


class AlertProfile(Enum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class AlertMode(Enum):
    SINGLE = "single"      # 单次报警
    REPEAT = "repeat"      # 重复提醒直到确认


class NotifyChannel(Enum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    ALL = "all"


@dataclass
class PriceThreshold:
    short_1m_pump: float = 6.0
    short_1m_dump: float = -6.0
    mid_5m_pump: float = 9.0
    mid_5m_dump: float = -9.0
    long_15m_pump: float = 15.0
    long_15m_dump: float = -15.0
    hourly_pump: float = 21.0
    hourly_dump: float = -21.0


@dataclass
class SpreadThreshold:
    spot_futures: float = 2.5
    funding_high: float = 0.25
    funding_low: float = -0.25


@dataclass
class VolumeThreshold:
    spike_ratio: float = 12.0     # 原5.0
    large_order_usdt: float = 500000  # 原100000


@dataclass
class BigOrderThreshold:
    """巨量挂单阈值配置 - 高阈值版本"""
    enabled: bool = True
    
    # 绝对值阈值（提高10倍）
    min_order_small_cap: float = 500000       # 小市值最低 $500K（原$50K）
    min_order_mid_cap: float = 2000000        # 中市值最低 $2M（原$200K）
    min_order_large_cap: float = 5000000      # 大市值最低 $5M（原$500K）
    min_order_mega_cap: float = 10000000      # 超大市值最低 $10M（原$1M）
    
    # 相对阈值（占24h成交额百分比，提高10倍）
    ratio_small_cap: float = 20.0      # 小市值 20%（原2%）
    ratio_mid_cap: float = 10.0        # 中市值 10%（原1%）
    ratio_large_cap: float = 5.0       # 大市值 5%（原0.5%）
    ratio_mega_cap: float = 2.0        # 超大市值 2%（原0.2%）
    
    # 价格偏离阈值 - 挂单价格距当前价格的距离
    max_price_deviation: float = 5.0  # 最大偏离5%以内才报警
    
    # 深度检查层数
    depth_levels: int = 20  # 检查前20档
    
    def get_threshold(self, volume_24h: float) -> tuple:
        """
        根据24h成交额返回阈值 (绝对值, 百分比)
        """
        if volume_24h < 10_000_000:  # < $10M 小市值
            return (self.min_order_small_cap, self.ratio_small_cap)
        elif volume_24h < 100_000_000:  # < $100M 中市值
            return (self.min_order_mid_cap, self.ratio_mid_cap)
        elif volume_24h < 1_000_000_000:  # < $1B 大市值
            return (self.min_order_large_cap, self.ratio_large_cap)
        else:  # >= $1B 超大市值
            return (self.min_order_mega_cap, self.ratio_mega_cap)
    
    def is_big_order(self, order_value: float, volume_24h: float) -> bool:
        """判断是否为巨量挂单"""
        if volume_24h <= 0:
            return order_value >= self.min_order_small_cap
        
        abs_threshold, ratio_threshold = self.get_threshold(volume_24h)
        ratio_value = volume_24h * ratio_threshold / 100
        
        # 取两者中的较大值作为阈值
        threshold = max(abs_threshold, ratio_value)
        return order_value >= threshold


@dataclass
class RepeatAlertConfig:
    """重复提醒配置 - 紧急模式"""
    enabled: bool = False
    interval_seconds: int = 10       # 每10秒重复一次（紧急）
    max_repeats: int = 30            # 最多重复30次（5分钟内）
    require_confirm: bool = True


@dataclass
class NightModeConfig:
    """夜间模式配置 - 紧急唤醒"""
    enabled: bool = True
    auto_switch: bool = True
    night_start: str = "23:00"
    night_end: str = "07:00"
    # 夜间更紧急：间隔短、次数多
    night_interval_seconds: int = 15  # 夜间每15秒重复
    night_max_repeats: int = 20       # 夜间最多20次（5分钟内）
    night_add_email: bool = True


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    to_addresses: List[str] = field(default_factory=list)
    use_tls: bool = True


@dataclass
class AlertModeConfig:
    """报警模式配置"""
    mode: AlertMode = AlertMode.SINGLE
    repeat: RepeatAlertConfig = field(default_factory=RepeatAlertConfig)
    night: NightModeConfig = field(default_factory=NightModeConfig)


@dataclass
class UserConfig:
    """用户配置"""
    user_id: str = ""
    username: str = ""
    chat_id: str = ""
    is_active: bool = True
    is_admin: bool = False
    created_at: str = ""
    
    timezone_offset: int = 8
    timezone_name: str = "北京/香港/台北"
    
    profile: AlertProfile = AlertProfile.MODERATE
    
    price: PriceThreshold = field(default_factory=PriceThreshold)
    spread: SpreadThreshold = field(default_factory=SpreadThreshold)
    volume: VolumeThreshold = field(default_factory=VolumeThreshold)
    big_order: BigOrderThreshold = field(default_factory=BigOrderThreshold)  # 新增
    
    alert_mode: AlertModeConfig = field(default_factory=AlertModeConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    
    notify_channels: List[NotifyChannel] = field(
        default_factory=lambda: [NotifyChannel.TELEGRAM]
    )
    
    enable_spot: bool = True
    enable_futures: bool = True
    enable_spread: bool = True
    enable_volume: bool = True
    enable_funding: bool = False
    enable_big_order: bool = True  # 新增：巨量挂单开关
    
    cooldown_seconds: int = 300
    
    watch_mode: str = "all"
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    
    min_alert_level: str = "INFO"
    
    # 24小时成交额筛选
    min_volume_24h: float = 0
    volume_filter_enabled: bool = False
    
    def should_monitor(self, symbol: str) -> bool:
        """检查是否应该监控该代币（黑名单始终生效）"""
        symbol = symbol.upper()
        
        # 黑名单始终生效
        if symbol in self.blacklist:
            return False
        for blocked in self.blacklist:
            blocked_base = blocked.replace('USDT', '')
            symbol_base = symbol.replace('USDT', '')
            if symbol_base == blocked_base:
                return False
        
        # 白名单模式
        if self.watch_mode == "whitelist":
            if symbol in self.whitelist:
                return True
            for allowed in self.whitelist:
                allowed_base = allowed.replace('USDT', '')
                symbol_base = symbol.replace('USDT', '')
                if symbol_base == allowed_base:
                    return True
            return False
        
        return True
    
    def should_monitor_by_volume(self, volume_24h: float) -> bool:
        """检查成交额是否达到阈值"""
        if not self.volume_filter_enabled or self.min_volume_24h <= 0:
            return True
        return volume_24h >= self.min_volume_24h
    
    def get_volume_filter_display(self) -> str:
        """获取成交额筛选显示文本"""
        if not self.volume_filter_enabled or self.min_volume_24h <= 0:
            return "不限制"
        v = self.min_volume_24h
        if v >= 1_000_000_000:
            return f"≥${v/1_000_000_000:.1f}B"
        elif v >= 1_000_000:
            return f"≥${v/1_000_000:.0f}M"
        elif v >= 1_000:
            return f"≥${v/1_000:.0f}K"
        return f"≥${v:.0f}"
    
    def get_local_time(self, utc_time: datetime = None) -> datetime:
        """将时间转换为用户时区"""
        if utc_time is None:
            utc_time = datetime.now(timezone.utc)
        elif utc_time.tzinfo is None:
            return utc_time + timedelta(hours=self.timezone_offset)
        user_tz = timezone(timedelta(hours=self.timezone_offset))
        return utc_time.astimezone(user_tz)
    
    def get_local_time_str(self, utc_time: datetime = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        local = self.get_local_time(utc_time)
        return f"{local.strftime(fmt)} (UTC{self.timezone_offset:+d})"
    
    def is_night_time(self) -> bool:
        if not self.alert_mode.night.enabled:
            return False
        local_time = self.get_local_time()
        now = local_time.time()
        try:
            start = datetime.strptime(self.alert_mode.night.night_start, "%H:%M").time()
            end = datetime.strptime(self.alert_mode.night.night_end, "%H:%M").time()
        except:
            return False
        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end
    
    def get_effective_mode(self) -> AlertMode:
        night_config = self.alert_mode.night
        if night_config.enabled and night_config.auto_switch and self.is_night_time():
            return AlertMode.REPEAT
        return self.alert_mode.mode
    
    def get_repeat_config(self) -> dict:
        """获取当前生效的重复配置"""
        if self.is_night_time() and self.alert_mode.night.enabled:
            return {
                'enabled': True,
                'interval_seconds': self.alert_mode.night.night_interval_seconds,
                'max_repeats': self.alert_mode.night.night_max_repeats,
                'require_confirm': True,
            }
        else:
            repeat = self.alert_mode.repeat
            return {
                'enabled': repeat.enabled or self.alert_mode.mode == AlertMode.REPEAT,
                'interval_seconds': repeat.interval_seconds,
                'max_repeats': repeat.max_repeats,
                'require_confirm': repeat.require_confirm,
            }
    
    def get_notify_channels(self) -> List[NotifyChannel]:
        channels = list(self.notify_channels)
        night_config = self.alert_mode.night
        if (night_config.enabled and 
            night_config.night_add_email and 
            self.is_night_time() and
            self.email.enabled and
            NotifyChannel.EMAIL not in channels):
            channels.append(NotifyChannel.EMAIL)
        return channels


# 预设配置
# 预设配置
PRESET_CONFIGS = {
    AlertProfile.CONSERVATIVE: {
        "price": PriceThreshold(
            short_1m_pump=10.0,
            short_1m_dump=-10.0,
            mid_5m_pump=15.0,
            mid_5m_dump=-15.0,
            long_15m_pump=25.0,
            long_15m_dump=-25.0,
            hourly_pump=35.0,
            hourly_dump=-35.0,
        ),
        "spread": SpreadThreshold(4.0, 0.4, -0.4),
        "volume": VolumeThreshold(20.0, 1000000),
        "big_order": BigOrderThreshold(
            enabled=True,
            min_order_small_cap=1000000,      # $1M（原$100K）
            min_order_mid_cap=5000000,        # $5M（原$500K）
            min_order_large_cap=10000000,     # $10M（原$1M）
            min_order_mega_cap=20000000,      # $20M（原$2M）
            ratio_small_cap=20.0,
            ratio_mid_cap=10.0,
            ratio_large_cap=5.0,
            ratio_mega_cap=2.0,
        ),
        "cooldown_seconds": 600,
    },
    AlertProfile.MODERATE: {
        "price": PriceThreshold(
            short_1m_pump=6.0,
            short_1m_dump=-6.0,
            mid_5m_pump=9.0,
            mid_5m_dump=-9.0,
            long_15m_pump=15.0,
            long_15m_dump=-15.0,
            hourly_pump=21.0,
            hourly_dump=-21.0,
        ),
        "spread": SpreadThreshold(2.5, 0.25, -0.25),
        "volume": VolumeThreshold(12.0, 500000),
        "big_order": BigOrderThreshold(
            enabled=True,
            min_order_small_cap=500000,       # $500K（原$50K）
            min_order_mid_cap=2000000,        # $2M（原$200K）
            min_order_large_cap=5000000,      # $5M（原$500K）
            min_order_mega_cap=10000000,      # $10M（原$1M）
            ratio_small_cap=20.0,
            ratio_mid_cap=10.0,
            ratio_large_cap=5.0,
            ratio_mega_cap=2.0,
        ),
        "cooldown_seconds": 300,
    },
    AlertProfile.AGGRESSIVE: {
        "price": PriceThreshold(
            short_1m_pump=3.5,
            short_1m_dump=-3.5,
            mid_5m_pump=5.0,
            mid_5m_dump=-5.0,
            long_15m_pump=9.0,
            long_15m_dump=-9.0,
            hourly_pump=12.0,
            hourly_dump=-12.0,
        ),
        "spread": SpreadThreshold(1.5, 0.15, -0.15),
        "volume": VolumeThreshold(7.0, 200000),
        "big_order": BigOrderThreshold(
            enabled=True,
            min_order_small_cap=300000,       # $300K（原$30K）
            min_order_mid_cap=1000000,        # $1M（原$100K）
            min_order_large_cap=3000000,      # $3M（原$300K）
            min_order_mega_cap=5000000,       # $5M（原$500K）
            ratio_small_cap=20.0,
            ratio_mid_cap=10.0,
            ratio_large_cap=5.0,
            ratio_mega_cap=2.0,
        ),
        "cooldown_seconds": 120,
    },
}


class UserManager:
    """多用户管理器"""
    
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.users_file = os.path.join(data_dir, "users.json")
        self.users: Dict[str, UserConfig] = {}
        self.admin_ids: Set[str] = set()
        
        os.makedirs(data_dir, exist_ok=True)
        self._load()
        
        admin_env = os.getenv('ADMIN_USER_IDS', '')
        if admin_env:
            self.admin_ids = set(admin_env.split(','))
    
    def _load(self):
        if os.path.exists(self.users_file):
            try:
                with open(self.users_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for user_id, user_data in data.items():
                        self.users[user_id] = self._dict_to_config(user_data)
                logger.info(f"已加载 {len(self.users)} 个用户配置")
            except Exception as e:
                logger.error(f"加载用户数据失败: {e}")
    
    def _save(self):
        try:
            data = {uid: self._config_to_dict(cfg) for uid, cfg in self.users.items()}
            with open(self.users_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存用户数据失败: {e}")
    
    def _dict_to_config(self, data: dict) -> UserConfig:
        config = UserConfig()
        
        config.user_id = data.get('user_id', '')
        config.username = data.get('username', '')
        config.chat_id = data.get('chat_id', '')
        config.is_active = data.get('is_active', True)
        config.is_admin = data.get('is_admin', False)
        config.created_at = data.get('created_at', '')
        
        config.timezone_offset = data.get('timezone_offset', 8)
        config.timezone_name = data.get('timezone_name', '北京/香港/台北')
        
        if 'profile' in data:
            config.profile = AlertProfile(data['profile'])
            if config.profile in PRESET_CONFIGS:
                preset = PRESET_CONFIGS[config.profile]
                config.price = preset['price']
                config.spread = preset['spread']
                config.volume = preset['volume']
                config.big_order = preset['big_order']
                config.cooldown_seconds = preset['cooldown_seconds']
        
        config.watch_mode = data.get('watch_mode', 'all')
        config.whitelist = [s.upper() for s in data.get('whitelist', [])]
        config.blacklist = [s.upper() for s in data.get('blacklist', [])]
        
        config.min_volume_24h = data.get('min_volume_24h', 0)
        config.volume_filter_enabled = data.get('volume_filter_enabled', False)
        
        # 巨量挂单开关
        config.enable_big_order = data.get('enable_big_order', True)
        
        if 'alert_mode' in data:
            am = data['alert_mode']
            if 'mode' in am:
                config.alert_mode.mode = AlertMode(am['mode'])
            
            if 'repeat' in am:
                r = am['repeat']
                config.alert_mode.repeat.enabled = r.get('enabled', False)
                config.alert_mode.repeat.interval_seconds = r.get('interval_seconds', 10)
                config.alert_mode.repeat.max_repeats = r.get('max_repeats', 30)
                config.alert_mode.repeat.require_confirm = r.get('require_confirm', True)
            
            if 'night' in am:
                n = am['night']
                config.alert_mode.night.enabled = n.get('enabled', False)
                config.alert_mode.night.auto_switch = n.get('auto_switch', True)
                config.alert_mode.night.night_start = n.get('night_start', '23:00')
                config.alert_mode.night.night_end = n.get('night_end', '07:00')
                config.alert_mode.night.night_interval_seconds = n.get('night_interval_seconds', 15)
                config.alert_mode.night.night_max_repeats = n.get('night_max_repeats', 20)
                config.alert_mode.night.night_add_email = n.get('night_add_email', True)
        
        if 'email' in data:
            config.email.enabled = data['email'].get('enabled', False)
            config.email.to_addresses = data['email'].get('to_addresses', [])
        
        for key in ['enable_spot', 'enable_futures', 'enable_spread', 
                    'enable_volume', 'enable_funding', 'enable_big_order', 'cooldown_seconds']:
            if key in data:
                setattr(config, key, data[key])
        
        if 'notify_channels' in data:
            config.notify_channels = [NotifyChannel(c) for c in data['notify_channels']]
        
        return config
    
    def _config_to_dict(self, config: UserConfig) -> dict:
        return {
            'user_id': config.user_id,
            'username': config.username,
            'chat_id': config.chat_id,
            'is_active': config.is_active,
            'is_admin': config.is_admin,
            'created_at': config.created_at,
            'timezone_offset': config.timezone_offset,
            'timezone_name': config.timezone_name,
            'profile': config.profile.value,
            'watch_mode': config.watch_mode,
            'whitelist': config.whitelist,
            'blacklist': config.blacklist,
            'min_volume_24h': config.min_volume_24h,
            'volume_filter_enabled': config.volume_filter_enabled,
            'enable_big_order': config.enable_big_order,
            'alert_mode': {
                'mode': config.alert_mode.mode.value,
                'repeat': {
                    'enabled': config.alert_mode.repeat.enabled,
                    'interval_seconds': config.alert_mode.repeat.interval_seconds,
                    'max_repeats': config.alert_mode.repeat.max_repeats,
                    'require_confirm': config.alert_mode.repeat.require_confirm,
                },
                'night': {
                    'enabled': config.alert_mode.night.enabled,
                    'auto_switch': config.alert_mode.night.auto_switch,
                    'night_start': config.alert_mode.night.night_start,
                    'night_end': config.alert_mode.night.night_end,
                    'night_interval_seconds': config.alert_mode.night.night_interval_seconds,
                    'night_max_repeats': config.alert_mode.night.night_max_repeats,
                    'night_add_email': config.alert_mode.night.night_add_email,
                },
            },
            'email': {
                'enabled': config.email.enabled,
                'to_addresses': config.email.to_addresses,
            },
            'notify_channels': [c.value for c in config.notify_channels],
            'enable_spot': config.enable_spot,
            'enable_futures': config.enable_futures,
            'enable_spread': config.enable_spread,
            'enable_volume': config.enable_volume,
            'enable_funding': config.enable_funding,
            'cooldown_seconds': config.cooldown_seconds,
        }
    
    def get_user(self, user_id: str) -> Optional[UserConfig]:
        return self.users.get(str(user_id))
    
    def get_or_create_user(self, user_id: str, username: str = "", 
                           chat_id: str = "") -> UserConfig:
        user_id = str(user_id)
        if user_id not in self.users:
            config = UserConfig(
                user_id=user_id,
                username=username,
                chat_id=chat_id or user_id,
                is_active=True,
                is_admin=user_id in self.admin_ids,
                created_at=datetime.now().isoformat(),
            )
            self.users[user_id] = config
            self._save()
            logger.info(f"新用户注册: {user_id} ({username})")
        else:
            if username:
                self.users[user_id].username = username
            if chat_id:
                self.users[user_id].chat_id = chat_id
        return self.users[user_id]
    
    def update_user(self, user_id: str, **kwargs):
        user_id = str(user_id)
        if user_id in self.users:
            for key, value in kwargs.items():
                if hasattr(self.users[user_id], key):
                    setattr(self.users[user_id], key, value)
            self._save()
    
    def set_profile(self, user_id: str, profile: AlertProfile):
        user_id = str(user_id)
        if user_id in self.users:
            config = self.users[user_id]
            config.profile = profile
            if profile in PRESET_CONFIGS:
                preset = PRESET_CONFIGS[profile]
                config.price = preset['price']
                config.spread = preset['spread']
                config.volume = preset['volume']
                config.big_order = preset['big_order']
                config.cooldown_seconds = preset['cooldown_seconds']
            self._save()
    
    def set_alert_mode(self, user_id: str, mode: AlertMode):
        user_id = str(user_id)
        if user_id in self.users:
            self.users[user_id].alert_mode.mode = mode
            if mode == AlertMode.REPEAT:
                self.users[user_id].alert_mode.repeat.enabled = True
            self._save()
    
    def set_night_mode(self, user_id: str, enabled: bool):
        user_id = str(user_id)
        if user_id in self.users:
            self.users[user_id].alert_mode.night.enabled = enabled
            self._save()
    
    def set_night_time(self, user_id: str, start: str, end: str):
        user_id = str(user_id)
        if user_id in self.users:
            self.users[user_id].alert_mode.night.night_start = start
            self.users[user_id].alert_mode.night.night_end = end
            self._save()
    
    def enable_email(self, user_id: str, email_address: str = None) -> bool:
        user_id = str(user_id)
        if user_id not in self.users:
            return False
        config = self.users[user_id]
        config.email.enabled = True
        if email_address and email_address not in config.email.to_addresses:
            config.email.to_addresses.append(email_address)
        if NotifyChannel.EMAIL not in config.notify_channels:
            config.notify_channels.append(NotifyChannel.EMAIL)
        self._save()
        return True
    
    def disable_email(self, user_id: str) -> bool:
        user_id = str(user_id)
        if user_id not in self.users:
            return False
        config = self.users[user_id]
        config.email.enabled = False
        if NotifyChannel.EMAIL in config.notify_channels:
            config.notify_channels.remove(NotifyChannel.EMAIL)
        self._save()
        return True
    
    def set_timezone(self, user_id: str, offset: int, name: str = ""):
        user_id = str(user_id)
        if user_id in self.users:
            self.users[user_id].timezone_offset = offset
            self.users[user_id].timezone_name = name or f"UTC{offset:+d}"
            self._save()
    
    def add_to_whitelist(self, user_id: str, symbols: List[str]):
        user_id = str(user_id)
        if user_id in self.users:
            for symbol in symbols:
                symbol = symbol.upper().strip()
                if symbol and symbol not in self.users[user_id].whitelist:
                    self.users[user_id].whitelist.append(symbol)
            self._save()
    
    def remove_from_whitelist(self, user_id: str, symbols: List[str]):
        user_id = str(user_id)
        if user_id in self.users:
            for symbol in symbols:
                symbol = symbol.upper().strip()
                if symbol in self.users[user_id].whitelist:
                    self.users[user_id].whitelist.remove(symbol)
            self._save()
    
    def add_to_blacklist(self, user_id: str, symbols: List[str]):
        user_id = str(user_id)
        if user_id in self.users:
            for symbol in symbols:
                symbol = symbol.upper().strip()
                if symbol and symbol not in self.users[user_id].blacklist:
                    self.users[user_id].blacklist.append(symbol)
            self._save()
    
    def remove_from_blacklist(self, user_id: str, symbols: List[str]):
        user_id = str(user_id)
        if user_id in self.users:
            for symbol in symbols:
                symbol = symbol.upper().strip()
                if symbol in self.users[user_id].blacklist:
                    self.users[user_id].blacklist.remove(symbol)
            self._save()
    
    def set_watch_mode(self, user_id: str, mode: str):
        user_id = str(user_id)
        if user_id in self.users and mode in ['all', 'whitelist', 'blacklist']:
            self.users[user_id].watch_mode = mode
            self._save()
    
    def get_active_users(self) -> List[UserConfig]:
        return [u for u in self.users.values() if u.is_active]
    
    def is_admin(self, user_id: str) -> bool:
        user_id = str(user_id)
        return user_id in self.admin_ids or (
            user_id in self.users and self.users[user_id].is_admin
        )
    
    def get_all_users(self) -> List[UserConfig]:
        return list(self.users.values())
    
    def set_volume_filter(self, user_id: str, enabled: bool, min_volume: float = 0):
        user_id = str(user_id)
        if user_id in self.users:
            self.users[user_id].volume_filter_enabled = enabled
            if min_volume > 0:
                self.users[user_id].min_volume_24h = min_volume
            self._save()


# 全局用户管理器
user_manager = UserManager()