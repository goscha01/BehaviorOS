"""Concrete Context Sources.

Import order here is the registration order — a stable tiebreaker for
sources sharing a priority. The Merger sorts by priority ASCENDING and
respects registration order among equals.

Adding a new source:
1. Drop `apps/context/sources/<name>.py` with a `@register`ed class.
2. Add `from apps.context.sources import <name>  # noqa` below.
"""

from apps.context.sources import customer_history  # noqa: F401
from apps.context.sources import previous_outcomes  # noqa: F401
from apps.context.sources import serviceflow_status  # noqa: F401
from apps.context.sources import previous_objections  # noqa: F401
