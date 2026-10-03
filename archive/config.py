"""Explicit local archive configuration; no implicit account or conversation scope."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Config:
    account: str
    binary: Path
    talkers: tuple[str, ...]
    initial_since: int
    overlap_seconds: int = 3600
    settle_seconds: int = 120
    chunk_seconds: int = 86400
    reconcile_days: int = 30
    no_media: bool = False


def load_config(root, path=None):
    path = Path(path).expanduser() if path else Path(root) / 'config.toml'
    with path.open('rb') as handle:
        document = tomllib.load(handle)
    settings = document.get('archive')
    if not isinstance(settings, dict):
        raise ValueError('config requires [archive]')
    known = set(Config.__dataclass_fields__)
    if settings.keys() - known:
        raise ValueError('unknown archive configuration fields')
    account = settings.get('account')
    talkers = settings.get('talkers')
    if not isinstance(account, str) or not account.strip():
        raise ValueError('config requires an explicit account')
    if (not isinstance(talkers, list) or not talkers
            or any(not isinstance(t, str) or not t.strip() for t in talkers)
            or len(set(talkers)) != len(talkers)):
        raise ValueError('config requires a nonempty, unique conversation whitelist')
    binary = settings.get('binary')
    if not isinstance(binary, str) or not Path(binary).expanduser().is_absolute():
        raise ValueError('config binary must be an absolute local path')
    values = dict(settings, binary=Path(binary).expanduser(), talkers=tuple(talkers))
    for name, minimum in (('initial_since', 0), ('overlap_seconds', 0),
                          ('settle_seconds', 0), ('chunk_seconds', 1),
                          ('reconcile_days', 1)):
        value = values.get(name, Config.__dataclass_fields__[name].default)
        if type(value) is not int or value < minimum:
            raise ValueError(f'config {name} must be an integer >= {minimum}')
    if type(values.get('no_media', False)) is not bool:
        raise ValueError('config no_media must be boolean')
    return Config(**values)
