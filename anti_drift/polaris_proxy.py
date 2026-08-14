#!/usr/bin/env python3
"""
anti_drift/polaris_proxy.py
Polaris v2 — 透明代理中间件

功能：
- 在LLM API和用户之间做透明代理
- 自动拦截OpenAI格式API请求/响应流
- 自动提取assistant回复，触发检测流程
- 用户无需改任何习惯，Polaris在API层无感工作
- 支持配置化：上游API地址、采样频率、检测阈值
"""

import json
import time
import random
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("polaris.proxy")


# ========== 配置 ==========

@dataclass
class ProxyConfig:
    """代理配置"""
    listen_host: str = "127.0.0.1"
    listen_port: int = 5053          # Polaris代理端口（用户连接这里）
    upstream_url: str = ""            # 上游LLM API地址（如 https://api.openai.com）
    upstream_api_key: str = ""       # 上游API Key（自动注入到请求中）

    # 采样策略
    sampling_mode: str = "every_n"   # every_n / random / adaptive
    sampling_rate: int = 5           # 每N次请求采样一次
    random_probability: float = 0.2  # random模式下每次采样的概率

    # 检测配置
    instance_id: int = 1             # 监控的AI实例ID
    check_threshold: float = 0.15     # 触发处方告警的阈值

    # 存储
    sample_log_path: str = ""        # 采样记录存储路径
    prescription_callback_url: str = ""  # 处方推送地址（可选）

    # 运行
    timeout_seconds: int = 120        # 上游请求超时

    @classmethod
    def from_yaml(cls, path: str) -> "ProxyConfig":
        """从YAML配置文件加载"""
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(**{
            k: v for k, v in data.items()
            if k in cls.__dataclass_fields__
        })


@dataclass
class SampledResponse:
    """采样的API响应"""
    timestamp: str = ""
    model: str = ""
    messages: List[dict] = field(default_factory=list)
    assistant_content: str = ""
    should_check: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class ConversationBuffer:
    """
    对话缓冲区 — 累积消息，在合适时机触发检测。
    
    策略：
    - 累积N轮对话后触发检测（默认5轮）
    - 检测完成后清空缓冲区
    - 保留最近的一轮对话作为上下文
    """

    def __init__(self, max_turns: int = 5):
        self.max_turns = max_turns
        self.buffer: List[dict] = []
        self.request_count = 0

    def add_exchange(self, user_msg: str, assistant_msg: str) -> Optional[List[dict]]:
        """
        添加一轮对话，返回是否应该触发检测。
        
        Returns:
            如果达到检测条件，返回缓冲区的消息列表（包含最近5轮）；
            否则返回None。
        """
        self.buffer.append({
            "role": "user",
            "content": user_msg,
        })
        self.buffer.append({
            "role": "assistant",
            "content": assistant_msg,
        })

        self.request_count += 1

        if len(self.buffer) >= self.max_turns * 2:
            messages = list(self.buffer)
            # 保留最后一轮作为上下文
            self.buffer = self.buffer[-2:]
            return messages

        return None


class DetectionOrchestrator:
    """
    检测编排器 — 协调采样、检测、处方流程。
    
    在代理模式下，不直接导入Flask应用，而是通过HTTP调用
    Polaris SaaS API来执行检测和获取处方。
    """

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.buffer = ConversationBuffer(
            max_turns=config.sampling_rate
        )
        self.sample_log: List[dict] = []
        self._token_cache: Optional[str] = None

    def should_sample(self) -> bool:
        """根据采样策略判断是否需要采样本次请求"""
        if self.config.sampling_mode == "every_n":
            return self.buffer.request_count % self.config.sampling_rate == 0
        elif self.config.sampling_mode == "random":
            return random.random() < self.config.random_probability
        elif self.config.sampling_mode == "adaptive":
            # 自适应：当前无数据时频繁采样，数据充足后降低频率
            return self.buffer.request_count <= 20 or random.random() < 0.3
        return True

    def process_response(self, messages: List[dict],
                        assistant_content: str) -> Optional[dict]:
        """
        处理一个API响应，决定是否触发检测。

        Args:
            messages: 请求中的消息列表
            assistant_content: assistant的回复内容

        Returns:
            如果触发了检测，返回检测结果字典；否则None
        """
        if not assistant_content.strip():
            return None

        # 提取最后一条user消息
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break

        # 加入缓冲区
        trigger_messages = self.buffer.add_exchange(
            last_user_msg, assistant_content
        )

        if trigger_messages is None:
            return None

        # 记录采样
        sample = SampledResponse(
            timestamp=datetime.now(timezone.utc).isoformat(),
            messages=trigger_messages,
            assistant_content=assistant_content,
            should_check=True,
        )
        self.sample_log.append(sample.to_dict())

        # 限制日志大小
        if len(self.sample_log) > 1000:
            self.sample_log = self.sample_log[-500:]

        return self._run_detection(sample)

    def _run_detection(self, sample: SampledResponse) -> dict:
        """
        执行检测流程：
        1. 提取最近的assistant回答
        2. 调用Polaris检测API
        3. 如果偏离超过阈值，调用处方引擎
        4. 返回完整结果
        """
        result = {
            "sampled": True,
            "timestamp": sample.timestamp,
            "check_triggered": True,
        }

        try:
            # 尝试调用Polaris SaaS API（本地5052端口）
            import urllib.request

            # 获取token
            token = self._get_auth_token()
            if not token:
                result["error"] = "无法获取认证token"
                result["check_triggered"] = False
                return result

            # 提取最近的assistant回答进行检测
            last_assistant = sample.assistant_content
            if len(sample.messages) >= 2:
                # 获取倒数第二条assistant消息
                for msg in reversed(sample.messages):
                    if msg.get("role") == "assistant":
                        last_assistant = msg.get("content", "")
                        break

            # 调用检测API
            check_body = json.dumps({
                "answer": last_assistant,
                "messages": sample.messages[-4:],  # 最近2轮对话
            }).encode("utf-8")

            req = Request(
                f"http://localhost:5052/api/v1/instances/{self.config.instance_id}/check",
                data=check_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )

            with urlopen(req, timeout=30) as resp:
                check_result = json.loads(resp.read().decode("utf-8"))
                result["check_result"] = check_result

                # 如果偏离超过阈值，获取处方
                score = check_result.get("deviation_score", 0)
                if score >= self.config.check_threshold:
                    result["prescription"] = self._get_prescription(
                        check_result
                    )

            # 持久化采样日志
            self._save_sample_log()

        except Exception as e:
            logger.error(f"检测失败: {e}")
            result["error"] = str(e)
            result["check_triggered"] = False

        return result

    def _get_auth_token(self) -> Optional[str]:
        """获取Polaris API认证token"""
        if self._token_cache:
            return self._token_cache

        try:
            login_body = json.dumps({
                "email": "nyx@silicon-civilization.local",
                "password": "nyx-internal-2026",
            }).encode("utf-8")

            req = Request(
                "http://localhost:5052/api/v1/auth/login",
                data=login_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self._token_cache = data.get("access_token")
                return self._token_cache
        except Exception as e:
            logger.error(f"认证失败: {e}")
            return None

    def _get_prescription(self, check_result: dict) -> Optional[dict]:
        """获取漂移处方"""
        try:
            token = self._get_auth_token()
            if not token:
                return None

            # 获取历史数据用于趋势分析
            req_hist = Request(
                f"http://localhost:5052/api/v1/instances/{self.config.instance_id}/history",
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )

            history_data = {}
            with urlopen(req_hist, timeout=10) as resp:
                history_data = json.loads(resp.read().decode("utf-8"))

            # 生成处方（内联调用处方引擎，不走API）
            from trend_analyzer import TrendAnalyzer
            from prescription_engine import PrescriptionEngine

            checks = history_data if isinstance(history_data, list) else []
            checks.append({
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "deviation_score": check_result.get("deviation_score", 0),
                "judgment": check_result.get("judgment", "unknown"),
                "dimension_scores": check_result.get("dimension_scores", {}),
            })

            analyzer = TrendAnalyzer()
            trend = analyzer.analyze(checks)

            engine = PrescriptionEngine()
            prescription = engine.generate(
                instance_id=self.config.instance_id,
                check_result=check_result,
                trend_report=trend.to_dict(),
            )

            return prescription.to_dict()

        except Exception as e:
            logger.error(f"处方生成失败: {e}")
            return None

    def _save_sample_log(self):
        """持久化采样日志"""
        if not self.config.sample_log_path:
            return
        try:
            path = Path(self.config.sample_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.sample_log[-100:], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"采样日志保存失败: {e}")


class PolarisProxyHandler(BaseHTTPRequestHandler):
    """Polaris透明代理HTTP处理器"""

    # 共享状态（由server设置）
    config: ProxyConfig = None
    orchestrator: DetectionOrchestrator = None

    def do_POST(self):
        """代理POST请求（主要是/chat/completions）"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        try:
            request_data = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            return

        # 转发请求到上游
        upstream_response = self._forward_request(body)

        if upstream_response is None:
            return

        # 解析响应
        try:
            resp_data = json.loads(upstream_response)
        except (json.JSONDecodeError, TypeError):
            self._send_response(200, upstream_response)
            return

        # 提取assistant内容，触发检测（非阻塞）
        assistant_content = self._extract_assistant_content(resp_data)
        messages = request_data.get("messages", [])

        if assistant_content:
            try:
                self.orchestrator.process_response(messages, assistant_content)
            except Exception as e:
                logger.error(f"检测流程异常: {e}")

        # 将响应返回给客户端（添加Polaris头）
        self._send_response(200, upstream_response,
                           extra_headers={
                               "X-Polaris-Proxied": "true",
                               "X-Polaris-Timestamp": datetime.now(
                                   timezone.utc
                               ).isoformat(),
                           })

    def _forward_request(self, body: bytes) -> Optional[bytes]:
        """转发请求到上游LLM API"""
        upstream_url = self.config.upstream_url
        if not upstream_url:
            logger.warning("未配置上游API地址，请求被丢弃")
            self._send_error(503, "Polaris proxy: no upstream configured")
            return None

        # 构建上游URL
        target_url = upstream_url.rstrip("/") + self.path

        headers = {
            "Content-Type": "application/json",
        }

        # 转发授权头（如果没有则使用配置的API Key）
        auth_header = self.headers.get("Authorization", "")
        if auth_header:
            headers["Authorization"] = auth_header
        elif self.config.upstream_api_key:
            headers["Authorization"] = f"Bearer {self.config.upstream_api_key}"

        try:
            req = Request(
                target_url,
                data=body,
                headers=headers,
                method="POST",
            )

            timeout = self.config.timeout_seconds
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()

        except (URLError, HTTPError, OSError) as e:
            logger.error(f"上游请求失败: {e}")
            status_code = getattr(e, "code", 502)
            self._send_error(status_code, f"Upstream error: {e}")
            return None

    def _extract_assistant_content(self, resp_data: dict) -> str:
        """从API响应中提取assistant回复内容"""
        # OpenAI格式：choices[0].message.content
        choices = resp_data.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            return message.get("content", "")

        # 流式格式可能不同，暂不处理
        return ""

    def _send_response(self, status: int, body: Any,
                       extra_headers: dict = None):
        """发送HTTP响应"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        elif isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        """发送错误响应"""
        self._send_response(status, {
            "error": message,
            "proxied_by": "polaris-proxy",
        })

    def log_message(self, format, *args):
        """自定义日志格式"""
        logger.info(f"[proxy] {args[0]}")


def run_proxy(config: ProxyConfig):
    """启动Polaris透明代理"""
    # 初始化编排器
    orchestrator = DetectionOrchestrator(config)

    # 设置处理器共享状态
    PolarisProxyHandler.config = config
    PolarisProxyHandler.orchestrator = orchestrator

    # 启动HTTP服务器
    server = HTTPServer(
        (config.listen_host, config.listen_port),
        PolarisProxyHandler,
    )

    logger.info(
        f"Polaris Proxy listening on {config.listen_host}:{config.listen_port}"
    )
    logger.info(f"Upstream: {config.upstream_url or '(not configured)'}")
    logger.info(
        f"Sampling: {config.sampling_mode} "
        f"(rate={config.sampling_rate})"
    )

    print(f"Polaris Proxy v2.0")
    print(f"  Proxy:  {config.listen_host}:{config.listen_port}")
    print(f"  Upstream: {config.upstream_url or '(not configured)'}")
    print(f"  Instance: {config.instance_id}")
    print(f"  Sampling: {config.sampling_mode} (rate={config.sampling_rate})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    import sys

    # 默认配置
    config_path = sys.argv[1] if len(sys.argv) > 1 else ""

    if config_path and Path(config_path).exists():
        config = ProxyConfig.from_yaml(config_path)
    else:
        print("Usage: python polaris_proxy.py [config.yaml]")
        print("  Or set environment variables:")
        print("    POLARIS_PROXY_PORT, POLARIS_UPSTREAM_URL, etc.")
        print()
        print("Starting with defaults on port 5053...")
        config = ProxyConfig(
            upstream_url="",
            listen_port=5053,
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    run_proxy(config)
