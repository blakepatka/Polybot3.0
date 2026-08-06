"""External data feeds: spot reference prices and Polymarket market data."""

from .spot import SpotFeed
from .polymarket import PolymarketFeed

__all__ = ["SpotFeed", "PolymarketFeed"]
