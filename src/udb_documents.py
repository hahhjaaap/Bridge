import sys

from _udb_compat import load, run

if __name__ == "__main__":
    raise SystemExit(run("document"))
sys.modules[__name__] = load("udb.companion.documents")
