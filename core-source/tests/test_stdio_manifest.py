import json
from pathlib import Path
import pytest
from udb.desktop import mcp_command, command
from udb.core import PolicyError


def test_default_and_existing_stdio_manifest(tmp_path):
    assert mcp_command(tmp_path) == command(tmp_path, '--stdio')
    exe=tmp_path/'python.exe';exe.write_bytes(b'fixture')
    config={'mcpServers':{'universal-desktop-bridge':{'type':'stdio','command':'${PLUGIN_ROOT}/python.exe','args':['-u','${PLUGIN_ROOT}/projects/unified/udb_unified.py','--root','a b']}}}
    (tmp_path/'mcp.json').write_text(json.dumps(config))
    argv=mcp_command(tmp_path)
    assert Path(argv[0]) == exe
    assert argv[1:] == ['-u',str(tmp_path)+'/projects/unified/udb_unified.py','--root','a b']


@pytest.mark.parametrize('value', ['{broken', '{}', '[]', '"text"'])
def test_invalid_manifest_does_not_downgrade(tmp_path,value):
    (tmp_path/'mcp.json').write_text(value)
    with pytest.raises(PolicyError):mcp_command(tmp_path)


def test_oversized_manifest(tmp_path):
    (tmp_path/'mcp.json').write_bytes(b' '*65537)
    with pytest.raises(PolicyError):mcp_command(tmp_path)
