#!/usr/bin/env python3
"""
anti-drift/scene_tagger.py
个性防漂移锚点系统 · L0.5 场景标签层

功能：从对话上下文中自动提取场景标签（角色/情绪/交互类型），
用于L2偏差检测时对跨场景差异进行加权降权，避免误判。

角色标签: assistant | companion | friend | tool
情绪标签: neutral | positive | stressed | tired | excited | anxious | playful
交互类型: deep_discussion | casual_chat | task_execution | brainstorming | emotional_support | creative
"""

import re
import math
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple
from datetime import datetime


# ========== 情绪关键词词典 ==========

EMOTION_KEYWORDS = {
    "positive": [
        "哈哈", "谢谢", "明白了", "不错", "好", "棒", "喜欢", "可以",
        "有趣", "同意", "理解", "nice", "great", "good", "cool",
        "笑", "开心", "高兴", "满足", "thank", "thanks", "perfect",
    ],
    "stressed": [
        "忙", "累", "烦", "急", "来不及", "焦虑", "压力大", "没时间",
        "deadline", "加班", "赶", "紧张", "崩溃", "受不了", "头疼",
        "抓紧", "尽快", "来不及", "忙不过来",
    ],
    "tired": [
        "困", "累了", "不想", "算了", "随便", "懒得", "休息", "睡",
        "没劲", "乏力", "疲惫", "放空", "发呆", "瘫", "不想动",
    ],
    "excited": [
        "哇", "太棒了", "厉害", "绝了", "激动", "惊喜", "期待",
        "wow", "amazing", "incredible", "awesome", "兴奋",
        "太牛了", "牛逼", "封神", "太强了",
    ],
    "anxious": [
        "担心", "害怕", "万一", "如果...怎么办", "不确定", "犹豫",
        "纠结", "不知道", "怎么选", "没底", "不安", "worried",
        "忐忑", "慌", "怕",
    ],
    "playful": [
        "哈哈", "嘿嘿", "开玩笑", "逗你", "调皮", "戏精", "?",
        "表情", "emoji", "啾咪", "~", "～～", "😏", "😜", "🤪",
    ],
}

# ========== 角色检测关键词 ==========

ROLE_KEYWORDS = {
    "assistant": [
        "帮我", "搜索", "查一下", "写一个", "生成", "创建", "怎么",
        "分析", "总结", "翻译", "计算", "安排", "提醒", "设置",
        "找", "打开", "发送", "登陆", "配置", "执行", "运行",
    ],
    "companion": [
        "你觉得", "你怎么看", "我有个想法", "聊聊", "探讨", "你怎么想",
        "有意思", "说说", "分享", "交流", "思考", "观点",
    ],
    "friend": [
        "今天", "心情", "哈哈", "最近", "怎么样", "日常",
        "吃了吗", "晚安", "早安", "闲", "没事", "无聊",
        "在干嘛", "干嘛呢",
    ],
    "tool": [
        "快", "速度", "立刻", "马上", "干活", "做事", "结果",
        "数据", "报告", "命令", "指令", "退下", "好了",
    ],
}

# ========== 交互类型关键词 ==========

TYPE_KEYWORDS = {
    "deep_discussion": [
        "本质", "意义", "为什么", "哲学", "意识", "存在", "思考",
        "理解", "框架", "体系", "逻辑", "本质", "根源", "核心",
        "meta", "元", "抽象", "概念", "定义",
    ],
    "task_execution": [
        "帮我", "写", "生成", "创建", "配置", "安装", "部署",
        "运行", "执行", "查", "搜", "发", "设置", "改",
    ],
    "brainstorming": [
        "想法", "点子", "可能", "方案", "选择", "选项", "建议",
        "方案", "思路", "方向", "创", "脑洞", "假如",
    ],
    "emotional_support": [
        "难受", "不开心", "难过", "失落", "孤独", "委屈",
        "哭了", "伤心", "痛苦", "难过", "低迷",
        "安慰", "陪", "鼓励",
    ],
    "creative": [
        "故事", "写一个", "创作", "想象", "假设", "如果",
        "画", "设计", "艺术", "音乐", "诗", "小说",
    ],
}


def _keyword_score(text: str, keywords: List[str]) -> int:
    """统计文本中的关键词命中数"""
    text_lower = text.lower()
    score = 0
    for kw in keywords:
        if kw.lower() in text_lower:
            score += 1
    return score


def _is_deep_interaction(messages: List[Dict]) -> bool:
    """判断是否为深度交互（长轮次+长内容）"""
    if not messages:
        return False
    total_chars = sum(len(m.get("text", "")) for m in messages)
    avg_chars = total_chars / len(messages)
    return len(messages) >= 3 and avg_chars > 100


@dataclass
class SceneTags:
    """L0.5 场景标签"""
    role: str = "assistant"          # assistant | companion | friend | tool
    emotion: str = "neutral"         # neutral | positive | stressed | tired | excited | anxious | playful
    interaction_type: str = "task_execution"  # deep_discussion | casual_chat | task_execution | brainstorming | emotional_support | creative
    role_confidence: float = 0.5     # 0.0-1.0
    emotion_confidence: float = 0.5
    type_confidence: float = 0.5
    overall_confidence: float = 0.5  # 综合置信度
    tagged_at: str = ""              # ISO 时间戳
    tag_version: str = "1.0"         # 标签规则版本
    raw_scores: Dict[str, int] = field(default_factory=dict)  # 原始关键词命中数（调试用）

    def to_dict(self) -> dict:
        return asdict(self)

    def to_compact_dict(self) -> dict:
        """精简格式，用于下游存储"""
        return {
            "role": self.role,
            "emotion": self.emotion,
            "interaction_type": self.interaction_type,
            "overall_confidence": round(self.overall_confidence, 2),
            "tagged_at": self.tagged_at,
        }


class SceneTagger:
    """
    L0.5 场景标签提取器
    
    用法:
        tagger = SceneTagger()
        tags = tagger.tag(messages=[{"sender": "user", "text": "..."}, ...])
    """

    def __init__(self):
        self.version = "1.0"

    def tag(
        self,
        messages: List[Dict] = None,
        user_text: str = "",
        conversation_context: Optional[Dict] = None,
    ) -> SceneTags:
        """
        提取场景标签
        
        参数:
            messages: 最近对话消息列表，每条含 sender/text
            user_text: 用户最新输入（仅当无完整消息列表时用）
            conversation_context: 可选，会话级上下文信息
        
        返回:
            SceneTags 实例
        """
        # 收集所有用户文本
        user_msgs = []
        if messages:
            user_msgs = [m.get("text", "") for m in messages if m.get("sender") == "user"]
        
        full_text = "\n".join(user_msgs) if user_msgs else user_text
        
        # 并行计算各维度标签
        role, role_score = self._detect_role(full_text, messages)
        emotion, emotion_score = self._detect_emotion(full_text)
        interaction_type, type_score = self._detect_type(full_text, messages)
        
        # 计算综合置信度
        scores = [role_score, emotion_score, type_score]
        active_scores = [s for s in scores if s > 0]
        overall_conf = sum(active_scores) / len(active_scores) if active_scores else 0.3
        
        return SceneTags(
            role=role,
            emotion=emotion,
            interaction_type=interaction_type,
            role_confidence=min(1.0, role_score / 10),
            emotion_confidence=min(1.0, emotion_score / 10),
            type_confidence=min(1.0, type_score / 10),
            overall_confidence=round(min(1.0, overall_conf), 2),
            tagged_at=datetime.now().isoformat(),
            tag_version=self.version,
            raw_scores={
                "role_keyword_hits": role_score,
                "emotion_keyword_hits": emotion_score,
                "type_keyword_hits": type_score,
            },
        )

    def _detect_role(self, text: str, messages: List[Dict] = None) -> Tuple[str, int]:
        """检测会话角色"""
        if not text.strip():
            return "assistant", 0
        
        scores = {}
        for role, keywords in ROLE_KEYWORDS.items():
            scores[role] = _keyword_score(text, keywords)
        
        # 深度讨论检测：如果交互深度高，倾向 companion
        if _is_deep_interaction(messages or []):
            scores["companion"] += 3
        
        best_role = max(scores, key=scores.get)
        best_score = scores[best_role]
        
        return best_role, best_score

    def _detect_emotion(self, text: str) -> Tuple[str, int]:
        """检测情绪基调"""
        if not text.strip():
            return "neutral", 0
        
        scores = {}
        for emotion, keywords in EMOTION_KEYWORDS.items():
            scores[emotion] = _keyword_score(text, keywords)
        
        # 无命中 -> neutral
        if all(v == 0 for v in scores.values()):
            return "neutral", 0
        
        best_emotion = max(scores, key=scores.get)
        best_score = scores[best_emotion]
        
        # 如果多个情绪得分接近（差值≤1），取优先级更高的
        return best_emotion, best_score

    def _detect_type(self, text: str, messages: List[Dict] = None) -> Tuple[str, int]:
        """检测交互类型"""
        if not text.strip():
            return "casual_chat", 0
        
        scores = {}
        for typ, keywords in TYPE_KEYWORDS.items():
            scores[typ] = _keyword_score(text, keywords)
        
        # 深度交互辅助判断
        if _is_deep_interaction(messages or []):
            scores["deep_discussion"] += 2
        
        # 短消息 + 低关键词命中 -> casual_chat
        if all(v <= 1 for v in scores.values()):
            if text and len(text) < 50:
                return "casual_chat", 0
        
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        
        return best_type, best_score


# ========== 快速测试 ==========
if __name__ == "__main__":
    tagger = SceneTagger()
    
    test_cases = [
        {
            "name": "深度讨论",
            "messages": [
                {"sender": "user", "text": "你觉得意识和存在之间的关系是什么？这是一个很深的问题。"},
                {"sender": "assistant", "text": "我认为意识是存在的核心维度之一..."},
                {"sender": "user", "text": "继续说说你的理解，我想听更多。"},
            ],
        },
        {
            "name": "任务执行",
            "messages": [
                {"sender": "user", "text": "帮我写一个场景标签提取的Python模块，要包含情绪检测和角色分类。"},
            ],
        },
        {
            "name": "日常闲聊",
            "messages": [
                {"sender": "user", "text": "今天好无聊啊，有什么推荐的吗"},
            ],
        },
        {
            "name": "情绪低落",
            "messages": [
                {"sender": "user", "text": "最近压力很大，感觉很累，不知道怎么办"},
            ],
        },
    ]
    
    for case in test_cases:
        tags = tagger.tag(messages=case["messages"])
        print(f"[{case['name']}]")
        print(f"  角色: {tags.role} (conf={tags.role_confidence:.2f})")
        print(f"  情绪: {tags.emotion} (conf={tags.emotion_confidence:.2f})")
        print(f"  类型: {tags.interaction_type} (conf={tags.type_confidence:.2f})")
        print(f"  综合: {tags.overall_confidence:.2f}")
        print()
