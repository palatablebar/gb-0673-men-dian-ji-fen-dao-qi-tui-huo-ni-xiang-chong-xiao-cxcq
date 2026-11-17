"""纯标准库测试垫片：在没有 pytest/fastapi 的环境中运行 tests/test_withdrawal.py。

仅用于本地离线自检；CI/Docker 内仍以真实 pytest + FastAPI 为准。
提供 pytest.fixture、pytest.raises 与最小 tmp_path/monkeypatch。
"""
import sys
import types
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- fastapi 打桩（规则级测试不触网，但导入链需要） ----
for name in ('fastapi', 'fastapi.requests', 'fastapi.responses',
             'fastapi.templating', 'fastapi.staticfiles'):
    sys.modules.setdefault(name, types.ModuleType(name))
fa = sys.modules['fastapi']


class _Router:
    def __init__(self, *a, **k):
        pass

    def get(self, *a, **k):
        return lambda fn: fn

    post = get


fa.APIRouter = _Router
fa.Request = object
fa.Body = lambda *a, **k: None
sys.modules['fastapi.responses'].JSONResponse = object
sys.modules['fastapi.responses'].Response = object
sys.modules['fastapi.responses'].RedirectResponse = object


class _Jinja:
    def __init__(self, *a, **k):
        pass

    def TemplateResponse(self, *a, **k):
        return None


sys.modules['fastapi.templating'].Jinja2Templates = _Jinja


class _Static:
    def __init__(self, *a, **k):
        pass


sys.modules['fastapi.staticfiles'].StaticFiles = _Static

# ---- pytest 垫片 ----
pytest = types.ModuleType('pytest')


class _Raises:
    def __init__(self, exc):
        self.exc = exc
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, etype, evalue, tb):
        if etype is None:
            raise AssertionError(f'DID_NOT_RAISE {getattr(self.exc, "__name__", self.exc)}')
        if not issubclass(etype, self.exc):
            return False
        self.value = evalue
        return True


def raises(exc):
    return _Raises(exc)


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def fixture(fn):
    fn.__is_fixture__ = True
    return fn


pytest.fixture = fixture
pytest.raises = raises
sys.modules['pytest'] = pytest


class _TmpPath:
    def __init__(self, p):
        self.p = p

    def __truediv__(self, other):
        return _TmpPath(self.p / other)

    def __str__(self):
        return str(self.p)


def _make_tmp(path):
    path.mkdir(parents=True, exist_ok=True)
    return _TmpPath(path)


def evaluate_default(payload=None, until=8):
    from app.engine import evaluate
    return evaluate(payload or {}, until=until)


def run_module(mod_name):
    import importlib
    mod = importlib.import_module(mod_name)
    fixtures = {n: v for n, v in vars(mod).items()
                if callable(v) and getattr(v, '__is_fixture__', False)}
    tests = [(n, v) for n, v in vars(mod).items()
             if n.startswith('test_') and callable(v)]
    tmp_dir = tempfile.mkdtemp()
    passed = failed = 0
    # pytest 会把 fixture 注入到测试可解析的命名空间；这里预解析全部 fixture 值，
    # 让函数体内对模块级 fixture 名的直接引用也能拿到值。
    fixture_values = {}
    for fname, ffn in fixtures.items():
        fparams = __import__('inspect').signature(ffn).parameters
        fkwargs = {}
        for fp in fparams:
            if fp == 'tmp_path':
                fkwargs[fp] = _make_tmp(Path(tmp_dir) / f'fixture-{fname}')
            elif fp == 'monkeypatch':
                fkwargs[fp] = _MonkeyPatch()
            elif fp == 'sample':
                fkwargs[fp] = evaluate_default()
            elif fp == 'clean':
                fkwargs[fp] = evaluate_default({'dataset': 'clean'})
            elif fp == 'mis':
                fkwargs[fp] = evaluate_default({'dataset': 'misissue'}, until=8)
        fixture_values[fname] = ffn(**fkwargs)
    for name, fn in tests:
        kwargs = {}
        import inspect
        params = inspect.signature(fn).parameters
        builtins = {
            'tmp_path': lambda: _make_tmp(Path(tmp_dir) / name.replace('test_', '')),
            'monkeypatch': lambda: _MonkeyPatch(),
            'sample': lambda: evaluate_default(),
            'clean': lambda: evaluate_default({'dataset': 'clean'}),
            'mis': lambda: evaluate_default({'dataset': 'misissue'}, until=8),
        }
        for pname in params:
            if pname in builtins:
                kwargs[pname] = builtins[pname]()
            elif pname in fixture_values:
                kwargs[pname] = fixture_values[pname]
            elif pname in fixtures:
                kwargs[pname] = fixtures[pname]()
        try:
            # 模拟 pytest：fixture 名在测试函数的全局命名空间里以注入值可见，
            # 覆盖模块级同名 fixture 函数（含函数体内直接引用的全局 fixture）。
            saved_globals = {}
            for pname, value in {**fixture_values, **kwargs}.items():
                if pname in mod.__dict__:
                    saved_globals[pname] = mod.__dict__[pname]
                    mod.__dict__[pname] = value
            fn(**kwargs)
            print(f'[PASS] {mod_name}::{name}')
            passed += 1
        except Exception:
            print(f'[FAIL] {mod_name}::{name}')
            traceback.print_exc()
            failed += 1
        finally:
            for gname, gval in saved_globals.items():
                mod.__dict__[gname] = gval
            mp = kwargs.get('monkeypatch')
            if mp:
                mp.undo()
    return passed, failed


if __name__ == '__main__':
    target = sys.argv[1:] or ['tests.test_withdrawal']
    total_p = total_f = 0
    for mod in target:
        p, f = run_module(mod)
        total_p += p
        total_f += f
    print(f'\n{total_p} passed, {total_f} failed')
    sys.exit(1 if total_f else 0)
