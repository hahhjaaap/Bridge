import sys

from _udb_compat import load

sys.modules[__name__] = load("udb.companion.extensions")
