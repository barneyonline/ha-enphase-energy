"""Internal implementation boundaries for the Enphase cloud client.

The public compatibility facade remains :mod:`custom_components.enphase_ev.api`.
These modules deliberately depend only on an injected ``aiohttp.ClientSession``
and narrow callbacks so they can be extracted into a standalone library later.
"""
