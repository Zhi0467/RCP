"""Stdlib host bootstrap; shipped verbatim with its source dependencies."""

from __future__ import annotations

import json
import sys
import types


def main() -> None:
    payload = json.load(sys.stdin)
    for name in ("rcp", "rcp.agents", "rcp.browser", "rcp.transport"):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
    for name, source in payload["sources"].items():
        module = types.ModuleType(name)
        module.__file__ = name.replace(".", "/") + ".py"
        sys.modules[name] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    result = sys.modules["rcp.browser.host"].dispatch(payload["request"])
    print(json.dumps(result))


if __name__ == "__main__":
    main()
