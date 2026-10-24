"""Package the explicitly selected candidate EXE, never the installed EXE."""
import argparse
import json
import sysconfig
from pathlib import Path
from udb.release import binary_build_info, create_archive

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    print(json.dumps(create_archive(Path(__file__).resolve().parent,args.exe,args.output,
        binary_build_info(args.exe),sysconfig.get_paths()['purelib']),indent=2))

if __name__ == '__main__':
    main()
