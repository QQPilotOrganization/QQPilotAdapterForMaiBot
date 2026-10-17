"""让测试可以从插件父目录导入 ``QQPilotAdapter`` 包。"""

from pathlib import Path
from typing import Iterator

import shutil
import sys
import uuid

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = PROJECT_ROOT.parent
if str(PARENT_ROOT) not in sys.path:
    sys.path.insert(0, str(PARENT_ROOT))


@pytest.fixture()
def plugin_root() -> Path:
    """返回插件目录。"""

    return PROJECT_ROOT


@pytest.fixture()
def tmp_path() -> Iterator[Path]:
    """在插件工作区内提供临时目录，避免依赖受限的系统临时目录。"""

    path = PROJECT_ROOT / "temp" / "pytest" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
