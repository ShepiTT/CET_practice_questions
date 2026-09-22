"""Expose CUDA wheels installed in the isolated data environment on Windows."""
import os
from pathlib import Path
import sys

_handles = []


def load_cuda_libraries():
    if sys.platform != 'win32':
        return
    root = Path(sys.prefix) / 'Lib/site-packages/nvidia'
    folders = sorted({str(path.parent) for path in root.rglob('*.dll')})
    for folder in folders:
        _handles.append(os.add_dll_directory(folder))
    os.environ['PATH'] = os.pathsep.join(folders + [os.environ.get('PATH', '')])
