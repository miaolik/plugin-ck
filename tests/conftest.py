"""共享 fixtures：数据目录隔离 + 以插件包形式导入 main / ck_web。

main.py / ck_web.py 依赖 ElainaBot_v2 框架（core.plugin.*）。测试时按以下顺序
查找框架仓库并把它加入 sys.path：环境变量 ELAINABOT_V2_PATH → 同级目录
../ElainaBot_v2。找不到时跳过依赖框架的测试（ck_engine 测试不受影响）。
"""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ck_engine  # noqa: E402


def _find_framework():
    candidates = []
    env = os.environ.get("ELAINABOT_V2_PATH")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT.parent / "ElainaBot_v2")
    for c in candidates:
        if (c / "core" / "plugin" / "decorators.py").exists():
            return c
    return None


FRAMEWORK_DIR = _find_framework()
_plugin_pkg_error = None
if FRAMEWORK_DIR:
    if str(FRAMEWORK_DIR) not in sys.path:
        sys.path.insert(0, str(FRAMEWORK_DIR))
    # 以包形式加载插件，使 main.py / ck_web.py 的相对导入生效；
    # ckplugin.ck_engine 复用顶层 ck_engine，保证测试内是同一个模块实例。
    pkg = types.ModuleType("ckplugin")
    pkg.__path__ = [str(REPO_ROOT)]
    pkg.__package__ = "ckplugin"
    sys.modules.setdefault("ckplugin", pkg)
    sys.modules.setdefault("ckplugin.ck_engine", ck_engine)
    try:
        importlib.import_module("ckplugin.ck_web")
        importlib.import_module("ckplugin.main")
    except Exception as exc:  # pragma: no cover - 框架不兼容时跳过
        _plugin_pkg_error = str(exc)


def _plugin_module(name):
    if FRAMEWORK_DIR is None:
        pytest.skip("未找到 ElainaBot_v2 框架（设置 ELAINABOT_V2_PATH 或克隆到同级目录）")
    if _plugin_pkg_error:
        pytest.skip(f"插件包导入失败: {_plugin_pkg_error}")
    return sys.modules[f"ckplugin.{name}"]


@pytest.fixture
def main_mod():
    return _plugin_module("main")


@pytest.fixture
def ckweb_mod():
    return _plugin_module("ck_web")


@pytest.fixture
def cron_mod():
    return _plugin_module("ck_cron")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 ck_engine 内所有数据路径常量指向临时目录。

    store_*/settings_*/globals_*/rank_* 在调用时都是读模块全局，
    因此这里 monkeypatch 模块属性即可隔离文件读写。
    """
    d = tmp_path / "data"
    db = d / "db"
    d.mkdir(parents=True, exist_ok=True)
    db.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ck_engine, "DATA_DIR", d)
    monkeypatch.setattr(ck_engine, "DB_DIR", db)
    monkeypatch.setattr(ck_engine, "GLOBAL_FILE", d / "全局变量.json")
    monkeypatch.setattr(ck_engine, "SETTINGS_FILE", d / "设置.json")
    return d


@pytest.fixture
def web_dirs(tmp_path, monkeypatch, data_dir, ckweb_mod):
    """ck_web 在导入时绑定了路径常量，需要一并指向临时目录。"""
    dicts = tmp_path / "dicts"
    dicts.mkdir(exist_ok=True)
    monkeypatch.setattr(ckweb_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(ckweb_mod, "DICT_DIR", dicts)
    monkeypatch.setattr(ck_engine, "DICT_DIR", dicts)
    return {"data": data_dir, "dicts": dicts}


@pytest.fixture
def engine():
    """全新的引擎实例（不加载磁盘词库）。"""
    return ck_engine.CKEngine()
