"""Discover public schemas and counts from implementations, never constants."""
import asyncio
from pathlib import Path
import tempfile
from .catalog import public_catalog

def catalog():
    from .core import Agent
    from .server import make_server
    from .companion.runtime import TOOLS
    with tempfile.TemporaryDirectory(prefix='udb-catalog-') as temp:
        server=make_server(Agent(Path(temp)))
        native=[tool.model_dump(mode='json',exclude_none=True) for tool in asyncio.run(server.list_tools())]
    core_route=object();companion_route=object()
    routes={t['name']:core_route for t in native};routes.update({t['name']:companion_route for t in TOOLS})
    public=public_catalog([*native,*TOOLS],routes)
    return dict(tools=public,tool_count=len(public),backend_count=len(native)+len(TOOLS),core_count=len(native),companion_count=len(TOOLS))
