"""Offline allowlist/credential/ZIP verification; never execute archive contents."""
import argparse
import json
from pathlib import Path
from udb.release import verify_archive

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive',type=Path)
    parser.add_argument('--sha256',help='Expected hash from a separately trusted channel')
    args = parser.parse_args()
    print(json.dumps(verify_archive(args.archive,args.sha256),indent=2))
