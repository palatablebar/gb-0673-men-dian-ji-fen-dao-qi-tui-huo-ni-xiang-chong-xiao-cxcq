import json

import pytest

from app.modules.withdrawal.rules import plan_withdrawal, FULL, PARTIAL
from app import store
from app.errors import BusinessError


@pytest.fixture
def mis():
    from app.engine import evaluate
    return evaluate({'dataset': 'misissue'}, until=8)


def test_misissue_context_and_rule_loaded(mis):
    plan = mis['withdrawal_plan']
    assert plan['wrong_rule'] == '月底奖励'
    assert plan['batch'] == 'batch-20261117'
    # 只处理该批：常规规则的发放批次绝不出现在撤回范围
    wrong_lots = {e['lot'] for e in plan['revoke_entries']}
    assert wrong_lots <= {f'W10{i}' for i in range(1, 9)}


def test_full_vs_partial_decisions(mis):
    rows = {r['member']: r for r in mis['withdrawal_plan']['rows']}
    assert {m for m, r in rows.items() if r['decision'] == FULL} == {'00001', '00002', '00007'}
    assert {m for m, r in rows.items() if r['decision'] == PARTIAL} == \
        {'00003', '00004', '00005', '00006', '00008'}


def test_partial_recovers_only_remaining(mis):
    rows = {r['member']: r for r in mis['withdrawal_plan']['rows']}
    # 00005 误发 40 全部被耗用，无可追回余额 -> 撤回 0，不产生负余额
    assert rows['00005']['clawback'] == 0
    assert rows['00005']['after_balance'] == rows['00005']['before_balance']
    # 00003 部分撤回，撤回额等于误发批次尚可追回余额
    assert rows['00003']['clawback'] == rows['00003']['recoverable'] == 10


def test_full_revoke_feasible_when_history_holds(mis):
    rows = {r['member']: r for r in mis['withdrawal_plan']['rows']}
    row = rows['00001']
    assert row['history_checked'] is True
    assert row['affected_spends'] == []
    assert row['clawback'] == row['wrong_issued'] == 90


def test_partial_lists_affected_history(mis):
    rows = {r['member']: r for r in mis['withdrawal_plan']['rows']}
    # 00005 剔除误发后消费 S105 无法成立，必须列出受影响事件
    assert 'S105' in rows['00005']['affected_spends']
    assert rows['00005']['history_checked'] is False


def test_conservation_and_no_negative(mis):
    recon = mis['withdrawal_plan']['reconciliation']
    assert recon['balanced'] and recon['grand_balanced'] and recon['no_negative']
    assert recon['within_issued'] and recon['replay_verified']
    assert recon['wrong_issued_total'] == recon['clawback_total'] + recon['forfeited_total']
    assert recon['pre_grand_total'] - recon['clawback_total'] == recon['post_grand_total']
    assert all(r['after_balance'] >= 0 for r in mis['withdrawal_plan']['rows'])


def test_original_records_untouched(mis):
    ids = {r['id'] for r in mis['records']}
    for original in ('W101', 'W108', 'S103', 'R104'):
        assert original in ids
    # 撤回只追加 REVOKE，不新增/删除 EARN、SPEND、REFUND
    assert all(e['action'] == 'REVOKE' for e in mis['withdrawal_plan']['revoke_entries'])
    assert len(mis['point_ledger']['spends']) == sum(1 for r in mis['records'] if r['type'] == 'SPEND')
    assert len(mis['point_ledger']['refunds']) == sum(1 for r in mis['records'] if r['type'] == 'REFUND')


def test_plan_digest_stable(mis):
    from app.engine import evaluate
    again = evaluate({'dataset': 'misissue'}, until=8)
    assert again['withdrawal_plan']['plan_digest'] == mis['withdrawal_plan']['plan_digest']


def test_empty_plan_for_non_misissue_dataset(sample):
    plan = sample['withdrawal_plan']
    assert plan['rows'] == []
    assert plan['reconciliation']['grand_balanced'] is True


def test_publish_idempotent_and_keeps_old_balance(tmp_path):
    db = str(tmp_path / 'w.sqlite')
    store.save(db, mis)
    rid = mis['manifest']['run_id']
    before = {b['member']: b['remaining'] for b in mis['archive']['balances']}

    first = store.publish_withdrawal(db, rid)
    assert first['state'] == '已发布'
    second = store.publish_withdrawal(db, rid, idempotency_key=first['idempotency_key'])
    assert second['id'] == first['id']
    assert len(store.list_withdrawals(db, rid)) == 1

    run = store.get(db, rid)
    after = {b['member']: b['remaining'] for b in run['withdrawal_published']['post_balances']}
    claw = mis['withdrawal_plan']['reconciliation']['clawback_total']
    assert sum(before.values()) - claw == sum(after.values())
    # 旧 run 结果已备份在撤回单内
    assert json_loads_previous(first, before)


def json_loads_previous(doc, before):
    return bool(doc.get('previous_run'))


def test_republish_with_new_key_blocked(tmp_path):
    db = str(tmp_path / 'w.sqlite')
    store.save(db, mis)
    rid = mis['manifest']['run_id']
    store.publish_withdrawal(db, rid)
    with pytest.raises(BusinessError) as exc:
        store.publish_withdrawal(db, rid, idempotency_key='different')
    assert exc.value.code == 'ALREADY_PUBLISHED'


def test_stale_revision_rejected(tmp_path):
    db = str(tmp_path / 'w.sqlite')
    store.save(db, mis)
    rid = mis['manifest']['run_id']
    with pytest.raises(BusinessError) as exc:
        store.publish_withdrawal(db, rid, expected_revision=999)
    assert exc.value.code == 'STALE_VERSION'
    # 被拒绝后余额保持发布前
    assert 'withdrawal_published' not in store.get(db, rid)


def test_rollback_restores_previous_balance(tmp_path):
    db = str(tmp_path / 'w.sqlite')
    store.save(db, mis)
    rid = mis['manifest']['run_id']
    before = {b['member']: b['remaining'] for b in mis['archive']['balances']}
    doc = store.publish_withdrawal(db, rid)
    rolled = store.rollback_withdrawal(db, doc['id'])
    assert rolled['state'] == '已回滚'
    restored = store.get(db, rid)
    assert 'withdrawal_published' not in restored
    assert {b['member']: b['remaining'] for b in restored['archive']['balances']} == before
    # 重复回滚幂等，不再改动
    again = store.rollback_withdrawal(db, doc['id'])
    assert again['state'] == '已回滚'


def test_publish_empty_plan_blocked(tmp_path, sample):
    db = str(tmp_path / 'e.sqlite')
    store.save(db, sample)
    with pytest.raises(BusinessError) as exc:
        store.publish_withdrawal(db, sample['manifest']['run_id'])
    assert exc.value.code == 'NO_WITHDRAWAL_PLAN'


def test_failed_publish_marks_doc_and_keeps_balance(tmp_path, monkeypatch):
    db = str(tmp_path / 'w.sqlite')
    store.save(db, mis)
    rid = mis['manifest']['run_id']
    before_revision = store.get(db, rid)['revision']

    real_dumps = json.dumps
    call = {'n': 0}
    class Boom(Exception):
        pass

    def flaky_dumps(obj, *a, **k):
        # 仅在撤回单已落库、核心事务写 run 余额的那次序列化时中断。
        if isinstance(obj, dict) and obj.get('withdrawal_published'):
            call['n'] += 1
            raise Boom('publish interrupted')
        return real_dumps(obj, *a, **k)
    monkeypatch.setattr(json, 'dumps', flaky_dumps)
    with pytest.raises(Boom):
        store.publish_withdrawal(db, rid)
    monkeypatch.undo()

    assert call['n'] >= 1
    run = store.get(db, rid)
    assert 'withdrawal_published' not in run
    assert run['revision'] == before_revision
    docs = store.list_withdrawals(db, rid)
    assert docs and docs[0]['state'] == '发布失败'


def test_withdrawal_archive_contains_evidence(mis):
    from app.archive import serialize
    import zipfile, io, json
    z = zipfile.ZipFile(io.BytesIO(serialize(mis)))
    evidence = json.loads(z.read('evidence.json'))
    assert evidence['withdrawal_plan']['plan_digest'] == mis['withdrawal_plan']['plan_digest']
