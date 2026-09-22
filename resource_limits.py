"""Bound local data jobs to an explicit CPU set and below-normal priority."""
import os
import sys


def limit_resources(cpu_ids):
    ids = sorted(set(cpu_ids))
    if not ids or any(i < 0 or i >= (os.cpu_count() or 1) for i in ids):
        raise ValueError('Invalid CPU set')
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        from ocr_batch import cpu_topology
        topology = cpu_topology()
        if not topology or sum(not set(cpus).intersection(ids) for _, cpus in topology) < 4:
            raise ValueError('At least four physical cores must remain free')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetCurrentProcess()
        if not kernel.SetProcessAffinityMask(handle, sum(1 << i for i in ids)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetPriorityClass(handle, 0x4000):
            raise ctypes.WinError(ctypes.get_last_error())
    elif hasattr(os, 'sched_setaffinity'):
        if len(ids) > (os.cpu_count() or 1) - 4:
            raise ValueError('At least four CPUs must remain free')
        os.sched_setaffinity(0, ids)
        os.nice(10)
    print(f'CPU set: {ids}; priority: below normal', flush=True)
