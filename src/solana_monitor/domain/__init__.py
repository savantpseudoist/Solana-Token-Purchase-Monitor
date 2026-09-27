"""Domain layer: framework-free models and rules.

Nothing in this package may import ``telegram``, ``aiohttp``, or any other
infrastructure library; it is the part of the system that must stay trivially
testable.
"""

from __future__ import annotations
