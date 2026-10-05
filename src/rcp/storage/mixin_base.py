"""The base every `AppStore` topic mixin declares, for the type checker only.

Each mixin reaches the shared connection, clock, and row mappers through `self`,
which only the assembled `AppStore` provides. `base.py` imports the mixin modules
for their migrations, so a runtime base here would be an import cycle. At runtime
the base is `object`, which leaves `AppStore`'s method resolution order unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rcp.storage.base import AppStoreBase
    from rcp.storage.rows import RowMappingMixin

    class StoreMixinBase(RowMappingMixin, AppStoreBase):
        pass

else:
    StoreMixinBase = object
