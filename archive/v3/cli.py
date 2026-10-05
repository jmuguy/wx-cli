"""Explicit operator CLI for the v3 archive stack.

The commands are deliberately opt-in. Production guard is the default and
fails closed on platforms or mounts whose identity cannot be proved. Synthetic
storage is available only when ``--allow-synthetic-guard`` is supplied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .errors import ArchiveError, GuardError, ProtocolError
from .guard import ProductionGuard, SyntheticGuard
from .util import canonical, sha256_hex


def _private_json(path, *, label):
    path = Path(path)
    try:
        st = path.lstat()
    except OSError as exc:
        raise ArchiveError('private_file_missing', f'{label} file unavailable: {exc}') from exc
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
        raise ArchiveError('private_file_permissions',
                           f'{label} must be a regular file with mode 0600 or stricter',
                           {'path': str(path)})
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ArchiveError('private_file_invalid', f'{label} JSON is unreadable') from exc
    if not isinstance(value, dict):
        raise ArchiveError('private_file_invalid', f'{label} must be a JSON object')
    return value


def _secret(path, field='key'):
    obj = _private_json(path, label='secret')
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise ArchiveError('secret_invalid', f'secret file must contain non-empty {field}')
    try:
        if len(value) % 2 == 0 and all(c in '0123456789abcdefABCDEF' for c in value):
            return bytes.fromhex(value)
    except ValueError:
        pass
    return value.encode('utf-8')


def _guard(args, archive_id=None, config_attr='guard_config'):
    if getattr(args, 'allow_synthetic_guard', False):
        return SyntheticGuard(archive_id or args.archive_id)
    config_path = getattr(args, config_attr, None)
    if not config_path:
        raise GuardError('guard_config_required',
                         'production guard configuration is required')
    config = _private_json(config_path, label='guard configuration')
    if archive_id is not None:
        config = dict(config)
        config['archive_id'] = archive_id
    return ProductionGuard(config)


def _open_store(args, *, writer=False):
    archive_id = args.archive_id
    guard = _guard(args)
    binding = guard.verify(args.root)
    from .master import MasterStore
    try:
        store = MasterStore(binding, archive_id)
        if writer:
            store.acquire_writer_lock()
        return binding, store
    except BaseException:
        binding.close()
        raise


def _close_store(binding, store):
    try:
        store.close()
    finally:
        binding.close()


def _load_role_config(path):
    data = _private_json(path, label='auth')
    if not isinstance(data.get('roles'), dict):
        raise ArchiveError('auth_config_invalid', 'auth file requires a roles object')
    return data


def _transport(args):
    from .transport import LocalSubprocessTransport, SshTransport
    if args.transport == 'ssh':
        if not args.host or not args.forced_command:
            raise ArchiveError('transport_config_required',
                               'SSH requires --host and --forced-command')
        return SshTransport(args.host, args.forced_command,
                             ssh_config=args.ssh_config)
    cmd = [sys.executable, '-m', 'archive.v3', 'serve',
           '--role', 'collector', '--root', str(args.root),
           '--archive-id', args.archive_id,
           '--auth-file', str(args.auth_file)]
    if args.allow_synthetic_guard:
        cmd.append('--allow-synthetic-guard')
    elif args.guard_config:
        cmd += ['--guard-config', str(args.guard_config)]
    return LocalSubprocessTransport(cmd)


def _collector(args):
    from .collector import CaptureClient, Collector
    transport = _transport(args)
    token_obj = _private_json(args.token_file, label='collector token')
    token = token_obj.get('token')
    if not isinstance(token, str) or not token:
        transport.close()
        raise ArchiveError('auth_token_invalid', 'token file requires a token field')
    client = CaptureClient(transport, args.archive_id, token)
    client.hello()
    return Collector(client, args.work_dir), transport


def _print(value):
    print(canonical(value))


def _backup_module():
    try:
        from . import backup
    except ImportError as exc:
        raise ArchiveError(
            'backup_dependency_missing',
            'backup and restore-drill require the Python cryptography package '
            'for AES-GCM; install it through the project environment') from exc
    return backup


def _cmd_init(args):
    root = Path(args.root)
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise GuardError('root_invalid',
                         'init requires an existing, non-symlink directory')
    scope = _private_json(args.scope, label='scope')
    raw_auth = _load_role_config(args.auth_file) if args.auth_file else None
    if args.allow_synthetic_guard:
        os.chmod(root, 0o700)
        SyntheticGuard.write_marker(root, args.archive_id, args.epoch,
                                    int(time.time()))
        guard = SyntheticGuard(args.archive_id)
    else:
        guard = _guard(args)
        # Verify physical topology, UUID/device/mount root and free-space
        # floor BEFORE the first marker or SQLite side effect.
        guard._prove_topology(os.path.abspath(root))
        guard._check_capacity(os.path.abspath(root))
        guard.write_marker(root, args.epoch, int(time.time()))
    binding = guard.verify(root)
    from .master import MasterStore
    try:
        store = MasterStore.initialize(binding, args.archive_id, scope,
                                       epoch=args.epoch)
        store.close()
    finally:
        binding.close()
    if raw_auth is not None:
        dest = root / 'auth.json'
        if dest.exists():
            raise ArchiveError('auth_exists', 'root auth.json already exists')
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            roles = {}
            for role, cfg in raw_auth['roles'].items():
                token = cfg.get('token') if isinstance(cfg, dict) else None
                if not isinstance(token, str) or not token:
                    raise ArchiveError('auth_config_invalid',
                                       f'auth role {role!r} requires token')
                roles[role] = {'token_sha256': hashlib.sha256(
                    token.encode('utf-8')).hexdigest()}
            os.write(fd, (canonical({'roles': roles}) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    _print({'ok': True, 'archive_id': args.archive_id,
            'root': str(root), 'epoch': args.epoch,
            'guard': 'synthetic' if args.allow_synthetic_guard else 'production'})


def _cmd_snapshot(args):
    collector, transport = _collector(args)
    payload = Path(args.out)
    if payload.exists():
        transport.close()
        raise ArchiveError('snapshot_output_exists', 'output directory must not exist')
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.mkdir(mode=0o700)
    wx_cli = Path(args.wx_cli)
    if not wx_cli.is_file() or not os.access(wx_cli, os.X_OK):
        transport.close()
        raise ArchiveError('wx_cli_missing', 'wx-cli executable is unavailable')
    source_key = Path(args.source_key_file)
    _private_json(source_key, label='source key')
    temp = Path(tempfile.mkdtemp(prefix='.wx-v3-snapshot-', dir=payload.parent))
    try:
        claim = collector.claim_source_snapshot(payload, args.account, args.talker)
        generation = temp / 'generation'
        proc = subprocess.run([str(wx_cli), 'archive-snapshot', '--source',
                              str(args.source), '--out', str(generation),
                              '--key-file', str(source_key), '--t-max-ms',
                              str(args.t_max_ms), '--json'],
                             capture_output=True, text=True)
        if proc.returncode:
            raise ArchiveError('snapshot_failed',
                               'archive-snapshot failed',
                               {'exit_code': proc.returncode,
                                'stderr': proc.stderr[-2000:]})
        export_dir = temp / 'export'
        inspect_args = [str(wx_cli), 'archive-inspect',
                                  '--snapshots', str(generation),
                                  '--key-file', str(source_key), '--action',
                                  'export', '--talker', args.talker,
                                  '--account', args.account,
                                  '--archive-id', args.archive_id,
                                  '--out', str(export_dir)]
        if args.attach_root:
            inspect_args += ['--attach-root', str(args.attach_root)]
        if args.dat_key_file:
            _private_json(args.dat_key_file, label='media decode key')
            inspect_args += ['--dat-key-file', str(args.dat_key_file)]
        inspect = subprocess.run(inspect_args,
                                 capture_output=True, text=True)
        if inspect.returncode:
            raise ArchiveError('inspect_failed', 'archive-inspect failed',
                               {'exit_code': inspect.returncode,
                                'stderr': inspect.stderr[-2000:]})
        for entry in export_dir.iterdir():
            shutil.move(str(entry), payload / entry.name)
        # claim file remains in place and is preserved next to the completed
        # export so collect_session can verify run/c0 authority.
        _print({'ok': True, 'claim': claim, 'export': str(payload),
                'snapshot_report': json.loads(proc.stdout) if proc.stdout.strip().startswith('{') else proc.stdout.strip()})
    except BaseException:
        # Keep the claim and any produced files as evidence for recovery.
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)
        transport.close()


def _cmd_collect(args):
    collector, transport = _collector(args)
    try:
        result = collector.collect_session(args.export)
        _print(result)
    finally:
        transport.close()


def _cmd_upload(args):
    from .collector import CaptureClient
    transport = _transport(args)
    try:
        token = _private_json(args.token_file, label='collector token').get('token')
        if not isinstance(token, str) or not token:
            raise ArchiveError('auth_token_invalid', 'token file requires token')
        client = CaptureClient(transport, args.archive_id, token)
        client.hello()
        info = client.upload_file(args.file, args.sha256, args.length,
                                  args.batch_id, kind=args.kind)
        _print({'ok': True, 'upload': info, 'batch_id': args.batch_id})
    finally:
        transport.close()


def _cmd_reconcile(args):
    from .collector import CaptureClient
    transport = _transport(args)
    try:
        token = _private_json(args.token_file, label='collector token').get('token')
        if not isinstance(token, str) or not token:
            raise ArchiveError('auth_token_invalid', 'token file requires token')
        client = CaptureClient(transport, args.archive_id, token)
        client.hello()
        run = _private_json(args.run_file, label='run')
        uploads = _private_json(args.media_uploads_file, label='media uploads') if args.media_uploads_file else {'uploads': []}
        result = client.commit_batch(args.batch_id, run,
                                     uploads.get('uploads', []))
        _print(result)
    finally:
        transport.close()


def _cmd_project(args):
    from .projection import ProjectionBuilder
    binding, store = _open_store(args, writer=True)
    try:
        policy = _private_json(args.policy, label='projection policy')
        projection_guard = None
        if not args.allow_synthetic_guard:
            projection_guard = _guard(args, args.archive_id + ':projection',
                                      config_attr='projection_guard_config')
        projection_id = args.archive_id + ':projection'
        try:
            store.binding.stat_path('projection/.v3_archive_identity.json')
            # Explicit project refreshes an existing projection in place:
            # validate its independent marker before using the existing
            # builder, update the active policy, then publish a new snapshot.
            pguard = (projection_guard or SyntheticGuard(projection_id))
            pbinding = pguard.verify(Path(store.binding.root) / 'projection')
            pbinding.close()
            builder = ProjectionBuilder(store, projection_id)
            builder.set_policy(policy)
        except GuardError as exc:
            if exc.code not in ('path_missing', 'path_open_failed'):
                raise
            builder = ProjectionBuilder.init(store, policy,
                                             projection_guard=projection_guard)
        try:
            manifest = builder.build_and_publish()
            _print(manifest)
        finally:
            builder.close()
    finally:
        _close_store(binding, store)


def _cmd_query(args):
    from .projection import ProjectionService
    if args.master_root:
        raise ArchiveError('master_root_rejected',
                           'query takes only an independent projection root')
    guard_config = None
    if not args.allow_synthetic_guard:
        guard_config = _private_json(args.guard_config, label='projection guard')
        guard_config['archive_id'] = args.archive_id + ':projection'
    service = ProjectionService.open(
        args.root, projection_id=args.archive_id + ':projection',
        account=args.account, expected_epoch=args.expected_epoch,
        guard_config=guard_config,
        allow_synthetic=args.allow_synthetic_guard)
    try:
        if args.match is not None:
            result = service.search(args.talker, args.match, limit=args.limit)
        elif args.seq is not None:
            result = service.context(args.talker, args.seq,
                                     before=args.before, after=args.after)
        elif args.talker:
            result = service.messages(args.talker, limit=args.limit)
        else:
            result = service.sessions()
        _print(result)
    finally:
        service.close()


def _cmd_backup(args):
    backup = _backup_module()
    binding, store = _open_store(args, writer=True)
    try:
        key = _secret(args.key_file)
        if args.kind == 'baseline':
            result = backup.create_baseline(store, args.target, key,
                                            verified_rotation=args.verified_rotation)
        elif args.kind == 'incremental':
            result = backup.create_incremental(store, args.target, key)
        else:
            result = backup.create_backup(store, args.target, key)
        _print(result)
    finally:
        _close_store(binding, store)


def _cmd_restore_drill(args):
    if not args.allow_synthetic_guard:
        raise GuardError(
            'production_restore_unverified',
            'restore-drill currently publishes a synthetic guard marker; '
            'production restore requires an independently verified production '
            'restore path and is refused by default')
    backup = _backup_module()
    result = backup.restore_drill(args.backup, _secret(args.key_file),
                                  scratch_dir=args.scratch,
                                  expect_archive_id=args.archive_id)
    _print(result)


def _cmd_migrate_v2(args):
    from .migrate_v2 import migrate_v2
    collector, transport = _collector(args)
    try:
        report = migrate_v2(args.v2_root, collector, args.work_dir,
                            account=args.account, dry_run=args.dry_run)
        _print(report)
    finally:
        transport.close()


def _cmd_status(args):
    backup = _backup_module()
    binding, store = _open_store(args)
    try:
        _print({'archive_id': args.archive_id, 'epoch': store.epoch(),
                'commit_seq': store.commit_seq(),
                'messages': store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                'scope': [dict(row) for row in store.db.execute(
                    'SELECT account,talker,capture_allowed,query_allowed FROM scope_grants')],
                'backup': backup.backup_status(store)})
    finally:
        _close_store(binding, store)


def _cmd_serve(args):
    from .endpoint import Endpoint, serve_endpoint
    if args.role == 'query':
        if not args.auth_file:
            raise ArchiveError('query_auth_required',
                               'query auth must be an explicit private file outside master root')
        auth_path = Path(args.auth_file).resolve()
        root = Path(args.root).resolve()
        master_root = root.parent if root.name == 'projection' else root
        if (auth_path == root or root in auth_path.parents or
                auth_path == master_root or master_root in auth_path.parents):
            raise ArchiveError('query_auth_isolation',
                               'query auth file must not be read from the master private root')
        projection_id = args.archive_id + ':projection'
        guard = _guard(args, projection_id, config_attr='projection_guard_config')
        binding = guard.verify(args.root)
        from .projection import ProjectionService
        guard_cfg = None if args.allow_synthetic_guard else _private_json(
            args.projection_guard_config, label='projection guard')
        if guard_cfg is not None:
            guard_cfg['archive_id'] = projection_id
        endpoint = Endpoint(binding, args.archive_id,
            projection_service_factory=lambda: ProjectionService.open(
                args.root, projection_id=projection_id, account=args.account,
                expected_epoch=args.expected_epoch, guard_config=guard_cfg,
                allow_synthetic=args.allow_synthetic_guard))
        endpoint.auth = _load_role_config(args.auth_file)
        endpoint.auth['roles'] = {
            'query': endpoint.auth['roles']['query']
        } if 'query' in endpoint.auth['roles'] else {}
        if not endpoint.auth['roles']:
            binding.close()
            raise ArchiveError('auth_role_missing',
                               'query auth file must configure the query role')
    else:
        guard = _guard(args)
        binding = guard.verify(args.root)
        endpoint = Endpoint(binding, args.archive_id)
        endpoint.open_master(writer=True)
        roles = endpoint.auth.get('roles', {})
        if 'collector' not in roles:
            endpoint.close()
            binding.close()
            raise ArchiveError('auth_role_missing',
                               'master auth file must configure collector role')
        endpoint.auth['roles'] = {'collector': roles['collector']}
    try:
        return serve_endpoint(endpoint, sys.stdin.buffer, sys.stdout.buffer)
    finally:
        endpoint.close()
        binding.close()


def _common(parser, *, query=False):
    parser.add_argument('--root', required=True,
                        help='master archive root; projection root for query')
    parser.add_argument('--archive-id', required=True)
    parser.add_argument('--allow-synthetic-guard', action='store_true',
                        help='explicit test-only guard; production default fails closed')
    parser.add_argument('--guard-config',
                        help='private production volume expectation JSON')
    parser.add_argument('--projection-guard-config',
                        help='private production projection volume expectation JSON')


def _network(parser):
    parser.add_argument('--transport', choices=('local', 'ssh'), default='local')
    parser.add_argument('--auth-file', required=True,
                        help='server hashed-token auth.json, mode 0600')
    parser.add_argument('--token-file', required=True,
                        help='client raw token JSON, mode 0600')
    parser.add_argument('--host')
    parser.add_argument('--forced-command')
    parser.add_argument('--ssh-config')
    parser.add_argument('--work-dir', type=Path, required=True)


def build_parser():
    parser = argparse.ArgumentParser(prog='python -m archive.v3')
    subs = parser.add_subparsers(dest='command', required=True)

    p = subs.add_parser('init', help='explicitly initialize a v3 master')
    _common(p)
    p.add_argument('--scope', required=True, type=Path)
    p.add_argument('--epoch', type=int, default=1)
    p.add_argument('--auth-file', type=Path,
                   help='private raw role-token input; hashed into root auth.json')
    p.set_defaults(func=_cmd_init)

    p = subs.add_parser('snapshot', help='claim, snapshot, inspect, then retain a source export')
    _common(p); _network(p)
    p.add_argument('--source', required=True, type=Path)
    p.add_argument('--source-key-file', required=True, type=Path)
    p.add_argument('--wx-cli', default='target/release/wx-cli')
    p.add_argument('--attach-root', type=Path)
    p.add_argument('--dat-key-file', type=Path)
    p.add_argument('--t-max-ms', type=int, default=30000)
    p.add_argument('--account', required=True); p.add_argument('--talker', required=True)
    p.add_argument('--out', required=True, type=Path)
    p.set_defaults(func=_cmd_snapshot)

    p = subs.add_parser('collect', help='collect a previously claimed session export')
    _common(p); _network(p)
    p.add_argument('--export', required=True, type=Path)
    p.set_defaults(func=_cmd_collect)

    p = subs.add_parser('upload', help='upload one content-addressed object into pending staging')
    _common(p); _network(p)
    p.add_argument('--file', required=True, type=Path)
    p.add_argument('--sha256', required=True); p.add_argument('--length', required=True, type=int)
    p.add_argument('--batch-id', required=True); p.add_argument('--kind', default='asset')
    p.set_defaults(func=_cmd_upload)

    p = subs.add_parser('reconcile', help='commit a prepared run through the NAS endpoint')
    _common(p); _network(p)
    p.add_argument('--run-file', required=True, type=Path)
    p.add_argument('--media-uploads-file', type=Path)
    p.add_argument('--batch-id', required=True)
    p.set_defaults(func=_cmd_reconcile)

    p = subs.add_parser('project', help='build and atomically publish the query projection')
    _common(p)
    p.add_argument('--policy', required=True, type=Path)
    p.set_defaults(func=_cmd_project)

    p = subs.add_parser('query', help='read only from an independent projection root')
    _common(p, query=True)
    p.add_argument('--account', required=True); p.add_argument('--expected-epoch', required=True, type=int)
    p.add_argument('--talker'); p.add_argument('--match'); p.add_argument('--seq', type=int)
    p.add_argument('--limit', type=int, default=50); p.add_argument('--before', type=int, default=10); p.add_argument('--after', type=int, default=10)
    p.add_argument('--master-root', help=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_query)

    p = subs.add_parser('backup', help='create an encrypted baseline or incremental backup')
    _common(p)
    p.add_argument('--target', required=True, type=Path); p.add_argument('--key-file', required=True, type=Path)
    p.add_argument('--kind', choices=('auto', 'baseline', 'incremental'), default='auto')
    p.add_argument('--verified-rotation', action='store_true')
    p.set_defaults(func=_cmd_backup)

    p = subs.add_parser('restore-drill', help='restore and verify a backup chain in a new root')
    _common(p)
    p.add_argument('--backup', required=True, type=Path); p.add_argument('--scratch', required=True, type=Path)
    p.add_argument('--key-file', required=True, type=Path)
    p.set_defaults(func=_cmd_restore_drill)

    p = subs.add_parser('migrate-v2', help='snapshot and migrate an existing v2 archive')
    _common(p); _network(p)
    p.add_argument('--v2-root', required=True, type=Path); p.add_argument('--account')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=_cmd_migrate_v2)

    p = subs.add_parser('status', help='report master scope, epoch, commit and backup posture')
    _common(p)
    p.set_defaults(func=_cmd_status)

    p = subs.add_parser('serve', help='forced-command JSONL endpoint')
    _common(p)
    p.add_argument('--role', choices=('collector', 'query'), required=True)
    p.add_argument('--auth-file', type=Path,
                   help='hashed role auth file; query path must be outside master root')
    p.add_argument('--account'); p.add_argument('--expected-epoch', type=int)
    p.set_defaults(func=_cmd_serve)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        return result if isinstance(result, int) else 0
    except (ArchiveError, GuardError, ProtocolError) as exc:
        print(canonical({'ok': False, 'error': exc.to_json()}), file=sys.stderr)
        return 2
    except Exception as exc:
        # Keep operator-facing diagnostics useful without exposing key data.
        print(canonical({'ok': False, 'error': {'code': 'cli_failed',
              'message': f'{type(exc).__name__}: {exc}'}}), file=sys.stderr)
        return 1
