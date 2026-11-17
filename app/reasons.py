REASONS = {
    'COLUMN_COUNT':'列数错误', 'MISSING_ID':'编号缺失', 'DUPLICATE_CONFLICT':'重复编号冲突',
    'MISSING_VALUE':'数值缺失', 'BAD_NUMBER':'数值格式错误', 'BAD_TIME':'时间格式错误',
    'BAD_UNIT':'单位不受支持', 'UNKNOWN_REFERENCE':'引用对象不存在', 'INVALID_RANGE':'范围错误',
    'VERSION_CONFLICT':'版本冲突', 'EMPTY_FILE':'空文件', 'BLANK_FILE':'全空行',
    'HEADER_ONLY':'仅有表头', 'STALE_VERSION':'复核版本已更新', 'NOT_FOUND':'批次不存在',
    'NO_MISISSUE_BATCH':'无误发批次', 'NO_WITHDRAWAL_PLAN':'无可发布撤回方案',
    'PUBLISH_BLOCKED':'撤回对账未通过', 'PUBLISH_CONFLICT':'撤回单状态冲突',
    'ALREADY_PUBLISHED':'撤回已发布', 'ROLLBACK_CONFLICT':'回滚状态冲突',
    'BAD_IDEMPOTENCY_KEY':'幂等键无效', 'BAD_REVISION':'版本号无效',
    'FULL_REVOKE_FEASIBLE':'历史成立可整批撤销', 'PARTIAL_HISTORY_DEPENDENT':'历史依赖仅部分撤回',
    'NOTHING_RECOVERABLE':'无尚可追回余额',
}
def reject(state, record, code, detail):
    state['quarantine'].append({'line':record.get('_line',0),'id':record.get('id',''),
       'code':code,'detail':detail,'raw':{k:v for k,v in record.items() if not k.startswith('_')}})
