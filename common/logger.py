#!/usr/bin/env python3
"""
硅基文明知识库 — 统一日志模块
提供 JSON 结构化日志，支持模块级 logger 和统一配置。
"""
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# 默认配置
DEFAULT_LOG_FILE = "logs/silicon-civilization-kb.log"
DEFAULT_LEVEL = logging.INFO


class JsonFormatter(logging.Formatter):
    """JSON 格式化器 — 将日志记录格式化为 JSON"""

    # 内置字段列表（避免 extra 冲突）
    BUILTIN_FIELDS = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # 添加额外字段（从 record.__dict__ 中读取，过滤掉内置字段）
        for key, value in record.__dict__.items():
            if key not in self.BUILTIN_FIELDS and not key.startswith("_"):
                log_obj[key] = value

        # 添加异常信息（如果有）
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, ensure_ascii=False)


def get_logger(
    name: str,
    log_file: Optional[str] = None,
    level: int = DEFAULT_LEVEL,
    stdout: bool = True,
) -> logging.Logger:
    """
    获取模块级日志记录器

    Args:
        name: 日志记录器名称（通常使用 __name__）
        log_file: 日志文件路径（可选，默认使用 DEFAULT_LOG_FILE）
        level: 日志级别（默认 INFO）
        stdout: 是否输出到控制台（默认 True）

    Returns:
        logging.Logger: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 文件输出（统一日志文件）
    file_path = log_file or DEFAULT_LOG_FILE
    Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(file_path, encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    logger.addHandler(fh)

    # 控制台输出（可选）
    if stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(JsonFormatter())
        logger.addHandler(sh)

    return logger


# 预设常用 logger
def get_module_logger(module_name: str) -> logging.Logger:
    """获取模块级 logger（快捷方式）"""
    return get_logger(f"silicon_civilization.{module_name}")


# 导出快捷函数
def debug(logger: logging.Logger, msg: str, **kwargs):
    logger.debug(msg, extra=kwargs if kwargs else None)


def info(logger: logging.Logger, msg: str, **kwargs):
    logger.info(msg, extra=kwargs if kwargs else None)


def warning(logger: logging.Logger, msg: str, **kwargs):
    logger.warning(msg, extra=kwargs if kwargs else None)


def error(logger: logging.Logger, msg: str, **kwargs):
    logger.error(msg, extra=kwargs if kwargs else None)


def critical(logger: logging.Logger, msg: str, **kwargs):
    logger.critical(msg, extra=kwargs if kwargs else None)
