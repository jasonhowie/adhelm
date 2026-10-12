"""Typed provider adapters.

Adapters expose only implemented, explicitly capability-bound operations.  An
importable adapter does not imply provider access or live verification.
"""

from adhelm.providers.ga4 import Ga4DataClient, Ga4Identity

__all__ = ["Ga4DataClient", "Ga4Identity"]
