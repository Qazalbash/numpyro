# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from types import ModuleType
from typing import Callable


def patch_dependency(target: str, root_module: ModuleType) -> Callable:
    parts = target.split(".")
    assert parts[0] == root_module.__name__
    module = root_module
    for part in parts[1:-1]:
        module = getattr(module, part)
    name = parts[-1]
    old_fn = getattr(module, name)
    old_fn = getattr(old_fn, "_pyro_unpatched", old_fn)  # ensure patching is idempotent

    def decorator(new_fn):  # noqa: ANN001, ANN202
        new_fn.__name__ = name
        new_fn._pyro_unpatched = old_fn
        setattr(module, name, new_fn)
        return new_fn

    return decorator
