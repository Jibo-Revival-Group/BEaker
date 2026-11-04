#!/usr/bin/env python3
"""Build a stock-format "os" OTA package (./filesystem.tar.bz2 in a tar) from a Jibo rootfs.ext4.

Uses e2fsprogs' debugfs instead of a loop mount, so it works in containers. Owners, modes and
device nodes come from the image's own listing, not from the extracted copy.
Usage: tools/make_os_package.py rootfs.ext4 updates/packages/os.tar
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

# `debugfs ls -p` line: /inode/mode(octal)/uid/gid/name/size/
LS_P = re.compile(r'^/(\d+)/([0-7]+)/(\d+)/(\d+)/(.*)/(\d*)/$')


def debugfs(image, commands):
    with tempfile.NamedTemporaryFile('w', suffix='.cmds', delete=False) as f:
        f.write(''.join(c + '\n' for c in commands))
    try:
        return subprocess.run(['debugfs', '-f', f.name, image], capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(f.name)


def listing(image, dirs):
    """Map image path -> (mode, uid, gid) for every entry in the given directories."""
    entries, cwd = {}, ''
    for line in debugfs(image, ['ls -p "%s"' % d for d in dirs]).splitlines():
        echoed = re.match(r'^debugfs:\s+ls -p "(.*)"$', line)
        if echoed:
            cwd = echoed.group(1).rstrip('/')
            continue
        m = LS_P.match(line)
        if m and m.group(5) not in ('.', '..'):
            entries[cwd + '/' + m.group(5)] = (int(m.group(2), 8), int(m.group(3)), int(m.group(4)))
    return entries


def build_filesystem_tar(image, root, out):
    subprocess.run(['debugfs', '-R', 'rdump / ' + root, image], check=True, capture_output=True)
    dirs = ['/'] + ['/' + os.path.relpath(p, root) for p, _, _ in os.walk(root) if p != root]
    entries = listing(image, dirs)
    devices = [p for p, (mode, _, _) in entries.items() if stat.S_ISCHR(mode) or stat.S_ISBLK(mode)]
    numbers = {}
    for p in devices:  # rdump skips device nodes; take major/minor from the image
        info = debugfs(image, ['stat "%s"' % p])
        numbers[p] = tuple(map(int, re.search(r'Device major/minor number: (\d+):(\d+)', info).groups()))
    count = 0
    with tarfile.open(out, 'w:bz2', format=tarfile.GNU_FORMAT) as tar:
        tar.add(root, '.', recursive=False)
        for path in sorted(entries):
            if path == '/lost+found' or path.startswith('/lost+found/'):
                continue  # mkfs.ext4 on the robot makes its own
            mode, uid, gid = entries[path]
            local = root + path
            if path in numbers:
                info = tarfile.TarInfo('.' + path)
                info.type = tarfile.CHRTYPE if stat.S_ISCHR(mode) else tarfile.BLKTYPE
                info.devmajor, info.devminor = numbers[path]
            elif stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                info = tar.gettarinfo(local, '.' + path)
                if stat.S_IFMT(os.lstat(local).st_mode) != stat.S_IFMT(mode):
                    raise SystemExit('type differs after extract: ' + path)
            else:
                raise SystemExit('unsupported file type: ' + path)
            info.mode, info.uid, info.gid, info.uname, info.gname = stat.S_IMODE(mode), uid, gid, '', ''
            if info.isreg():
                with open(local, 'rb') as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
            count += 1
    return entries, count


def self_check(out, entries):
    """Every image entry (except lost+found) is in the tar with the image's mode/owner."""
    seen = {}
    with tarfile.open(out, 'r:bz2') as tar:
        for m in tar:
            seen[m.name[1:]] = (m.mode, m.uid, m.gid)
    for path, (mode, uid, gid) in entries.items():
        if path.startswith('/lost+found'):
            continue
        assert seen.get(path) == (stat.S_IMODE(mode), uid, gid), (path, seen.get(path), oct(mode))


def main(image, outfile):
    work = tempfile.mkdtemp(prefix='os-pkg-', dir=os.path.dirname(os.path.abspath(outfile)))
    try:
        root, pkg = os.path.join(work, 'root'), os.path.join(work, 'pkg')
        os.mkdir(root)
        os.mkdir(pkg)
        fs_tar = os.path.join(pkg, 'filesystem.tar.bz2')
        entries, count = build_filesystem_tar(image, root, fs_tar)
        self_check(fs_tar, entries)
        subprocess.run(['tar', '--numeric-owner', '--owner=0', '--group=0', '-C', pkg, '-cf', outfile, '.'], check=True)
        print('%s: %d entries, %d bytes' % (outfile, count, os.path.getsize(outfile)))
    finally:
        shutil.rmtree(work)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
