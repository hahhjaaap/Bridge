from _udb_compat import load, run
import sys
if __name__=='__main__':raise SystemExit(run('companion'))
sys.modules[__name__]=load('udb.companion.runtime')
