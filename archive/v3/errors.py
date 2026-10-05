"""Error taxonomy shared by every v3 component.

Every failure that a consumer can observe crosses a component boundary as an
ArchiveError with a stable machine-readable code. Codes are the contract;
messages are human hints. No component raises bare ValueError across a
boundary.
"""


class ArchiveError(Exception):
    """Base error. `code` is machine-readable; `detail` carries structured data."""

    def __init__(self, code, message, detail=None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_json(self):
        return {'code': self.code, 'message': self.message, 'detail': self.detail}

    @staticmethod
    def from_json(obj):
        return ArchiveError(obj.get('code', 'unknown_error'),
                            obj.get('message', 'unknown error'),
                            obj.get('detail') or {})


class GuardError(ArchiveError):
    """Disk/volume identity check failed — always fail closed."""


class VfsError(ArchiveError):
    """Pinned sqlite VFS unavailable or registration failed (fail closed)."""


class ProtocolError(ArchiveError):
    """Wire protocol violation, unknown op/version, or role violation."""


class AuthError(ProtocolError):
    """Role token / scope validation failed."""


class MasterError(ArchiveError):
    """Master store invariant violation (epoch, batch state, single writer)."""


class ReconcileError(ArchiveError):
    """Run application rejected (anomaly gate, incomplete enumeration...)."""


class ProjectionError(ArchiveError):
    """Projection missing/stale/revoked or master root passed to query entry."""


class BackupError(ArchiveError):
    """Backup/restore verification failed."""


class MigrationError(ArchiveError):
    """v2 migration invariant violated."""


class ExportError(ArchiveError):
    """S3 record export violates the nas-contract."""
