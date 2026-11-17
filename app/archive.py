import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN', '批次尚未完成')
    evidence = {'counts': result['counts'], 'views': result['views'],
                'quarantine': result['quarantine']}
    # 误发撤回证据随归档包留存：方案、REVOKE 流水与发布信息，原始记录仍在 views 中可追查。
    if result.get('withdrawal_plan'):
        evidence['withdrawal_plan'] = result['withdrawal_plan']
    if result.get('withdrawal_published'):
        evidence['withdrawal_published'] = result['withdrawal_published']
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(
            {**result['manifest'], 'revision': result.get('revision', 1),
             'status': result.get('status', '待复核')}, ensure_ascii=False, indent=2))
        archive.writestr('evidence.json', json.dumps(evidence, ensure_ascii=False, indent=2))
    return buffer.getvalue()
