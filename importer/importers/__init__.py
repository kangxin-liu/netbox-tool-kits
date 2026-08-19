# Classification: INTERNAL
"""One importer per sheet, all sharing the pipeline in `base.py`."""

from importer.importers.base import BaseImporter
from importer.importers.cabling import CablingImporter
from importer.importers.devices import DeviceImporter
from importer.importers.hierarchy import HierarchyImporter

# Importers are stateless — every run's state lives in the plan and the lookup cache passed in — so
# one instance each is enough and the views never have to decide which to build.
IMPORTERS: dict[str, BaseImporter] = {
    "hierarchy": HierarchyImporter(),
    "devices": DeviceImporter(),
    "cabling": CablingImporter(),
}


def importer_for(name: str) -> BaseImporter:
    return IMPORTERS[name]


__all__ = [
    "IMPORTERS",
    "BaseImporter",
    "CablingImporter",
    "DeviceImporter",
    "HierarchyImporter",
    "importer_for",
]
