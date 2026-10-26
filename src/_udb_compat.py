import importlib
import sys


def load(name):
    return importlib.import_module(name)


def run(role):
    from udb.cli import main

    return main(["_worker", role, *sys.argv[1:]])
