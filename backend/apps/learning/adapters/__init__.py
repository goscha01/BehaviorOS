"""Evidence source adapters.

Importing this package triggers registration of all built-in adapters. Add
new adapters as siblings and re-export them here so they self-register at
Django app startup.
"""

from apps.learning.adapters.base import (  # noqa: F401
    EvidenceSourceAdapter,
    get_adapter,
    iter_registered,
    register,
)
from apps.learning.adapters.dto import Evidence  # noqa: F401

# Side-effect imports so adapters self-register via @register.
from apps.learning.adapters import callio  # noqa: F401
from apps.learning.adapters import leadbridge  # noqa: F401
from apps.learning.adapters import serviceflow  # noqa: F401
