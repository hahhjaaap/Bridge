from _udb_compat import load, run
import sys
if __name__=='__main__':raise SystemExit(run('legacy-proxy'))
sys.modules[__name__]=load('udb.proxy')
