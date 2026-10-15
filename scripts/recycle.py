"""Recycle one verified local file, using modern Windows recycle-or-fail semantics."""
import json
import os
from pathlib import Path
import sys


def main():
    try:
        value = json.loads(sys.stdin.read(32768))
        path = Path(value['path'])
        if os.name != 'nt' or not path.is_absolute() or str(path).startswith('\\\\'):
            raise ValueError()
        # Import the modern implementation explicitly; never use a legacy fallback.
        from send2trash.win.modern import send2trash
        send2trash(str(path))
        if path.exists():
            raise OSError()
        print(json.dumps({'ok': True, 'operation': 'recycle', 'reclaimedBytes': 0}))
        return 0
    except Exception:
        print(json.dumps({'ok': False, 'error': 'RECYCLE_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
