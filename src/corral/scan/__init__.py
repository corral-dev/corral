"""Agent session history parsers (implemented in SessKit)."""

from corral import _wire_sesskit_cache

# Bind the host cache only when history parsing is actually requested.
_wire_sesskit_cache()
