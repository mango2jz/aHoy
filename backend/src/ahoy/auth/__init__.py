"""Authentication: token crypto, login sessions (cookies) and the request dependencies.

Submodules are imported directly (`from ahoy.auth import sessions`); this package imports
nothing so the store can use `ahoy.auth.crypto` without an import cycle.
"""
