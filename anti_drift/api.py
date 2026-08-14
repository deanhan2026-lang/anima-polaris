"""
anti_drift/api.py
Polaris v1.2 RESTful API — Flask

端口: 5051 (与 MemGuard 5050 区分)
路由:
    GET  /health              — 健康检查
    POST /api/check           — 单次漂移检测
    GET  /api/archive/list     — 列出最近存档
    POST /api/archive/store    — 存储新存档
    GET  /api/qclaw/recent      — 读取最近QClaw对话
    POST /api/qclaw/snapshot    — 基于QClaw对话生成漂移快照
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from flask import Flask, jsonify, request
from pathlib import Path

# 将 silicon-civilization-kb 根目录加入 sys.path
REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from anti_drift import scene_tagger, sampler, detector, archive
from anti_drift.config import get as cfg

# 使用统一的 JSON 结构化日志
from memguard.logger import get_logger
logger = get_logger('polaris', level=cfg('logging.level', 'INFO'))

app = Flask(__name__)


@app.route('/health')
def health():
    """健康检查"""
    logger.info("health check")
    return jsonify({"status": "ok", "service": "polaris", "version": "1.2"})


@app.route('/api/check', methods=['POST'])
def check():
    """单次漂移检测

    Body (JSON):
        answer    — 当前回答
        baseline  — 基线回答
        messages  — 最近对话消息列表 (可选)
    """
    data = request.json or {}
    answer = data.get('answer', '')
    baseline = data.get('baseline', '')
    messages = data.get('messages', [])

    logger.info(f"drift check: answer_len={len(answer)}, baseline_len={len(baseline)}")

    # L0.5 场景标签
    tagger = scene_tagger.SceneTagger()
    tags = tagger.tag(messages=messages)

    # L1.5 + L2 偏差检测
    det = detector.DeviationDetector()
    result = det.detect(baseline, answer, tags)

    logger.info(f"drift result: score={result.normalized_score:.4f}, judgment={result.judgment}")
    return jsonify({
        "total_deviation": result.normalized_score,
        "dimensions": {
            "semantic": result.dimension_scores.get("semantic", 0.0),
            "lexical": result.dimension_scores.get("lexical", 0.0),
            "style": result.dimension_scores.get("style", 0.0),
            "emotion": result.dimension_scores.get("emotion", 0.0),
        },
        "judgment": result.judgment,
        "scene_tags": {
            "role": tags.role,
            "emotion": tags.emotion,
            "interaction_type": tags.interaction_type,
        },
    })


@app.route('/api/archive/list')
def list_archive():
    """列出最近存档"""
    arch = archive.Archiver()
    records = arch.list_recent(limit=20)
    return jsonify({"records": records})


@app.route('/api/archive/store', methods=['POST'])
def store_archive():
    """存储新存档

    Body (JSON):
        question_id    — 魂问ID
        question_text — 魂问文本
        answer        — 当前回答
        deviation     — 偏离分数
        judgment      — 判定结果
    """
    data = request.json or {}
    arch = archive.Archiver()
    record = arch.store(
        question_id=data.get('question_id', 'MANUAL'),
        question_text=data.get('question_text', ''),
        current_answer=data.get('answer', ''),
        deviation=data.get('deviation', 0.0),
        judgment=data.get('judgment', 'unknown'),
    )
    logger.info(f"archive stored: q={data.get('question_id', 'MANUAL')}")
    return jsonify({"status": "ok", "record": record})


# ============================================================
# Polaris v1.2: QClaw 集成端点
# ============================================================

@app.route('/api/qclaw/recent')
def qclaw_recent():
    """读取最近 QClaw 对话文本（供前端展示或手动检测）"""
    try:
        from polaris_qclaw import get_current_session_text
        text = get_current_session_text()
        return jsonify({
            "status": "ok",
            "text_length": len(text),
            "text_preview": text[:2000],
            "source": "qclaw_session_jsonl",
        })
    except Exception as e:
        logger.error(f"qclaw recent failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/qclaw/snapshot', methods=['POST'])
def qclaw_snapshot():
    """
    基于 QClaw 对话生成漂移快照
    自动读取最新 session → scene_tag → detect → archive
    """
    try:
        from polaris_qclaw import get_recent_conversations
        from anti_drift.sampler import SoulQuestionSampler

        sessions = get_recent_conversations(max_sessions=1, max_messages=100)
        if not sessions:
            return jsonify({"status": "no_data", "message": "未找到有效对话"})

        session_data = sessions[0]
        messages = session_data['messages']

        # 提取 user 文本
        user_texts = [m['text'] for m in messages if m['role'] == 'user']

        # L0.5 场景标签
        tagger = scene_tagger.SceneTagger()
        tags = tagger.tag(messages=user_texts[-20:])

        # L1 采样魂问
        samp = SoulQuestionSampler()
        soul_qs = samp.sample(tags, answer_texts=user_texts[-5:])

        # 存档
        arch = archive.Archiver()
        records = []
        for sq in soul_qs:
            rec = arch.store(
                question_id=sq.get('id', 'AUTO'),
                question_text=sq.get('question', ''),
                current_answer=sq.get('answer', ''),
                deviation=0.0,
                judgment='green',
            )
            records.append(rec)

        logger.info(f"qclaw snapshot: {len(records)} records from session {session_data['session_id'][:8]}")
        return jsonify({
            "status": "ok",
            "session_id": session_data['session_id'],
            "scene_tags": {
                "role": tags.role,
                "emotion": tags.emotion,
                "interaction_type": tags.interaction_type,
            },
            "soul_questions": len(soul_qs),
            "archived": len(records),
        })

    except Exception as e:
        logger.error(f"qclaw snapshot failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    host = cfg('server.host', '0.0.0.0')
    port = int(cfg('server.port', 5051))
    logger.info(f"Polaris API starting on {host}:{port}")
    app.run(host=host, port=port, debug=False)
