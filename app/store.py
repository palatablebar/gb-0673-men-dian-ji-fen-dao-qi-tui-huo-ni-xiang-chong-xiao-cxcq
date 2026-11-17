import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute(
        "CREATE TABLE IF NOT EXISTS withdrawals ("
        "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, "
        "revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL, result TEXT NOT NULL, "
        "previous_run_result TEXT, created_at TEXT NOT NULL, published_at TEXT)"
    )
    return con

def save(path, result):
    run_id = result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)',
                    (run_id, result['manifest']['sha256'], json.dumps(result, ensure_ascii=False)))
    return get(path, run_id)

def get(path, run_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '批次不存在', 404)
    result = json.loads(row['result'])
    result.update(revision=row['revision'], status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path, run_id, revision):
    with connect(path) as con:
        cur = con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',
                          ('已复核', run_id, revision))
        if cur.rowcount != 1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?', (run_id,)).fetchone():
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            raise BusinessError('STALE_VERSION', '复核版本已更新，请刷新', 409)
    return get(path, run_id)

def _now():
    return datetime.now(timezone.utc).isoformat()

def _row_to_withdrawal(row):
    data = json.loads(row['result'])
    data.update(id=row['id'], run_id=row['run_id'], revision=row['revision'],
                state=row['state'], idempotency_key=row['idempotency_key'],
                created_at=row['created_at'], published_at=row['published_at'])
    if row['previous_run_result']:
        previous = json.loads(row['previous_run_result'])
        data['previous_run'] = {
            'run_id': previous['manifest']['run_id'],
            'revision': previous.get('revision', 1),
            'sha256': previous['manifest']['sha256'],
            'balances': previous.get('archive', {}).get('balances', []),
        }
    return data

def list_withdrawals(path, run_id=None):
    with connect(path) as con:
        if run_id:
            rows = con.execute('SELECT * FROM withdrawals WHERE run_id=? ORDER BY rowid DESC', (run_id,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM withdrawals ORDER BY rowid DESC LIMIT 100').fetchall()
    return [_row_to_withdrawal(r) for r in rows]

def get_withdrawal(path, withdrawal_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '撤回单不存在', 404)
    return _row_to_withdrawal(row)

def publish_withdrawal(path, run_id, idempotency_key=None, expected_revision=None):
    """事务化发布撤回单。

    幂等：同一幂等键重复调用直接返回已发布单据，不重复扣减。
    乐观锁：expected_revision 与 run 当前版本不一致返回 409。
    旧版本保护：发布前完整备份 run 结果；任何一步失败整体回滚，旧余额原样保留。
    """
    run = get(path, run_id)
    plan = run.get('withdrawal_plan')
    if not plan or not plan.get('rows'):
        raise BusinessError('NO_WITHDRAWAL_PLAN', '该批次没有可发布的撤回方案（无该误发规则的发放记录）')
    recon = plan['reconciliation']
    if not (recon['balanced'] and recon['no_negative'] and recon['grand_balanced']
            and recon['within_issued'] and recon['replay_verified']):
        raise BusinessError('PUBLISH_BLOCKED', '撤回对账未通过，拒绝发布，余额未改动')

    key = idempotency_key or f"plan-{run_id}-{plan['plan_digest'][:16]}"
    with connect(path) as con:
        existing = con.execute('SELECT * FROM withdrawals WHERE idempotency_key=?', (key,)).fetchone()
        if existing:
            return _row_to_withdrawal(existing)
        current = con.execute('SELECT revision,result FROM runs WHERE id=?', (run_id,)).fetchone()
        if not current:
            raise BusinessError('NOT_FOUND', '批次不存在', 404)
        if expected_revision is not None and current['revision'] != expected_revision:
            raise BusinessError('STALE_VERSION', '批次版本已更新，请刷新后重试', 409)
        if json.loads(current['result']).get('withdrawal_published'):
            raise BusinessError('ALREADY_PUBLISHED', '该批次已发布撤回，请使用新批次或回滚后重试', 409)

        withdrawal_id = f"WD-{plan['plan_digest'][:12]}"
        created_at = _now()
        document = {
            'batch': plan['batch'], 'wrong_rule': plan['wrong_rule'], 'as_of': plan['as_of'],
            'plan_digest': plan['plan_digest'], 'rows': plan['rows'],
            'revoke_entries': plan['revoke_entries'],
            'post_balances': plan['post_balances'], 'reconciliation': recon,
            'source_run': {'run_id': run_id, 'sha256': run['manifest']['sha256'],
                           'revision': current['revision']},
        }
        # 先落撤回单（待发布），旧 run 结果同时备份在同一行。
        con.execute(
            'INSERT INTO withdrawals(id,run_id,idempotency_key,revision,state,result,previous_run_result,created_at,published_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (withdrawal_id, run_id, key, 1, '待发布',
             json.dumps(document, ensure_ascii=False), current['result'], created_at, None),
        )

    # 核心发布在独立事务内完成：余额写入与状态翻转同时成功或同时回滚。
    try:
        with connect(path) as con:
            row = con.execute('SELECT state FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
            if not row or row['state'] != '待发布':
                raise BusinessError('PUBLISH_CONFLICT', '撤回单状态不允许发布', 409)
            latest = con.execute('SELECT revision,result FROM runs WHERE id=?', (run_id,)).fetchone()
            if not latest:
                raise BusinessError('NOT_FOUND', '批次不存在', 404)
            if expected_revision is not None and latest['revision'] != expected_revision:
                raise BusinessError('STALE_VERSION', '批次版本已更新，请刷新后重试', 409)
            updated = json.loads(latest['result'])
            updated['withdrawal_published'] = {
                'withdrawal_id': withdrawal_id, 'idempotency_key': key,
                'published_at': created_at, 'clawback_total': recon['clawback_total'],
                'post_balances': plan['post_balances'], 'plan_digest': plan['plan_digest'],
            }
            cur = con.execute('UPDATE runs SET result=?, revision=revision+1 WHERE id=? AND revision=?',
                              (json.dumps(updated, ensure_ascii=False), run_id, latest['revision']))
            if cur.rowcount != 1:
                raise BusinessError('STALE_VERSION', '批次版本已更新，请刷新后重试', 409)
            con.execute("UPDATE withdrawals SET state='已发布', revision=2, published_at=? WHERE id=?",
                        (created_at, withdrawal_id))
    except Exception:
        with connect(path) as con:
            con.execute("UPDATE withdrawals SET state='发布失败' WHERE id=? AND state='待发布'",
                        (withdrawal_id,))
        raise
    return get_withdrawal(path, withdrawal_id)

def rollback_withdrawal(path, withdrawal_id):
    """用发布前备份恢复 run 余额；撤回单标记已回滚。重复回滚不产生额外效果。"""
    with connect(path) as con:
        row = con.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '撤回单不存在', 404)
        if row['state'] == '已回滚':
            return _row_to_withdrawal(row)
        if row['state'] != '已发布':
            raise BusinessError('ROLLBACK_CONFLICT', '只有已发布撤回单可以回滚', 409)
        run_row = con.execute('SELECT result FROM runs WHERE id=?', (row['run_id'],)).fetchone()
        if not run_row:
            raise BusinessError('NOT_FOUND', '原批次已不存在，无法回滚', 404)
        restored = json.loads(row['previous_run_result'])
        restored.pop('withdrawal_published', None)
        con.execute('UPDATE runs SET result=?, revision=revision+1 WHERE id=?',
                    (json.dumps(restored, ensure_ascii=False), row['run_id']))
        con.execute("UPDATE withdrawals SET state='已回滚' WHERE id=?", (withdrawal_id,))
        row = con.execute('SELECT * FROM withdrawals WHERE id=?', (withdrawal_id,)).fetchone()
    return _row_to_withdrawal(row)
