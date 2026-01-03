"""
日志配置 - 结构化日志
"""
import sys
import os
from loguru import logger
from datetime import datetime


def setup_logging(
    log_level: str = "INFO",
    log_dir: str = "logs",
    app_name: str = "hawkeye"
):
    """
    配置日志系统
    
    Args:
        log_level: 日志级别 (DEBUG, INFO, WARNING, ERROR)
        log_dir: 日志目录
        app_name: 应用名称
    """
    
    # 创建日志目录
    os.makedirs(log_dir, exist_ok=True)
    
    # 移除默认处理器
    logger.remove()
    
    # 控制台输出 - 简洁彩色格式
    logger.add(
        sys.stdout,
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> | {message}",
        colorize=True,
        filter=lambda record: record["level"].no < 40,  # INFO及以下
    )
    
    # 控制台错误输出
    logger.add(
        sys.stderr,
        level="WARNING",
        format="<red>{time:HH:mm:ss}</red> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | {message}",
        colorize=True,
        filter=lambda record: record["level"].no >= 40,  # WARNING及以上
    )
    
    # 主日志文件 - 详细格式
    logger.add(
        os.path.join(log_dir, f"{app_name}.log"),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="100 MB",
        retention="7 days",
        compression="gz",
        encoding="utf-8",
    )
    
    # 错误日志文件 - 单独记录
    logger.add(
        os.path.join(log_dir, "error.log"),
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} | {message}\n{exception}",
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
        backtrace=True,
        diagnose=True,
    )
    
    # 报警日志文件 - 单独记录所有报警
    logger.add(
        os.path.join(log_dir, "alerts.log"),
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        rotation="50 MB",
        retention="30 days",
        filter=lambda record: "报警" in record["message"] or "🔔" in record["message"],
    )
    
    logger.info(f"日志系统已初始化: level={log_level}, dir={log_dir}")
    
    return logger


def get_logger(name: str = None):
    """获取logger实例"""
    if name:
        return logger.bind(name=name)
    return logger


class LogContext:
    """日志上下文管理器 - 用于追踪请求/操作"""
    
    def __init__(self, operation: str, **kwargs):
        self.operation = operation
        self.kwargs = kwargs
        self.start_time = None
    
    def __enter__(self):
        self.start_time = datetime.now()
        logger.debug(f"开始: {self.operation}", **self.kwargs)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = (datetime.now() - self.start_time).total_seconds() * 1000
        
        if exc_type:
            logger.error(
                f"失败: {self.operation} ({duration:.1f}ms) - {exc_val}",
                **self.kwargs
            )
        else:
            logger.debug(f"完成: {self.operation} ({duration:.1f}ms)", **self.kwargs)
        
        return False  # 不抑制异常


# 便捷函数
def log_alert(symbol: str, alert_type: str, message: str, user_id: str = None):
    """记录报警日志"""
    logger.info(f"🔔 报警 | {symbol} | {alert_type} | {message} | user={user_id}")


def log_error(component: str, error: Exception, context: str = ""):
    """记录错误日志"""
    logger.error(f"❌ {component} | {type(error).__name__}: {error} | {context}")


def log_ws_event(ws_type: str, event: str, details: str = ""):
    """记录WebSocket事件"""
    logger.debug(f"📡 WS-{ws_type} | {event} | {details}")


def log_user_action(user_id: str, action: str, details: str = ""):
    """记录用户操作"""
    logger.info(f"👤 用户 {user_id} | {action} | {details}")