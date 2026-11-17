from pathlib import Path
import json, os
ROOT = Path(__file__).resolve().parents[1]
META = json.loads((ROOT / 'app/project.json').read_text())
MODULES = META['keys']
RULE_VERSION = '2026.11.17-r3'
DB_PATH = os.environ.get('DB_PATH', '/data/workbench.sqlite')
SENTINELS = {'', '-', 'N/A', 'NULL', '未知'}
MAX_BYTES = 2_000_000
DATASETS = {'dirty', 'clean', 'legacy', 'misissue'}
# 各数据集对应的业务上下文；误发批场景单独使用含“月底奖励”规则与撤回参数的上下文。
CONTEXT_FILES = {'misissue': 'context-misissue.json'}
DEFAULT_WITHDRAWAL_RULE = '月底奖励'
