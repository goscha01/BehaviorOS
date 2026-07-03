"""Evidence analyzers.

Importing this package triggers registration of all built-in analyzers.
Adding a new analyzer means writing one class + importing it here.
"""

from apps.learning.analyzers.base import (  # noqa: F401
    AnalysisResult,
    BaseEvidenceAnalyzer,
    get_analyzer_for,
    iter_registered,
    register,
)

# Side-effect imports so analyzers self-register via @register.
from apps.learning.analyzers import call  # noqa: F401
from apps.learning.analyzers import conversation  # noqa: F401
from apps.learning.analyzers import outcome  # noqa: F401
