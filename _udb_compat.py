"""Compatibility bootstrap; implementations live only in src/udb."""
import importlib
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
def load(name):return importlib.import_module(name)
def run(role):
 from udb.cli import main
 return main(['_worker',role,*sys.argv[1:]])
