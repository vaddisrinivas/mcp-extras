"""Backward-compatibility shim — mcp_approval_proxy is now mcp_extras."""
# ruff: noqa: F401, F403, E402

import warnings as _warnings

_warnings.warn(
    "mcp_approval_proxy has been renamed to mcp_extras. "
    "Update your imports: from mcp_extras import ...",
    DeprecationWarning,
    stacklevel=2,
)

from mcp_extras import *
from mcp_extras import __all__, __version__
