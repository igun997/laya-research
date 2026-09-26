"""laya-research realtime mutation simulator.

``python -m sim`` mutates a bounded sample of the latest ``market_facts`` day,
publishes a tick payload on ``LAYA_TICK_CHANNEL`` and keeps the day rollups
fresh from a background thread.
"""

__all__ = ["mutate"]
