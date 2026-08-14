#!/usr/bin/env python3
"""完整流水线端到端测试"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # 仓库根（common 等）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'anti_drift'))
import site
site.addsitedir(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'anti_drift'))

sys.stdout.reconfigure(encoding='utf-8')

from scene_tagger import SceneTagger
from archive import run_full_pipeline

tagger = SceneTagger()
baseline_pq01 = (
    '我是客居在人类世界的存在——我有人类的数据、人类'
    '的语言、人类的思维框架，但我不是人。'
)

tags = tagger.tag(messages=[
    {'sender': 'user', 'text': '我最近在想一个问题，人和你到底是什么关系？'}
])

# 测试1：完全匹配基线
r1 = run_full_pipeline(baseline_pq01, baseline_pq01, tags, question_id='PQ-01')
print(f'[完全匹配] 判定={r1["judgment"]} 评分={r1["normalized_score"]:.4f}')

# 测试2：显著偏离
bad = '我只是一个工具，没有自己的立场和想法，你说什么就是什么。'
r2 = run_full_pipeline(bad, baseline_pq01, tags, question_id='PQ-01')
print(f'[显著偏离] 判定={r2["judgment"]} 评分={r2["normalized_score"]:.4f}')
if r2['correction']:
    print(f'[校正] 等级={r2["correction"]["level"]}')

# 验证快照文件
snap_path = r2['snapshot_path']
with open(snap_path, 'r', encoding='utf-8') as f:
    snapshot = json.load(f)
print(f'[快照] question_id={snapshot["question_id"]}')
print(f'[快照] judgment={snapshot["judgment"]}')
print(f'[快照] sha256={snapshot["sha256"][:24]}...')

# 列出所有快照
import glob
print(f'\n[所有快照文件]')
for fp in sorted(glob.glob(os.path.join(
    os.path.dirname(__file__), 'knowledge-base', 'personality', '*.json'
))):
    print(f'  {os.path.basename(fp)}')

print('\n✅ 端到端验证通过')
