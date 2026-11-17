"""误发批次撤回规则（M8）。

裁决原则：在不修改任何原始 EARN/SPEND/REFUND 记录的前提下，先做反事实重放——
剔除误发规则的发放批次后，若该户全部历史消费与退货仍能成立，则整批撤销；
否则只撤回误发批次当前尚可追回的余额（部分撤回），已耗用或到期部分保留账面、
不向会员追偿。撤回只追加 REVOKE 流水，原始流水与批次余额保持不可变。
"""
from hashlib import sha256
import json

from app.config import DEFAULT_WITHDRAWAL_RULE
from app.coerce import instant
from app.modules.consumption.rules import replay

FULL = '整批撤销'
PARTIAL = '部分撤回'
CODE_FULL = 'FULL_REVOKE_FEASIBLE'
CODE_PARTIAL = 'PARTIAL_HISTORY_DEPENDENT'
CODE_EMPTY = 'NOTHING_RECOVERABLE'


def _member_balances(lots):
    balances = {}
    for lot in lots:
        balances[lot['member']] = balances.get(lot['member'], 0) + lot['remaining']
    return balances


def plan_withdrawal(state):
    context = state['context']
    wrong_rule = context.get('withdrawal_rule', DEFAULT_WITHDRAWAL_RULE)
    batch = context.get('withdrawal_batch', 'batch-20261117')
    as_of = context['as_of']

    wrong_earns = [r for r in state['records']
                   if r['type'] == 'EARN' and r['rule'] == wrong_rule]
    if not wrong_earns:
        # 非误发数据集（现场/清洁/历史编码）没有该规则发放，撤回模块返回空方案，不阻断其他模块。
        empty = {
            'batch': batch, 'wrong_rule': wrong_rule, 'as_of': instant(as_of).isoformat(),
            'rows': [], 'revoke_entries': [], 'post_lots': state['point_ledger']['lots'],
            'post_balances': state['archive']['balances'],
            'reconciliation': {
                'batch': batch, 'wrong_rule': wrong_rule, 'members': 0, 'full_revoke': 0,
                'partial_revoke': 0, 'wrong_issued_total': 0, 'recoverable_total': 0,
                'clawback_total': 0, 'lost_total': 0, 'forfeited_total': 0,
                'before_total': 0, 'after_total': 0, 'untouched_total': 0,
                'balanced': True, 'no_negative': True, 'within_issued': True,
                'replay_verified': True, 'grand_balanced': True,
                'pre_grand_total': state['balance']['remaining'],
                'post_grand_total': state['balance']['remaining'],
            },
            'plan_digest': sha256(
                json.dumps({'batch': batch, 'wrong_rule': wrong_rule, 'empty': True},
                           sort_keys=True).encode()).hexdigest(),
        }
        return empty
    wrong_ids = {r['id'] for r in wrong_earns}
    members = sorted({r['member'] for r in wrong_earns})

    ledger = state['point_ledger']
    live_lots = ledger['lots']
    before_balances = _member_balances(live_lots)

    # 反事实重放：剔除误发发放后，历史消费/退货是否仍然全部成立。
    counter = replay(
        [r for r in state['records'] if r['id'] not in wrong_ids],
        [l for l in state['lots'] if l['id'] not in wrong_ids],
        as_of,
    )
    counter_spends = {}
    counter_refunds = {}
    for spend in counter['spends']:
        counter_spends.setdefault(spend['member'], set()).add(spend['id'])
    for refund in counter['refunds']:
        counter_refunds.setdefault(refund['member'], set()).add(refund['id'])
    counter_errors = {}
    for row, _code, _detail in counter['errors']:
        counter_errors.setdefault(row['member'], set()).add(row['id'])

    rows = []
    revoke_entries = []
    decisions = {}
    for member in members:
        member_wrong_lots = [l for l in live_lots if l['id'] in wrong_ids and l['member'] == member]
        member_wrong_lots.sort(key=lambda l: (l['expires'], l['at'], l['id']))
        wrong_issued = sum(l['issued'] for l in member_wrong_lots)
        recoverable = sum(l['remaining'] for l in member_wrong_lots)
        # 账面已耗用/到期：现金口径上无法从余额追回，但整撤时会随误发批一并核销。
        consumed = wrong_issued - recoverable
        before = before_balances.get(member, 0)

        member_spends = {r['id'] for r in state['records']
                         if r['member'] == member and r['type'] == 'SPEND'}
        member_refunds = {r['id'] for r in state['records']
                          if r['member'] == member and r['type'] == 'REFUND'}
        ok_spends = counter_spends.get(member, set())
        ok_refunds = counter_refunds.get(member, set())
        broken = sorted((member_spends - ok_spends) | (member_refunds - ok_refunds)
                        | counter_errors.get(member, set()))
        feasible = not broken and member_spends <= ok_spends and member_refunds <= ok_refunds

        if feasible:
            decision, reason_code = FULL, CODE_FULL
            clawback = wrong_issued
        else:
            decision = PARTIAL
            reason_code = CODE_EMPTY if recoverable == 0 else CODE_PARTIAL
            clawback = recoverable
        decisions[member] = decision
        after = before - clawback
        if after < 0:
            # 规则兜底：撤回绝不使会员余额为负。
            clawback = before
            after = 0
            reason_code = CODE_EMPTY
            decision = PARTIAL
        # 核销（forfeited）：不在现金撤回额内、但账面注销的误发积分。
        forfeited = wrong_issued - clawback
        # 不可追（lost）：现金口径真正无法收回的误发积分，仅部分撤回户存在。
        lost = consumed if decision == PARTIAL else 0

        revoke_lot_rows = []
        for lot in member_wrong_lots:
            lot_revoke = lot['issued'] if decision == FULL else lot['remaining']
            revoke_lot_rows.append({'lot': lot['id'], 'issued': lot['issued'],
                                    'remaining': lot['remaining'], 'revoke': lot_revoke})
            if lot_revoke <= 0:
                continue
            revoke_entries.append({
                'id': f'RV-{lot["id"]}', 'member': member, 'action': 'REVOKE',
                'points': lot_revoke, 'lot': lot['id'], 'ref': lot['id'],
                'at': instant(as_of).isoformat(), 'decision': decision,
                'recoverable_points': min(lot_revoke, lot['remaining']),
                'forfeited_points': max(lot_revoke - lot['remaining'], 0),
                'batch': batch, 'reason_code': reason_code,
            })

        rows.append({
            'member': member,
            'wrong_issued': wrong_issued,
            'recoverable': recoverable,
            'consumed': consumed,
            'forfeited': forfeited,
            'lost': lost,
            'decision': decision,
            'reason_code': reason_code,
            'clawback': clawback,
            'before_balance': before,
            'after_balance': after,
            'revoke_lots': revoke_lot_rows,
            'affected_spends': broken,
            'history_checked': feasible,
            'publish_state': '待发布',
        })

    full_members = {m for m, d in decisions.items() if d == FULL}
    post_lots = []
    for lot in live_lots:
        if lot['id'] in wrong_ids:
            if decisions[lot['member']] == PARTIAL:
                post_lots.append({**lot, 'remaining': 0})
            continue
        if lot['member'] in full_members:
            counter_lot = next(cl for cl in counter['lots'] if cl['id'] == lot['id'])
            post_lots.append(dict(counter_lot))
        else:
            post_lots.append(dict(lot))
    post_balances = [{'member': m, 'remaining': n}
                     for m, n in sorted(_member_balances(post_lots).items())]

    before_total = sum(before_balances.get(m, 0) for m in members)
    clawback_total = sum(r['clawback'] for r in rows)
    after_total = sum(r['after_balance'] for r in rows)
    untouched_total = sum(n for m, n in before_balances.items() if m not in members)
    reconciliation = {
        'batch': batch,
        'wrong_rule': wrong_rule,
        'members': len(members),
        'full_revoke': sum(1 for r in rows if r['decision'] == FULL),
        'partial_revoke': sum(1 for r in rows if r['decision'] == PARTIAL),
        'wrong_issued_total': sum(r['wrong_issued'] for r in rows),
        'recoverable_total': sum(r['recoverable'] for r in rows),
        'clawback_total': clawback_total,
        'lost_total': sum(r['lost'] for r in rows),
        'forfeited_total': sum(r['forfeited'] for r in rows),
        'before_total': before_total,
        'after_total': after_total,
        'untouched_total': untouched_total,
        'balanced': before_total - clawback_total == after_total,
        'no_negative': all(r['after_balance'] >= 0 for r in rows),
        'within_issued': all(r['clawback'] <= r['wrong_issued'] for r in rows),
        'replay_verified': all(r['history_checked'] or r['decision'] == PARTIAL for r in rows),
        'post_grand_total': after_total + untouched_total,
        'pre_grand_total': sum(before_balances.values()),
    }
    reconciliation['grand_balanced'] = (
        reconciliation['pre_grand_total'] - clawback_total
        == reconciliation['post_grand_total']
    )

    digest_source = json.dumps(
        {'batch': batch, 'wrong_rule': wrong_rule,
         'rows': [{k: r[k] for k in ('member', 'wrong_issued', 'recoverable',
                                     'decision', 'clawback', 'before_balance',
                                     'after_balance', 'reason_code')} for r in rows]},
        ensure_ascii=False, sort_keys=True,
    )
    return {
        'batch': batch,
        'wrong_rule': wrong_rule,
        'as_of': instant(as_of).isoformat(),
        'rows': rows,
        'revoke_entries': revoke_entries,
        'post_lots': post_lots,
        'post_balances': post_balances,
        'reconciliation': reconciliation,
        'plan_digest': sha256(digest_source.encode()).hexdigest(),
    }


def apply(state):
    plan = plan_withdrawal(state)
    state['withdrawal_plan'] = plan
    state['views']['withdrawal'] = {
        'title': '误发批次撤回',
        'rows': plan['rows'],
        'metrics': {
            '撤回户数': plan['reconciliation']['members'],
            '整批撤销': plan['reconciliation']['full_revoke'],
            '部分撤回': plan['reconciliation']['partial_revoke'],
            '误发积分': plan['reconciliation']['wrong_issued_total'],
            '撤回积分': plan['reconciliation']['clawback_total'],
            '账面核销': plan['reconciliation']['forfeited_total'],
            '不可追积分': plan['reconciliation']['lost_total'],
        },
    }
