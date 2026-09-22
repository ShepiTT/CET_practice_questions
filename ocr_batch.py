"""Parallel OCR driver: one worker process per logical processor.

onnxruntime has no thread knob we can reach through RapidOCR, and left alone a single
process spawns one thread per core and thrashes. So instead of one wide process we run N
narrow ones: each worker pins itself to a single logical processor with
SetProcessAffinityMask, so its threads cannot spread, and N files are recognised at once.
Throughput scales with N.

Workers run at BELOW_NORMAL priority, so while you are away they get the whole CPU, and
the moment you touch the machine your own apps preempt them.

--reserve N keeps N logical processors clear of workers. On an Intel hybrid chip the
reserved ones are taken from the P-cores (whole physical cores, from the top of the
range): reserving the E-cores instead would hand you the slow cores and keep the fast
ones busy, which is the opposite of what you want while you are using the machine.

    python ocr_batch.py                 # leave at least 4 whole physical cores free
    python ocr_batch.py --reserve 10    # additionally reserve at least 10 logical CPUs
    python ocr_batch.py --workers 8     # cap the worker count directly
    python ocr_batch.py --redo          # also redo caches that predate the raw-box .json
    python ocr_batch.py a.pdf b.pdf     # exactly these files

Longest files start first (longest-processing-time-first), which minimises the time the
last worker is left running alone.
"""
import multiprocessing as mp
import os
import struct
import sys
import time

import ocr

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def pin_to_core(core):
    """Confine this process to one logical processor, at below-normal priority. Windows only."""
    if sys.platform != 'win32':
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetCurrentProcess()
    kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
    kernel32.SetProcessAffinityMask(handle, 1 << core)


RELATION_PROCESSOR_CORE = 0
GROUP_AFFINITY_SIZE = 16          # KAFFINITY mask (8) + Group (2) + Reserved[3] (6)


def cpu_topology():
    """[(efficiency_class, [logical ids]), ...] — one entry per PHYSICAL core, or None.

    Reads GetLogicalProcessorInformationEx directly: no psutil, no subprocess (a
    PowerShell probe for this was unreliable here). On hybrid chips Windows reports
    EfficiencyClass 0 for the E-cores and a higher class for the P-cores, so we do not
    have to guess the enumeration order."""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        length = wintypes.DWORD(0)
        kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, None, ctypes.byref(length))
        buf = (ctypes.c_ubyte * length.value)()
        if not kernel32.GetLogicalProcessorInformationEx(RELATION_PROCESSOR_CORE, buf, ctypes.byref(length)):
            return None
        raw, cores, offset = bytes(buf), [], 0
        while offset < len(raw):
            relationship, size = struct.unpack_from('<II', raw, offset)
            if not size:
                break
            if relationship == RELATION_PROCESSOR_CORE:
                efficiency = raw[offset + 9]
                groups = max(1, struct.unpack_from('<H', raw, offset + 30)[0])
                ids = []
                for g in range(groups):
                    mask = struct.unpack_from('<Q', raw, offset + 32 + g * GROUP_AFFINITY_SIZE)[0]
                    ids += [i for i in range(64) if mask >> i & 1]
                cores.append((efficiency, sorted(ids)))
            offset += size
        return cores or None
    except Exception:
        return None


def core_layout():
    """(P-core logical ids, E-core logical ids). Everything is 'P' on a non-hybrid chip."""
    topo = cpu_topology()
    if not topo:
        return list(range(mp.cpu_count())), []
    slowest = min(eff for eff, _ in topo)
    if len({eff for eff, _ in topo}) < 2:
        return sorted(i for _eff, ids in topo for i in ids), []
    return (sorted(i for eff, ids in topo if eff > slowest for i in ids),
            sorted(i for eff, ids in topo if eff == slowest for i in ids))


def choose_cores(reserve):
    """Logical processors the workers may use, keeping `reserve` of them free.

    Whole physical P-cores are freed, highest-numbered first, so what is left for you is
    a fast core with both its threads — not one thread of a core whose sibling is pegged,
    and not the E-cores."""
    topology = cpu_topology()
    if not topology or len(topology) <= 4:
        raise ValueError('Cannot reserve four physical cores; OCR batch not started')
    # Reserve the fastest whole cores first, including both SMT siblings.
    ranked = sorted(topology, key=lambda pair: (pair[0], max(pair[1])), reverse=True)
    free, free_cores = set(), 0
    while ranked and (free_cores < 4 or len(free) < max(0, reserve)):
        free.update(ranked.pop(0)[1])
        free_cores += 1
    usable = sorted(i for _, ids in ranked for i in ids)
    if not usable:
        raise ValueError('Requested reserve leaves no CPU for OCR')
    return usable


def worker(core, tasks, done):
    pin_to_core(core)
    while True:
        try:
            path = tasks.get_nowait()
        except Exception:
            return
        started = time.time()
        try:
            ocr.ocr_pdf(path, force=True)
            done.put((path, time.time() - started, None))
        except Exception as e:                       # one bad PDF must not stop the batch
            done.put((path, time.time() - started, repr(e)))


def page_count(path):
    import pdfplumber
    try:
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def needs_ocr(path, redo):
    """True when this scanned PDF has no usable cache. With redo, a cache written before
    raw boxes were kept (no .json beside the .txt) also counts as missing."""
    if ocr.has_text_layer(path):
        return False
    if ocr.cached_text(path) is None:
        return True
    return redo and not os.path.exists(ocr.cache_path(path, '.json'))


def collect(paths, redo):
    if paths:
        return [p for p in paths if os.path.exists(p)]
    found = []
    for folder in (ocr.DATA_DIR, os.path.join(ocr.DATA_DIR, 'answers')):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.endswith('.pdf') and needs_ocr(path, redo):
                found.append(path)
    return found


def take_flag(argv, name, default):
    if name not in argv:
        return argv, default
    i = argv.index(name)
    value = int(argv[i + 1])
    return argv[:i] + argv[i + 2:], value


def main(argv):
    redo = '--redo' in argv
    argv = [a for a in argv if a != '--redo']
    argv, reserve = take_flag(argv, '--reserve', 4)
    argv, cap = take_flag(argv, '--workers', 0)

    paths = collect(argv, redo)
    if not paths:
        print('nothing to do')
        return
    print(f'measuring {len(paths)} PDFs...', flush=True)
    sized = sorted(((page_count(p), p) for p in paths), reverse=True)   # longest first
    total_pages = sum(n for n, _ in sized)

    cores = choose_cores(reserve)
    if cap:
        cores = cores[:cap]
    cores = cores[:len(sized)]
    p_cores, e_cores = core_layout()
    free = [c for c in p_cores + e_cores if c not in set(cores)]
    print(f'{len(sized)} files, {total_pages} pages, {len(cores)} workers '
          f'on logical processors {",".join(map(str, cores))}'
          + (f' | left free for you: {",".join(map(str, free))}' if free else '')
          + '\nbelow-normal priority\n', flush=True)

    tasks, done = mp.Queue(), mp.Queue()
    for _pages, path in sized:
        tasks.put(path)
    procs = [mp.Process(target=worker, args=(core, tasks, done), daemon=True) for core in cores]
    started = time.time()
    for p in procs:
        p.start()

    finished_pages, failures = 0, []
    for i in range(1, len(sized) + 1):
        path, seconds, error = done.get()
        pages = next(n for n, p in sized if p == path)
        finished_pages += pages
        elapsed = time.time() - started
        rate = finished_pages / elapsed if elapsed else 0
        eta = (total_pages - finished_pages) / rate if rate else 0
        mark = 'FAILED' if error else 'ok'
        print(f'[{i}/{len(sized)}] {mark} {os.path.relpath(path, ocr.ROOT)} '
              f'({pages}p in {seconds:.0f}s) | {finished_pages}/{total_pages} pages, '
              f'{rate * 60:.0f} pages/min, ETA {eta / 60:.1f} min', flush=True)
        if error:
            failures.append((path, error))

    for p in procs:
        p.join(timeout=10)
    print(f'\ndone in {(time.time() - started) / 60:.1f} min')
    for path, error in failures:
        print(f'  FAILED {path}: {error}')


if __name__ == '__main__':
    main(sys.argv[1:])
