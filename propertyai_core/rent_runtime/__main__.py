"""No default role, no scheduler activation, no fixture loader, no public listener."""
from __future__ import annotations
import argparse
from pathlib import Path
from uuid import UUID

from .artifact import verify
from .common import OperationalError, observe
from .worker import config_error, emit, run_worker_process


def _worker_refusal(run_id: UUID | None = None) -> int:
    code, payload = config_error(run_id)
    return emit(code, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("web", "worker", "backup"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--config-sha256")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--after-file", type=Path)
    parser.add_argument("--after-sha256")
    args = parser.parse_args()

    if args.role == "backup":
        if (
            not all((args.config, args.config_sha256, args.manifest_sha256))
            or args.state is not None
            or args.dry_run
            or args.run_id is not None
            or args.after_file is not None
            or args.after_sha256 is not None
        ):
            observe("BACKUP", "CONFIG_INVALID", error_class="CONFIG_INVALID")
            return 78
        try:
            root = Path(__file__).resolve().parents[2]
            manifest = verify(root, args.manifest_sha256)
            from .persistent_backup import run_persistent_backup
            run_persistent_backup(
                manifest=manifest,
                config_path=args.config,
                config_sha256=args.config_sha256,
            )
            return 0
        except OperationalError as error:
            if error.code == "BACKUP_FAILED":
                return 75 if error.state == "FAILED_UNKNOWN_EFFECT" else 70
            error_class = "PACKAGE_INVALID" if error.code == "PACKAGE_INVALID" else "CONFIG_INVALID"
            observe("BACKUP", "FAILED_PRE_EFFECT", error_class=error_class)
            return 78
        except Exception:
            observe("BACKUP", "FAILED_PRE_EFFECT", error_class="CONFIG_INVALID")
            return 78

    if args.role == "worker":
        try:
            run_id = UUID(args.run_id) if args.run_id is not None else None
        except (ValueError, TypeError):
            return _worker_refusal()
        if (not all((args.config, args.config_sha256, args.manifest_sha256)) or args.state is not None
                or ((args.after_file is None) != (args.after_sha256 is None))):
            return _worker_refusal(run_id)
        try:
            root = Path(__file__).resolve().parents[2]
            manifest = verify(root, args.manifest_sha256)
        except Exception:
            return _worker_refusal(run_id)
        code, payload = run_worker_process(
            manifest=manifest,
            config_path=args.config,
            config_sha256=args.config_sha256,
            dry_run=args.dry_run,
            run_id=run_id,
            after_path=args.after_file,
            after_sha256=args.after_sha256,
        )
        return emit(code, payload)

    if any((args.dry_run, args.run_id is not None, args.after_file is not None, args.after_sha256 is not None)):
        observe("WEB", "CONFIG_INVALID", error_class="CONFIG_INVALID")
        return 78
    if not all((args.config, args.config_sha256, args.manifest_sha256, args.state)):
        observe("WEB", "CONFIG_INVALID", error_class="CONFIG_INVALID")
        return 78
    try:
        root = Path(__file__).resolve().parents[2]
        manifest = verify(root, args.manifest_sha256)
    except Exception:
        observe("WEB", "FAILED_PRE_EFFECT", error_class="PACKAGE_INVALID")
        return 78
    try:
        from .web import load_config, serve
        config = load_config(args.config, args.config_sha256)
        return serve(config, manifest, args.state)
    except Exception:
        observe("WEB", "CONFIG_INVALID", error_class="CONFIG_INVALID")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
