"""按符号名定位 torch/_inductor 源码位置，省掉手工翻目录。

用法：
    .\code\tools\run.ps1 code\tools\find_source.py torch._inductor.scheduler Scheduler.fuse_nodes
    .\code\tools\run.ps1 code\tools\find_source.py torch._inductor.codegen.cpp CppKernel
"""
from __future__ import annotations

import importlib
import inspect
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    module_name, attr = sys.argv[1], sys.argv[2]
    mod = importlib.import_module(module_name)
    obj = mod
    for part in attr.split("."):
        obj = getattr(obj, part)
    try:
        path = inspect.getsourcefile(obj) or inspect.getfile(obj)
        line = inspect.getsourcelines(obj)[1]
    except (TypeError, OSError):
        print(f"{module_name}.{attr} 没有可直接定位的源码（可能是内置对象）。")
        raise SystemExit(1)
    print(f"{module_name}.{attr}")
    print(f"  file: {path}")
    print(f"  line: {line}")
    print()
    print(inspect.getsource(obj)[:2000])


if __name__ == "__main__":
    main()