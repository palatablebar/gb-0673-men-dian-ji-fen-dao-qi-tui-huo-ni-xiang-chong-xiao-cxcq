"""误发撤回模块的标准库离线验证（不依赖 fastapi/pytest）。

运行：python3 scripts/verify_withdrawal.py
覆盖：撤回裁决、反事实重放、守恒、发布幂等、乐观锁、失败回滚、回滚恢复、历史可追溯。
"""
import sys
import types
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 为不依赖 fastapi 的纯规则执行打桩。
for name in ('fastapi',):
    sys.modules.setdefault(name, types.ModuleType(name))
fastapi = sys.modules['fastapi']
class _Router:
    def __init__(self, *a, **k): pass
    def get(self, *a, **k):
        def deco(fn): return fn
        return deco
    post = get
fastapi.APIRouter = _Router
for sub in ('requests', 'responses', 'templating', 'staticfiles'):
    mod = types.ModuleType('fastapi.' + sub)
    sys.modules['fastapi.' + sub] = mod
    setattr(fastapi, sub, mod)
fastapi.Request = object
fastapi.Body = lambda *a, **k: None
sys.modules['fastapi.responses'].JSONResponse = object
sys.modules['fastapi.responses'].Response = object
sys.modules['fastapi.responses'].RedirectResponse = object
fake_tpl = types.ModuleType('fastapi.templating')
class _Jinja:
    def __init__(self, *a, **k): pass
    def TemplateResponse(self, *a, **k): return None
fake_tpl.Jinja2Templates = _Jinja
sys.modules['fastapi.templating'] = fake_tpl
fake_static = types.ModuleType('fastapi.staticfiles')
fake_static.StaticFiles = object
sys.modules['fastapi.staticfiles'] = fake_static

from app.engine import evaluate
from app import store
from app.modules.withdrawal.rules import plan_withdrawal, FULL, PARTIAL

failures = []

def check(name, cond, detail=''):
    mark = 'PASS' if cond else 'FAIL'
    print(f'[{mark}] {name}' + (f' :: {detail}' if detail and not cond else ''))
    if not cond:
        failures.append(name)

state = evaluate({'dataset': 'misissue'}, until=8)
plan = state['withdrawal_plan']
recon = plan['reconciliation']
by_member = {r['member']: r for r in plan['rows']}

print('\n== 户级裁决 ==')
expected = {
    '00001': (FULL, 90, 0, 0),
    '00002': (FULL, 70, 0, 0),
    '00007': (FULL, 40, 0, 0),
    '00003': (PARTIAL, 10, 30, 30),
    '00004': (PARTIAL, 30, 20, 20),
    '00005': (PARTIAL, 0, 40, 40),
    '00006': (PARTIAL, 0, 60, 60),
    '00008': (PARTIAL, 0, 50, 50),
}
for member, (decision, clawback, lost, forfeited) in expected.items():
    row = by_member[member]
    check(f'{member} 裁决={decision}', row['decision'] == decision)
    check(f'{member} 撤回额={clawback}', row['clawback'] == clawback)
    check(f'{member} 不可追={lost}', row['lost'] == lost)
    check(f'{member} 账面核销={forfeited}', row['forfeited'] == forfeited)
    check(f'{member} 误发=撤回+核销', row['wrong_issued'] == row['clawback'] + row['forfeited'])
    check(f'{member} 撤回不超误发', row['clawback'] <= row['wrong_issued'])
    check(f'{member} 撤回后余额非负', row['after_balance'] >= 0)

print('\n== 整体对账 ==')
check('撤回户数=8', recon['members'] == 8)
check('整批撤销=3', recon['full_revoke'] == 3)
check('部分撤回=5', recon['partial_revoke'] == 5)
check('误发总额=440', recon['wrong_issued_total'] == 440, str(recon['wrong_issued_total']))
check('撤回总额=240', recon['clawback_total'] == 240, str(recon['clawback_total']))
check('不可追=200', recon['lost_total'] == 200, str(recon['lost_total']))
check('账面核销=200', recon['forfeited_total'] == 200, str(recon['forfeited_total']))
check('误发=撤回+核销', recon['wrong_issued_total'] ==
      recon['clawback_total'] + recon['forfeited_total'])
check('户内余额等式成立', recon['balanced'])
check('无负余额', recon['no_negative'])
check('总余额等式成立', recon['grand_balanced'])
check('撤回不超误发', recon['within_issued'])
check('反事实重放已核对', recon['replay_verified'])
check('总余额 前-撤回=后', recon['pre_grand_total'] - recon['clawback_total'] == recon['post_grand_total'])

print('\n== 历史记录不可变、可追溯 ==')
record_ids = {r['id'] for r in state['records']}
for wrong in ('W101', 'W102', 'W103', 'W104', 'W105', 'W106', 'W107', 'W108'):
    check(f'误发原始发放保留 {wrong}', wrong in record_ids)
for spend in ('S101', 'S103', 'S105', 'S108'):
    check(f'原始消费保留 {spend}', spend in record_ids)
for refund in ('R103', 'R104', 'R106'):
    check(f'原始退货保留 {refund}', refund in record_ids)
check('REVOKE 为追加流水', all(e['action'] == 'REVOKE' for e in plan['revoke_entries']))
check('REVOKE 笔数与撤回批次一致',
      len(plan['revoke_entries']) == sum(1 for r in plan['rows']
                                         for x in r['revoke_lots'] if x['revoke'] > 0))
# 消费退款的重放结果没有被撤回逻辑改动
check('原 SPEND 数不变', len(state['point_ledger']['spends']) ==
      sum(1 for r in state['records'] if r['type'] == 'SPEND'))
check('原 REFUND 数不变', len(state['point_ledger']['refunds']) ==
      sum(1 for r in state['records'] if r['type'] == 'REFUND'))

print('\n== 发布：幂等 / 乐观锁 / 旧版本保护 / 回滚 ==')
with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / 'verify.sqlite')
    rid = state['manifest']['run_id']
    store.save(db, state)
    before_balances = {b['member']: b['remaining'] for b in state['archive']['balances']}
    before_run = store.get(db, rid)
    check('发布前 run 无撤回标记', 'withdrawal_published' not in before_run)

    first = store.publish_withdrawal(db, rid)
    check('首发布状态=已发布', first['state'] == '已发布', first['state'])
    check('撤回单编号稳定', first['id'] == f"WD-{plan['plan_digest'][:12]}")
    published_run = store.get(db, rid)
    check('发布后 run 版本+1', published_run['revision'] == before_run['revision'] + 1)
    check('发布后余额已更新', 'withdrawal_published' in published_run)
    post_balances = {b['member']: b['remaining'] for b in published_run['withdrawal_published']['post_balances']}
    grand_before = sum(before_balances.values())
    grand_after = sum(post_balances.values())
    check('发布后总余额=前-撤回', grand_after == grand_before - recon['clawback_total'],
          f'{grand_before}->{grand_after}')

    # 重复发布：同幂等键返回同一单据，不重复扣减
    again = store.publish_withdrawal(db, rid, idempotency_key=first['idempotency_key'])
    check('同幂等键返回同一撤回单', again['id'] == first['id'])
    check('重复执行不重复扣减',
          sum(b['remaining'] for b in store.get(db, rid)['withdrawal_published']['post_balances'])
          == grand_after)
    docs = store.list_withdrawals(db, rid)
    check('撤回单只生成一张', len(docs) == 1, str(len(docs)))

    # 已发布批次再次发布应被拒绝
    blocked = False
    try:
        store.publish_withdrawal(db, rid, idempotency_key='another-key')
    except store.BusinessError as e:
        blocked = e.code == 'ALREADY_PUBLISHED'
    check('重复发布（新键）被拒绝且余额不动', blocked)
    check('拒绝后余额仍为发布后值',
          sum(b['remaining'] for b in store.get(db, rid)['withdrawal_published']['post_balances'])
          == grand_after)

    # 陈旧乐观锁
    stale = False
    try:
        store.publish_withdrawal(db, rid, idempotency_key='k-stale', expected_revision=1)
    except store.BusinessError as e:
        stale = e.code == 'STALE_VERSION'
    check('陈旧 revision 触发 409', stale)

    # 回滚恢复旧版本余额
    rolled = store.rollback_withdrawal(db, first['id'])
    check('回滚状态=已回滚', rolled['state'] == '已回滚')
    restored_run = store.get(db, rid)
    check('回滚后撤回标记移除', 'withdrawal_published' not in restored_run)
    restored_balances = {b['member']: b['remaining'] for b in restored_run['archive']['balances']}
    check('回滚恢复旧余额', restored_balances == before_balances)
    check('回滚后版本继续递增', restored_run['revision'] == before_run['revision'] + 2)
    # 重复回滚幂等
    rolled_again = store.rollback_withdrawal(db, first['id'])
    check('重复回滚幂等', rolled_again['state'] == '已回滚')
    check('重复回滚不改余额',
          {b['member']: b['remaining'] for b in store.get(db, rid)['archive']['balances']}
          == before_balances)

print('\n== 重新计算不产生额外扣减（run 级幂等） ==')
state2 = evaluate({'dataset': 'misissue'}, until=8)
check('同输入方案摘要不变', state2['withdrawal_plan']['plan_digest'] == plan['plan_digest'])
check('重算撤回总额一致', state2['withdrawal_plan']['reconciliation']['clawback_total'] == 240)

print('\n== 无撤回单的批次保护 ==')
dirty = evaluate({'dataset': 'dirty'}, until=8)
check('dirty 数据无误发批次时撤回方案为空', dirty['withdrawal_plan']['rows'] == [])
check('空方案对账仍平衡', dirty['withdrawal_plan']['reconciliation']['grand_balanced'])
no_plan = False
with tempfile.TemporaryDirectory() as tmp:
    db = str(Path(tmp) / 'd.sqlite')
    store.save(db, dirty)
    try:
        store.publish_withdrawal(db, dirty['manifest']['run_id'])
    except store.BusinessError as e:
        no_plan = e.code == 'NO_WITHDRAWAL_PLAN'
check('空方案发布被拒绝', no_plan)

print('\n== 归档包含撤回证据 ==')
from app.archive import serialize
import zipfile, io
z = zipfile.ZipFile(io.BytesIO(serialize(state)))
evidence = json.loads(z.read('evidence.json'))
check('归档包含 withdrawal_plan', 'withdrawal_plan' in evidence)
check('归档保留隔离与视图', 'quarantine' in evidence and 'views' in evidence)

print()
if failures:
    print(f'{len(failures)} 项失败：{failures}')
    sys.exit(1)
print('全部校验通过。')
