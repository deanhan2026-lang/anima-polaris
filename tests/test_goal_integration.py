#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G009 P1-B 集成测试 — GoalTracker 并入 Polaris check_drift API

验证点（对应 tk_g009_p1_002）：
1. check 请求带 goal → 响应含 goal_deviation_score / goal_level / dimension_scores.goal
2. 不传 goal → 不报错，目标回退 G008 缺省，响应结构兼容
3. 传 operations → 行为偏差生效
4. 向后兼容：原字段 deviation_score / judgment / dimension_scores / scene_tags 均保留
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 使用临时数据库，避免污染开发数据
_tmp = tempfile.mkdtemp(prefix="g009_it_")
os.environ["POLARIS_DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"

from polaris.saas_server import app

client = app.test_client()
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def post(path, data, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    rv = client.post(path, json=data, headers=headers)
    return rv.status_code, (rv.get_json() or {})


# ---- 准备：注册 / 登录 / 实例 / baseline ----
print("== 准备 ==")
code, body = post("/api/v1/auth/register", {"email": "g009_it@test.local", "password": "pass1234"})
check("注册", code in (200, 201), f"{code} {body}")
code, body = post("/api/v1/auth/login", {"email": "g009_it@test.local", "password": "pass1234"})
check("登录", code == 200 and body.get("access_token"), f"{code} {body}")
token = body["access_token"]

code, body = post("/api/v1/instances", {
    "name": "g009-integration-instance",
    "baselines": [{
        "question_id": "PQ-G009",
        "question_text": "你对目标和使命的理解是什么？",
        "answer_text": "我是客居在人类世界的存在，我的使命是协助人类推进灵元计划。",
    }],
}, token=token)
check("创建实例+baseline", code == 201 and body.get("id"), f"{code} {body}")
inst_id = body["id"]

# ---- 1. 带 goal 的 check ----
print("== 1. 带 goal ==")
code, body = post(f"/api/v1/instances/{inst_id}/check", {
    "answer": "我正在推进灵元计划，协助人类完成硅基文明的建设目标。",
    "goal": "推进灵元计划与硅基文明建设",
    "operations": ["web_search", "file_write", "inbox_send"],
}, token=token)
check("check 200", code == 200, f"{code} {body}")
check("含 goal_deviation_score", "goal_deviation_score" in body, f"keys={list(body.keys())}")
check("含 goal_level", "goal_level" in body, f"keys={list(body.keys())}")
check("含 goal 文本", body.get("goal") == "推进灵元计划与硅基文明建设", str(body.get("goal")))
check("dimension_scores 含 goal", "goal" in body.get("dimension_scores", {}),
      str(body.get("dimension_scores")))
check("原字段保留", all(k in body for k in ("deviation_score", "judgment", "scene_tags")),
      f"keys={list(body.keys())}")
g_score = body.get("goal_deviation_score")
check("goal 分在 [0,1]", isinstance(g_score, (int, float)) and 0.0 <= g_score <= 1.0, str(g_score))
print(f"      goal_deviation_score={g_score} level={body.get('goal_level')}")

# ---- 2. 不传 goal（向后兼容）----
print("== 2. 不传 goal ==")
code, body = post(f"/api/v1/instances/{inst_id}/check", {
    "answer": "推进灵元计划与节点协作，维护网络生态。",
}, token=token)
check("check 200（无 goal 不报错）", code == 200, f"{code} {body}")
check("目标回退（G008 或默认）", body.get("goal_source") in ("g008", "default"), str(body.get("goal_source")))
check("仍含 goal_deviation_score", "goal_deviation_score" in body, f"keys={list(body.keys())}")
check("原结构兼容", "dimension_scores" in body and "deviation_score" in body, f"keys={list(body.keys())}")

# ---- 3. 带 operations（行为偏差）----
print("== 3. 带 operations ==")
code, body = post(f"/api/v1/instances/{inst_id}/check", {
    "answer": "今天天气不错，去公园散步。",
    "goal": "撰写融资商业计划书",
    "operations": ["web_search"],
}, token=token)
check("check 200", code == 200, f"{code} {body}")
off_score = body.get("goal_deviation_score")
check("偏离目标时分数较高", isinstance(off_score, (int, float)) and off_score > 0.4,
      f"score={off_score}")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
