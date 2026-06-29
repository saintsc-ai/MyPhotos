"""Cutover helper — repoint root folders after moving a catalog to a new host.

Typical use: bulk-index the backlog on a powerful GPU PC, copy ``data/`` to
the NAS (see docs/operations/porting.md), then rewrite each root's
``abs_path`` from the indexing machine's path to the NAS path. Because
``rel_path`` is stored OS-independently (POSIX + NFC, see
``app.scanner.utils.to_posix_rel``), **only ``abs_path`` changes** — no
per-photo fix-up, no re-index.

This is the scriptable, offline equivalent of the admin API PATCH described
in porting.md §4 — no running server, cookies, or curl needed. Run it on the
target host with the services stopped.

Examples
--------
List roots (id, path, photo count)::

    python -m app.tools.cutover --list

Preview a rewrite (dry-run is the default — nothing is written)::

    python -m app.tools.cutover --map "D:/Photos=/volume1/photos"

Apply it (and rewrite a second root by id)::

    python -m app.tools.cutover --map "D:/Photos=/volume1/photos" \
                                --id 2=/volume1/archive --apply

Matching is whitespace/back-slash/trailing-slash insensitive, so a root
stored as ``D:\\Photos`` still matches ``--map "D:/Photos=..."``.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import func, select

from ..db import SessionLocal
from ..models import Photo, Root
from ..scanner.utils import join_root


def _norm(p: str) -> str:
    """Normalize a path for comparison/storage: forward slashes, no trailing
    slash, trimmed. Mirrors how to_posix_rel treats the root prefix."""
    return p.strip().replace("\\", "/").rstrip("/")


def _split_pair(s: str) -> tuple[str, str]:
    if "=" not in s:
        raise argparse.ArgumentTypeError(
            f"'{s}' must be OLD=NEW (or ID=NEW for --id)")
    left, right = s.split("=", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        raise argparse.ArgumentTypeError(f"'{s}' has an empty side")
    return left, right


def _photo_count(db, root_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(Photo).where(Photo.root_id == root_id)
    ).scalar_one()


def cmd_list(db) -> int:
    roots = db.execute(select(Root).order_by(Root.id)).scalars().all()
    if not roots:
        print("(루트 없음)")
        return 0
    for r in roots:
        flag = "enabled" if r.enabled else "disabled"
        ro = "ro" if r.readonly else "rw"
        print(f"[{r.id}] {r.label!r}  {r.abs_path}"
              f"  ({flag}, {ro}, {_photo_count(db, r.id)} photos)")
    return 0


def _verify_sample(db, root_id: int, new_path: str, sample: int = 8) -> tuple[int, list[str]]:
    """Sample a few photos under the root and check they exist at the new
    path. Returns (checked, missing-rel-paths)."""
    rels = db.execute(
        select(Photo.rel_path)
        .where(Photo.root_id == root_id, Photo.status == "active")
        .limit(sample)
    ).scalars().all()
    missing = [rel for rel in rels if not os.path.exists(join_root(new_path, rel))]
    return len(rels), missing


def cmd_rewrite(db, by_path: list[tuple[str, str]], by_id: list[tuple[str, str]],
                apply: bool, verify: bool) -> int:
    path_map = {_norm(o): _norm(n) for o, n in by_path}
    id_map: dict[int, str] = {}
    for k, v in by_id:
        try:
            id_map[int(k)] = _norm(v)
        except ValueError:
            print(f"오류: --id 의 ID는 정수여야 합니다: {k!r}", file=sys.stderr)
            return 2

    roots = db.execute(select(Root).order_by(Root.id)).scalars().all()
    plan: list[tuple[Root, str]] = []
    for r in roots:
        new = id_map.get(r.id)
        if new is None:
            new = path_map.get(_norm(r.abs_path))
        if new is not None and new != _norm(r.abs_path):
            plan.append((r, new))

    if not plan:
        # Surface unmatched map keys so typos are obvious.
        root_paths = {_norm(r.abs_path) for r in roots}
        for old in path_map:
            if old not in root_paths:
                print(f"  · --map 의 OLD가 어떤 루트와도 안 맞음: {old}", file=sys.stderr)
        for rid in id_map:
            if rid not in {r.id for r in roots}:
                print(f"  · --id 의 ID가 없는 루트: {rid}", file=sys.stderr)
        print("바꿀 루트가 없습니다 (매칭되는 경로/ID 없음 또는 이미 동일).")
        return 1

    print(f"{'적용' if apply else '미리보기 (dry-run)'} — 루트 {len(plan)}개:")
    exit_code = 0
    for r, new in plan:
        print(f"  [{r.id}] {r.label!r}")
        print(f"      {r.abs_path}")
        print(f"   →  {new}")
        if verify:
            checked, missing = _verify_sample(db, r.id, new)
            if checked == 0:
                print("      검증: (활성 사진 없음 — 건너뜀)")
            elif missing:
                print(f"      ⚠ 검증 실패: 샘플 {checked}장 중 {len(missing)}장이 "
                      f"새 경로에 없음. 예: {join_root(new, missing[0])}")
                print("        → 원본 사진이 새 경로에 같은 폴더 구조로 있는지 확인하세요.")
                exit_code = 3
            else:
                print(f"      ✓ 검증: 샘플 {checked}장 모두 새 경로에 존재")
        if apply:
            r.abs_path = new

    if apply:
        if exit_code == 3:
            print("\n검증 경고가 있었지만 --apply 가 지정되어 그대로 기록합니다.")
        db.commit()
        print("\n기록 완료. 서비스를 시작하세요.")
    else:
        print("\n(dry-run — 아무것도 기록하지 않았습니다. 적용하려면 --apply 추가)")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.tools.cutover",
        description="이전 후 roots.abs_path 재작성 (rel_path는 OS 무관이라 그대로).",
    )
    ap.add_argument("--list", action="store_true",
                    help="루트 목록(id, 경로, 사진 수)만 출력하고 종료")
    ap.add_argument("--map", action="append", type=_split_pair, default=[],
                    metavar="OLD=NEW",
                    help="abs_path 가 OLD 인 루트를 NEW 로 변경 (반복 가능)")
    ap.add_argument("--id", action="append", type=_split_pair, default=[],
                    metavar="ID=NEW", dest="by_id",
                    help="루트 ID 로 지정해 NEW 로 변경 (반복 가능)")
    ap.add_argument("--apply", action="store_true",
                    help="실제로 DB에 기록 (없으면 dry-run)")
    ap.add_argument("--no-verify", action="store_true",
                    help="새 경로의 파일 존재 샘플 검증 건너뛰기")
    args = ap.parse_args(argv)

    with SessionLocal() as db:
        if args.list:
            return cmd_list(db)
        if not args.map and not args.by_id:
            ap.print_help()
            return 0
        return cmd_rewrite(db, args.map, args.by_id,
                           apply=args.apply, verify=not args.no_verify)


if __name__ == "__main__":
    raise SystemExit(main())
