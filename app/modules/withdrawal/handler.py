from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from app.errors import BusinessError
from app.engine import evaluate
from app.web import render
from app import store

router = APIRouter()


@router.get('/withdrawal')
def page(request: Request):
    from app.main import DB_PATH
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'misissue')}))
    run_id = state['manifest']['run_id']
    docs = store.list_withdrawals(DB_PATH, run_id)
    published_state = {}
    for doc in docs:
        for row in doc['rows']:
            published_state[row['member']] = doc['state']
    plan = state.get('withdrawal_plan')
    if plan:
        for row in plan['rows']:
            row['publish_state'] = published_state.get(row['member'], '待发布')
        state['views']['withdrawal']['rows'] = plan['rows']
        state['withdrawal_docs'] = docs
    return render(request, 'withdrawal')


@router.post('/api/withdrawal')
def calculate(payload: dict = Body(default={})):
    # 误发撤回需要先完成前七个模块，再执行 M8。
    payload = dict(payload)
    payload.setdefault('dataset', 'misissue')
    return evaluate(payload, until=8)


@router.post('/api/runs/{run_id}/withdrawals/publish')
def publish(run_id: str, payload: dict = Body(default={})):
    from app.main import DB_PATH
    key = payload.get('idempotency_key')
    revision = payload.get('revision')
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise BusinessError('BAD_IDEMPOTENCY_KEY', '幂等键须为非空字符串')
    if revision is not None and (not isinstance(revision, int) or revision < 1):
        raise BusinessError('BAD_REVISION', '版本号须为正整数')
    return JSONResponse(store.publish_withdrawal(
        DB_PATH, run_id, idempotency_key=key, expected_revision=revision))


@router.get('/api/runs/{run_id}/withdrawals')
def withdrawals(run_id: str):
    from app.main import DB_PATH
    return store.list_withdrawals(DB_PATH, run_id)


@router.get('/api/withdrawals/{withdrawal_id}')
def withdrawal_detail(withdrawal_id: str):
    from app.main import DB_PATH
    return store.get_withdrawal(DB_PATH, withdrawal_id)


@router.post('/api/withdrawals/{withdrawal_id}/rollback')
def rollback(withdrawal_id: str):
    from app.main import DB_PATH
    return store.rollback_withdrawal(DB_PATH, withdrawal_id)
