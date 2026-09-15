from __future__ import annotations
import argparse
import os


def main():
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    os.environ.setdefault('DO_NOT_TRACK', '1')
    os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', '60')
    os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '60')
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker')
    parser.add_argument('--prepare', action='append', choices=['turbo', 'large', 'diarization'])
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--screenshot')
    parser.add_argument('--self-check')
    args = parser.parse_args()
    from models import clear_legacy_offline_flags
    clear_legacy_offline_flags()
    if args.self_check:
        from diagnostics import self_check
        return self_check(args.self_check)
    if args.worker:
        import signal
        from worker import run_job, cancel_on_signal
        signal.signal(signal.SIGTERM, cancel_on_signal)
        return run_job(args.worker)
    if args.prepare:
        from models import install
        for model in args.prepare:
            install(model)
        return 0
    from gui import run_gui
    return run_gui(args.demo, args.screenshot)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
