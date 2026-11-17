"""Library placement and the replace sequence. Only single-file renames; never directories."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import SwapError

OLD_SUFFIX = '.arm-old'
PART_SUFFIX = '.part'


def _exists(p: str) -> bool:
    return os.path.lexists(p)


def _rename(src: str, dst: str) -> None:
    try:
        os.rename(src, dst)
    except OSError as e:
        raise SwapError(f'rename failed {src!r} -> {dst!r}: {e}') from e


def place_new(part_path: str, final_path: str) -> None:
    if not part_path.endswith(PART_SUFFIX):
        raise SwapError(f'not a .part file: {part_path}')
    if not os.path.isfile(part_path):
        raise SwapError(f'staged file missing: {part_path}')
    if _exists(final_path):
        raise SwapError(f'final path already exists: {final_path}')
    size = os.path.getsize(part_path)
    _rename(part_path, final_path)
    after = os.path.getsize(final_path)
    if after != size:
        raise SwapError(f'size changed after rename ({size} -> {after}): {final_path}')


def hide_old(old_path: str) -> str:
    hidden = old_path + OLD_SUFFIX
    if not os.path.isfile(old_path):
        raise SwapError(f'old file missing: {old_path}')
    if _exists(hidden):
        raise SwapError(f'hidden target already exists: {hidden}')
    _rename(old_path, hidden)
    return hidden


def restore_old(hidden_path: str) -> str:
    if not hidden_path.endswith(OLD_SUFFIX):
        raise SwapError(f'not an {OLD_SUFFIX} file: {hidden_path}')
    if not os.path.isfile(hidden_path):
        raise SwapError(f'hidden file missing: {hidden_path}')
    orig = hidden_path[:-len(OLD_SUFFIX)]
    if _exists(orig):
        raise SwapError(f'original name already occupied: {orig}')
    _rename(hidden_path, orig)
    return orig


def delete_hidden(hidden_path: str) -> None:
    if not hidden_path.endswith(OLD_SUFFIX) or len(os.path.basename(hidden_path)) <= len(OLD_SUFFIX):
        raise SwapError(f'refusing to delete non-{OLD_SUFFIX} file: {hidden_path}')
    if os.path.isdir(hidden_path) and not os.path.islink(hidden_path):
        raise SwapError(f'refusing to delete a directory: {hidden_path}')
    try:
        os.unlink(hidden_path)
    except FileNotFoundError:
        return                      # idempotent
    except OSError as e:
        raise SwapError(f'cannot delete {hidden_path}: {e}') from e


@dataclass
class ReplaceState:
    old_path: str
    hidden_path: str
    part_path: str
    final_path: str


def replace_begin(old_path, part_path, final_path) -> ReplaceState:
    if not os.path.isfile(old_path):
        raise SwapError(f'old file missing: {old_path}')
    if not part_path.endswith(PART_SUFFIX) or not os.path.isfile(part_path):
        raise SwapError(f'staged .part file missing: {part_path}')
    if final_path != old_path and _exists(final_path):
        raise SwapError(f'final path already exists and is not the old file: {final_path}')
    hidden = hide_old(old_path)
    return ReplaceState(old_path, hidden, part_path, final_path)


def replace_place(state: ReplaceState) -> None:
    if not os.path.isfile(state.hidden_path):
        raise SwapError(f'old file is not hidden: {state.hidden_path}')
    if not os.path.isfile(state.part_path) and os.path.isfile(state.final_path):
        return                      # already placed
    place_new(state.part_path, state.final_path)


def replace_finish(state: ReplaceState) -> None:
    if not os.path.isfile(state.final_path):
        raise SwapError(f'new file not in place, refusing to delete old: {state.final_path}')
    delete_hidden(state.hidden_path)


def replace_rollback(state: ReplaceState) -> str:
    did = []
    hidden = os.path.isfile(state.hidden_path)
    final = os.path.isfile(state.final_path)
    part = os.path.isfile(state.part_path)
    orig_is_hidden_target = state.final_path == state.old_path
    if not hidden:
        if final and orig_is_hidden_target and not part:
            raise SwapError('old file already gone and new file placed; cannot roll back')
        if _exists(state.old_path):
            return 'nothing to roll back: original file is in place'
        raise SwapError(f'hidden old file missing: {state.hidden_path}')
    if final:
        if part:
            raise SwapError(f'both {state.final_path} and {state.part_path} exist; manual fix needed')
        _rename(state.final_path, state.part_path)
        did.append(f'moved new file back to {state.part_path}')
    restore_old(state.hidden_path)
    did.append(f'restored old file {state.old_path}')
    return '; '.join(did)
