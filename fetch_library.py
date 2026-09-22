"""Sync CET papers, answer keys and listening audio from the CET通 source repo.

Source: https://github.com/whoamiARC/cet-website (public/library/cet4 and /cet6), the repo
behind https://www.cettong.cn — "所有资料完全免费，仅供个人学习使用".

The repo is sparse-cloned once into .cache/cet-website (blobs outside the requested levels
are never fetched); later runs just `git pull`, so only new papers are downloaded. Files are
then copied into place under data/ using this project's naming:

    public/library/cet4/2024_12_1/test.pdf       -> data/2024-12-CET4-1.pdf
    public/library/cet6/2024_12_1/answer.pdf     -> data/answers/2024-12-CET6-1.pdf
    public/library/cet6/2024_12_1/listening.mp3  -> data/audio/2024-12-CET6-1.mp3

Existing files are never overwritten (the papers already in data/ come from another
source and the parser is tuned to them).

    python fetch_library.py              # CET-4 (default)
    python fetch_library.py --level 6    # CET-6
    python fetch_library.py --level all  # both
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = 'https://github.com/whoamiARC/cet-website.git'
CACHE = os.path.join(ROOT, '.cache', 'cet-website')
DATA = os.path.join(ROOT, 'data')
LEVELS = ('4', '6')

TARGETS = {
    'test.pdf': (DATA, '.pdf'),
    'answer.pdf': (os.path.join(DATA, 'answers'), '.pdf'),
    'listening.mp3': (os.path.join(DATA, 'audio'), '.mp3'),
}


def git(*args):
    subprocess.run(['git', *args], check=True)


def library_dir(level):
    return os.path.join(CACHE, 'public', 'library', f'cet{level}')


def sync_repo(levels):
    """Clone or update the cache, making sure every requested level is checked out."""
    paths = [f'public/library/cet{level}' for level in levels]
    if os.path.isdir(os.path.join(CACHE, '.git')):
        print('updating existing clone...')
        git('-C', CACHE, 'sparse-checkout', 'set', *paths)
        git('-C', CACHE, 'pull', '--ff-only')
        return
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    print(f"cloning ({', '.join(paths)})...")
    git('clone', '--depth', '1', '--filter=blob:none', '--sparse', REPO, CACHE)
    git('-C', CACHE, 'sparse-checkout', 'set', *paths)


def paper_name(set_id, level):
    """'2024_12_1' -> '2024-12-CET6-1'; '2022_09_2-3' -> '2022-09-CET6-2-3'."""
    m = re.fullmatch(r'(\d{4})_(\d{2})_([\d-]+)', set_id)
    if not m:
        return None
    return f'{m.group(1)}-{m.group(2)}-CET{level}-{m.group(3)}'


def place_files(level):
    copied, kept = [], 0
    folder = library_dir(level)
    if not os.path.isdir(folder):
        print(f'  cet{level}: nothing checked out')
        return copied, kept
    for set_id in sorted(os.listdir(folder)):
        name = paper_name(set_id, level)
        if not name:
            print(f'  skipping unrecognised folder {set_id}')
            continue
        for src_name, (dest_dir, ext) in TARGETS.items():
            src = os.path.join(folder, set_id, src_name)
            if not os.path.exists(src):
                continue
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name + ext)
            if os.path.exists(dest):
                kept += 1
                continue
            shutil.copy2(src, dest)
            copied.append(os.path.relpath(dest, ROOT))
    return copied, kept


def main(argv):
    levels = ('4',)
    if '--level' in argv:
        value = argv[argv.index('--level') + 1]
        levels = LEVELS if value == 'all' else (value,)
    bad = [l for l in levels if l not in LEVELS]
    if bad:
        sys.exit(f"unknown level {bad[0]!r}; use 4, 6 or all")

    sync_repo(levels)
    total_copied, total_kept = [], 0
    for level in levels:
        copied, kept = place_files(level)
        total_copied += copied
        total_kept += kept
    for path in total_copied:
        print(f'  + {path}')
    print(f'\n{len(total_copied)} files added, {total_kept} already present.')
    if total_copied:
        print('Run `python app.py` (or import_answers.py) to import any new papers.')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except subprocess.CalledProcessError as e:
        sys.exit(f'git failed: {e}')
