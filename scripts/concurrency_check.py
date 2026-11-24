"""Concurrency stress checks for the compare-before-commit workflow.

Run with: python3 scripts/concurrency_check.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


def batch(prefix, count=120, offset=0):
    return [(f"{prefix}-{i:04d}", (i * 131 + offset) % core.WEIGHT_SIZE) for i in range(count)]


def fresh_db():
    fd, path = tempfile.mkstemp(prefix="concurrency-", suffix=".db")
    os.close(fd)
    core.init_db_path(path)
    return path


def drop(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def _run_paused_precheck(path, target_day, base, started, release, result):
    """Run a precheck and pause it at the start of the comparison.

    The pause happens after precheck_tickets has opened its read transaction
    and snapshotted the window generations, so a concurrent write really does
    race the comparison itself.
    """
    original = core._compare_with_window
    fired = {"once": False}

    def paused(conn, target_day_arg, target_map, window_start, window_end, **kwargs):
        if not fired["once"]:
            fired["once"] = True
            started.set()
            release.wait(timeout=10)
        return original(
            conn, target_day_arg, target_map, window_start, window_end, **kwargs
        )

    core._compare_with_window = paused
    conn = core.connect(path)
    try:
        result["precheck"] = core.precheck_tickets(conn, target_day, base)
    finally:
        core._compare_with_window = original
        conn.close()


def test_outside_window_write_during_slow_comparison():
    """A report outside the window must commit while a comparison is busy."""
    path = fresh_db()
    setup = core.connect(path)
    base = batch("A")
    core.replace_day_tickets(setup, "2026-05-01", base)
    setup.close()

    started = threading.Event()
    release = threading.Event()
    result = {}
    worker = threading.Thread(
        target=_run_paused_precheck,
        args=(path, "2026-05-02", base, started, release, result),
    )
    worker.start()
    assert started.wait(timeout=10), "comparison never started"

    write_conn = core.connect(path)
    t0 = time.monotonic()
    summary = core.commit_reported_day(write_conn, "2026-05-20", [("OUT", 1)])
    elapsed = time.monotonic() - t0
    write_conn.close()

    release.set()
    worker.join()

    check = core.connect(path)
    rows = core.get_day_readback(check, "2026-05-20")["tickets"]
    check.close()
    drop(path)
    assert summary["new_count"] == 1
    assert len(rows) == 1
    # The write did not wait for the comparison to finish.
    assert elapsed < 5, elapsed
    return {"elapsed_seconds": round(elapsed, 4), "outside_report_committed": True}


def test_window_write_during_slow_comparison():
    """A window write commits; it invalidates the precheck being computed."""
    path = fresh_db()
    setup = core.connect(path)
    base = batch("A")
    core.replace_day_tickets(setup, "2026-05-01", base)
    setup.close()

    started = threading.Event()
    release = threading.Event()
    result = {}
    worker = threading.Thread(
        target=_run_paused_precheck,
        args=(path, "2026-05-02", base, started, release, result),
    )
    worker.start()
    assert started.wait(timeout=10), "comparison never started"

    conn = core.connect(path)
    t0 = time.monotonic()
    core.commit_reported_day(conn, "2026-05-05", [("W", 1)])
    elapsed = time.monotonic() - t0
    conn.close()

    release.set()
    worker.join()

    check = core.connect(path)
    try:
        core.confirm_precheck(check, result["precheck"]["precheck_id"])
        stale = False
    except core.PrecheckStale as exc:
        stale = exc.stale_dates
    finally:
        check.close()
    drop(path)
    assert elapsed < 5, elapsed
    assert stale, "precheck built on a stale window must not confirm"
    return {
        "elapsed_seconds": round(elapsed, 4),
        "window_report_committed": True,
        "confirm_refused": bool(stale),
        "stale_dates": stale,
    }


def test_concurrent_reuploads_same_day():
    """Two uploads for one date: the later submission is the one that stays."""
    path = fresh_db()
    conn = core.connect(path)
    core.replace_day_tickets(conn, "2026-05-01", batch("A"))
    conn.close()

    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def worker(label):
        conn = core.connect(path)
        try:
            seq = core.allocate_submission(conn)
            barrier.wait(timeout=5)
            try:
                core.commit_reported_day(conn, "2026-05-04", [(label, 10)], submission_seq=seq)
                with lock:
                    outcomes.append((label, seq, "committed"))
            except core.PrecheckStale:
                with lock:
                    outcomes.append((label, seq, "refused"))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check = core.connect(path)
    rows = core.get_day_readback(check, "2026-05-04")["tickets"]
    check.close()
    drop(path)
    assert len(rows) == 1, rows
    winner = rows[0]["ticket_no"]
    winner_seq = next(seq for label, seq, _ in outcomes if label == winner)
    loser = next(item for item in outcomes if item[0] != winner)
    assert loser[2] == "refused"
    assert winner_seq > loser[1]
    return {"winner": winner, "outcomes": outcomes}


def _race_confirm_and_report(path, precheck, report_first: bool):
    """Confirm and a window re-report race from a shared barrier."""
    barrier = threading.Barrier(2)
    outcome = {}

    def confirmer():
        conn = core.connect(path)
        try:
            barrier.wait(timeout=5)
            try:
                core.confirm_precheck(conn, precheck["precheck_id"])
                outcome["confirm"] = "ok"
            except core.PrecheckStale as exc:
                outcome["confirm"] = "stale"
                outcome["dates"] = exc.stale_dates
        finally:
            conn.close()

    def reporter():
        conn = core.connect(path)
        try:
            if report_first:
                core.commit_reported_day(conn, "2026-05-04", [("R", 1)])
            barrier.wait(timeout=5)
            if not report_first:
                core.commit_reported_day(conn, "2026-05-04", [("R", 1)])
            outcome["report"] = "ok"
        finally:
            conn.close()

    threads = [threading.Thread(target=confirmer), threading.Thread(target=reporter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcome


def test_expired_confirmation_is_refused():
    """A window re-report committed before confirm makes the precheck stale."""
    path = fresh_db()
    setup = core.connect(path)
    base = batch("A")
    core.replace_day_tickets(setup, "2026-05-01", base)
    precheck = core.precheck_tickets(setup, "2026-05-02", base)
    setup.close()

    outcome = _race_confirm_and_report(path, precheck, report_first=True)

    check = core.connect(path)
    target_rows = core.get_day_readback(check, "2026-05-02")["tickets"]
    window_rows = core.get_day_readback(check, "2026-05-04")["tickets"]
    pending = core.list_pending_prechecks(check)
    check.close()
    drop(path)

    assert outcome["confirm"] == "stale", outcome
    assert "2026-05-04" in outcome["dates"], outcome
    assert target_rows == [], "stale confirmation must not store the old draft"
    assert len(window_rows) == 1
    assert pending, "stale precheck must remain available for a recheck"
    return outcome


def test_confirmation_committed_before_window_report_wins():
    """If confirm committed first, the later window report stands on its own."""
    path = fresh_db()
    setup = core.connect(path)
    base = batch("A")
    core.replace_day_tickets(setup, "2026-05-01", base)
    precheck = core.precheck_tickets(setup, "2026-05-02", base)
    setup.close()

    outcome = _race_confirm_and_report(path, precheck, report_first=False)

    check = core.connect(path)
    target_rows = core.get_day_readback(check, "2026-05-02")["tickets"]
    window_rows = core.get_day_readback(check, "2026-05-04")["tickets"]
    check.close()
    drop(path)

    assert len(window_rows) == 1, outcome
    if outcome["confirm"] == "ok":
        assert len(target_rows) == len(base)
    else:
        assert outcome["confirm"] == "stale"
        assert target_rows == []
    return outcome


def main():
    results = {
        "outside_window_during_comparison": test_outside_window_write_during_slow_comparison(),
        "window_during_comparison": test_window_write_during_slow_comparison(),
        "concurrent_same_day_reuploads": test_concurrent_reuploads_same_day(),
        "expired_confirmation_is_refused": test_expired_confirmation_is_refused(),
        "confirmation_before_window_report": test_confirmation_committed_before_window_report_wins(),
    }
    for name, value in results.items():
        print(f"{name}: {value}")


if __name__ == "__main__":
    main()
