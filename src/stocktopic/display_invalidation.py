"""Atomic, persistent display invalidation inside existing business transactions."""
from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from inspect import signature

PREFIX = "display_v1:"
SECTIONS = ("core", "funds", "research")
_scope = ContextVar("display_mutation", default=None)


def get_value(con, key, default=None):
    row = con.execute("SELECT value FROM metadata WHERE key=?", (PREFIX + key,)).fetchone()
    return json.loads(row[0]) if row else default


def put_value(con, key, value):
    con.execute(
        "INSERT INTO metadata(key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (PREFIX + key, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
         datetime.now(UTC).isoformat()),
    )


def enqueue(con, sections=SECTIONS, theme_id=None, *, source=False):
    """Caller owns the transaction. Requests coalesce without invalidating source data."""
    pending = get_value(con, "pending", {})
    targets = ([str(v) for v in theme_id] if isinstance(theme_id, (list, tuple, set))
               else [str(theme_id) if theme_id is not None else "*"])
    if not targets:
        return
    for section in sections:
        pending[section] = sorted(set(pending.get(section, [])) | set(targets))
        if source:
            for target in targets:
                key = f"epoch:{section}:{target}"
                put_value(con, key, get_value(con, key, 0) + 1)
    if source:
        put_value(con, "revision", get_value(con, "revision", 0) + 1)
        put_value(con, "changed_at", datetime.now(UTC).timestamp())
    put_value(con, "pending", pending)


def invalidate_transaction(con):
    scope = _scope.get()
    if scope:
        sections, target = scope
        if isinstance(target, dict):
            if target["kind"] == "theme":
                target = target["owner"]
            else:
                target = [r[0] for r in con.execute(
                    "SELECT DISTINCT m.theme_id FROM theme_members m "
                    "JOIN candidate_themes t ON t.id=m.theme_id WHERE m.code=? AND m.active=1 "
                    "AND (t.status='confirmed' OR EXISTS "
                    "(SELECT 1 FROM metadata d WHERE d.key=? || m.theme_id || ':funds'))",
                    (target["owner"], PREFIX + "theme:"),
                )]
        enqueue(con, sections, target, source=True)


def display_mutation(*sections, theme_arg=None):
    def decorate(fn):
        sig = signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            target = (sig.bind_partial(*args, **kwargs).arguments.get(theme_arg)
                      if theme_arg else None)
            token = _scope.set((sections, target))
            try:
                return fn(*args, **kwargs)
            finally:
                _scope.reset(token)
        return wrapped
    return decorate


def display_metadata_mutation(fn):
    @wraps(fn)
    def wrapped(self, key, value):
        if key.startswith(("direction_state:", "direction_event:")):
            target = key.split(":")[1]
        elif key.startswith(("market_signal_coverage:", "kpl_event_coverage:")):
            target = None
        elif key == "direction_member_scope":
            target = None
        else:
            return fn(self, key, value)
        token = _scope.set((("core",), target))
        try:
            return fn(self, key, value)
        finally:
            _scope.reset(token)
    return wrapped


def display_flow_mutation(fn):
    @wraps(fn)
    def wrapped(self, kind, owner, *args, **kwargs):
        token = _scope.set((("funds",), {"kind": kind, "owner": str(owner)}))
        try:
            return fn(self, kind, owner, *args, **kwargs)
        finally:
            _scope.reset(token)
    return wrapped
