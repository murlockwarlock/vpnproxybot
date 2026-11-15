import argparse
import hashlib
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


TARGET = Path('/etc/nginx/sites-enabled/darimiru')
SNIPPET = Path('/etc/nginx/snippets/darimiru-email-auth.conf')
BACKUPS = Path('/root/darimiru-nginx-backups')
INCLUDE = 'include /etc/nginx/snippets/darimiru-email-auth.conf;'


def render_config(text):
    lines = text.splitlines(keepends=True)
    blocks = []
    start = None
    depth = 0
    for index, line in enumerate(lines):
        code = line.split('#', 1)[0].strip()
        if start is None and re.match(r'^server\s*\{', code):
            start = index
        if start is not None:
            depth += code.count('{') - code.count('}')
            if depth == 0:
                blocks.append((start, index + 1))
                start = None
    targets = []
    for start, end in blocks:
        code = ''.join(lines[start:end])
        if re.search(r'^\s*server_name\s+darimiru\.ru(?:\s|;)', code, re.MULTILINE) and re.search(r'^\s*listen\s+8444\s+ssl\s*;', code, re.MULTILINE):
            targets.append((start, end))
    if len(targets) != 1:
        raise ValueError('Expected one Darimiru HTTPS server')
    start, end = targets[0]
    matches = [index for index in range(start, end) if re.match(r'^\s*location\s+/profile\s*\{', lines[index])]
    if len(matches) != 1 or INCLUDE in text:
        raise ValueError('Unexpected profile location or existing include')
    index = matches[0]
    indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
    lines.insert(index, indent + INCLUDE + '\n\n')
    return ''.join(lines)


def write_atomic(path, content, mode, uid, gid):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def nginx_test():
    return subprocess.run(['nginx', '-t'], capture_output=True).returncode == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-config-sha256', required=True)
    parser.add_argument('--snippet', required=True, type=Path)
    parser.add_argument('--expected-snippet-sha256', required=True)
    args = parser.parse_args()
    if TARGET.is_symlink() or not TARGET.is_file() or SNIPPET.exists() or SNIPPET.is_symlink():
        raise SystemExit('Unexpected nginx target or existing snippet')
    original = TARGET.read_bytes()
    snippet = args.snippet.read_bytes()
    if hashlib.sha256(original).hexdigest() != args.expected_config_sha256 or hashlib.sha256(snippet).hexdigest() != args.expected_snippet_sha256:
        raise SystemExit('Nginx configuration or snippet changed after inspection')
    info = TARGET.stat()
    if info.st_uid != 0 or not nginx_test():
        raise SystemExit('Nginx configuration is not safe to change')
    updated = render_config(original.decode()).encode()
    if BACKUPS.is_symlink():
        raise SystemExit('Unsafe nginx backup directory')
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    backup_info = BACKUPS.stat()
    if backup_info.st_uid != 0 or stat.S_IMODE(backup_info.st_mode) & 0o077:
        raise SystemExit('Unsafe nginx backup directory permissions')
    backup = BACKUPS / ('darimiru-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.conf')
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    if backup.read_bytes() != original:
        raise SystemExit('Nginx backup verification failed')
    created = False
    modified = False
    try:
        fd = os.open(SNIPPET, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        created = True
        with os.fdopen(fd, 'wb') as handle:
            handle.write(snippet)
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(TARGET.read_bytes()).hexdigest() != args.expected_config_sha256:
            raise RuntimeError('Nginx configuration changed during installation')
        modified = True
        write_atomic(TARGET, updated, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        if not nginx_test():
            raise RuntimeError('Updated nginx configuration failed validation')
        if subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True).returncode:
            raise RuntimeError('Nginx reload failed')
    except Exception:
        if modified:
            write_atomic(TARGET, original, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        if created:
            SNIPPET.unlink(missing_ok=True)
        if modified and (not nginx_test() or subprocess.run(['systemctl', 'reload', 'nginx'], capture_output=True).returncode):
            raise SystemExit('Nginx rollback failed; inspect the saved backup')
        raise
    print('nginx_auth_routes: installed')
    print('nginx_backup:', backup)


if __name__ == '__main__':
    main()
