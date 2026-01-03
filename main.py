"""
鹰眼监控系统 - 主程序
"""
import asyncio
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 初始化日志（在其他导入之前）
from logging_config import setup_logging
setup_logging(
    log_level=os.getenv('LOG_LEVEL', 'INFO'),
    log_dir="logs",
    app_name="hawkeye"
)

from loguru import logger
from binance_client import BinanceClient
from alert_engine import AlertEngine
from notifier import MultiUserNotifier
from telegram_bot import TelegramBot
from models import Alert
from config import UserConfig


class HawkEyeSystem:
    """鹰眼监控系统"""
    
    def __init__(self):
        # 获取配置
        self.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        
        if not self.telegram_token:
            raise ValueError("请设置 TELEGRAM_BOT_TOKEN 环境变量")
        
        # 初始化组件
        self.binance = BinanceClient()
        self.alert_engine = AlertEngine()
        self.notifier = MultiUserNotifier(self.telegram_token)
        self.bot = TelegramBot(self.telegram_token, self.notifier)
        
        # 设置引用
        self.bot.set_system(self)
        self.alert_engine.binance = self.binance
        
        # 设置报警回调
        self.alert_engine.on_alert = self._handle_alert
        
        # 设置 Binance 回调
        self.binance.on_spot_update = self._on_ticker_update
        self.binance.on_futures_update = self._on_ticker_update
        self.binance.on_spread_update = self._on_spread_update
        self.binance.on_orderbook_update = self.alert_engine.check_orderbook_for_all_users
        
        logger.info("🦅 鹰眼监控系统初始化完成")
    
    async def _handle_alert(self, alert: Alert, user_config: UserConfig):
        """处理报警 - 发送给用户"""
        await self.notifier.send_alert_to_user(alert, user_config)
    
    async def _on_ticker_update(self, ticker):
        """处理行情更新"""
        await self.alert_engine.check_ticker_for_all_users(ticker)
    
    async def _on_spread_update(self, spread):
        """处理差价更新"""
        await self.alert_engine.check_spread_for_all_users(spread)
    
    async def start(self):
        """启动系统"""
        logger.info("🦅 鹰眼监控系统启动中...")
        
        try:
            # 启动通知系统
            await self.notifier.start()
            
            # 启动机器人
            await self.bot.start()
            
            logger.info("✅ 系统启动完成")
            
            # 启动 Binance 客户端（会阻塞）
            await self.binance.start()
            
        except Exception as e:
            logger.error(f"启动失败: {e}")
            raise
    
    async def stop(self):
        """停止系统"""
        logger.info("正在停止系统...")
        
        try:
            await self.binance.stop()
            await self.bot.stop()
            await self.notifier.stop()
            logger.info("✅ 系统已停止")
        except Exception as e:
            logger.error(f"停止时出错: {e}")


async def main():
    system = HawkEyeSystem()
    
    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("收到退出信号")
    except Exception as e:
        logger.error(f"系统异常: {e}")
    finally:
        await system.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 再见!")