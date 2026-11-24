import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


def batch(prefix, count=120, weight_offset=0):
    return [
        (f"{prefix}-{i:04d}", (i * 131 + weight_offset) % core.WEIGHT_SIZE)
        for i in range(count)
    ]


class PrecheckBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="precheck-test-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)
        self.conn = core.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, day, tickets):
        return core.replace_day_tickets(self.conn, day, tickets)


class WindowAndThresholdTests(PrecheckBase):
    def test_window_is_seven_days_each_side_and_crosses_year(self):
        self.upload("2025-12-28", batch("A"))
        hits = core.precheck_tickets(self.conn, "2026-01-02", batch("A"))
        self.assertTrue(hits["hit"])
        self.assertEqual(hits["window_start"], "2025-12-26")
        self.assertEqual(hits["window_end"], "2026-01-09")
        self.assertEqual([m["date"] for m in hits["matches"]], ["2025-12-28"])

    def test_day_eight_days_away_is_outside_window(self):
        self.upload("2026-05-01", batch("A"))
        result = core.precheck_tickets(self.conn, "2026-05-09", batch("A"))
        self.assertFalse(result["hit"])
        self.assertIsNone(result["precheck_id"])

    def test_boundary_seven_days_counts(self):
        self.upload("2026-05-01", batch("A"))
        result = core.precheck_tickets(self.conn, "2026-05-08", batch("A"))
        self.assertTrue(result["hit"])

    def test_both_sides_need_one_hundred_tickets(self):
        self.upload("2026-05-01", batch("A", count=99))
        result = core.precheck_tickets(self.conn, "2026-05-02", batch("A", count=99))
        self.assertFalse(result["hit"])

    def test_both_sides_need_ninety_five_percent(self):
        base = batch("A", count=100)
        # Neighbor has 120 tickets; the report shares 96. Its own ratio is
        # 80%, so even though the report is at 96% there is no hit.
        neighbour = base + batch("B", count=20)
        self.upload("2026-05-01", neighbour)
        target = base[:96] + batch("C", count=4)
        result = core.precheck_tickets(self.conn, "2026-05-02", target)
        self.assertFalse(result["hit"])

    def test_exactly_ninety_five_percent_on_both_sides_hits(self):
        base = batch("A", count=100)
        self.upload("2026-05-01", base)
        target = base[:95] + batch("C", count=5)
        result = core.precheck_tickets(self.conn, "2026-05-02", target)
        self.assertTrue(result["hit"])
        match = result["matches"][0]
        self.assertEqual((match["matched"], match["target_ratio"], match["other_ratio"]), (95, 95.0, 95.0))

    def test_same_numbers_different_weights_do_not_match(self):
        base = batch("A", count=100)
        self.upload("2026-05-01", base)
        changed = [(no, weight + 1) for no, weight in base]
        result = core.precheck_tickets(self.conn, "2026-05-02", changed)
        self.assertFalse(result["hit"])

    def test_all_matching_days_are_listed_with_counts(self):
        base = batch("A", count=100)
        self.upload("2026-05-01", base)
        self.upload("2026-05-02", base[:98] + batch("B", count=2))
        self.upload("2026-05-03", base[:97] + batch("C", count=3))
        result = core.precheck_tickets(self.conn, "2026-05-04", base)
        dates = [m["date"] for m in result["matches"]]
        self.assertEqual(dates, ["2026-05-01", "2026-05-02", "2026-05-03"])
        self.assertEqual(result["matches"][0]["matched"], 100)
        self.assertEqual(result["matches"][1]["matched"], 98)
        self.assertEqual(result["match_count"], 3)

    def test_holds_do_not_affect_comparison(self):
        base = batch("A", count=100)
        self.upload("2026-05-01", base)
        for ticket_no, _weight in base[:10]:
            core.hold_ticket(self.conn, "2026-05-01", ticket_no, "review")
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.assertTrue(result["hit"])
        self.assertEqual(result["matches"][0]["matched"], 100)

    def test_legacy_database_reports_participate(self):
        # Simulate rows written before day_reports existed.
        self.conn.execute("DELETE FROM day_reports")
        self.conn.execute("DELETE FROM day_write_order")
        base = batch("L", count=110)
        self.conn.executemany(
            "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
            [("2024-06-01", no, weight) for no, weight in base],
        )
        core.init_schema(self.conn)  # startup backfill
        result = core.precheck_tickets(self.conn, "2024-06-03", base)
        self.assertTrue(result["hit"])
        self.assertEqual(result["matches"][0]["date"], "2024-06-01")
        self.assertEqual(result["matches"][0]["other_count"], 110)

    def test_comparison_work_ignores_non_overlapping_neighbour_tickets(self):
        base = batch("A", count=100)
        self.upload("2026-05-01", base)
        target_map = dict(base)

        def vm_steps():
            counter = {"steps": 0}

            def progress():
                counter["steps"] += 1
                return 0

            self.conn.set_progress_handler(progress, 100)
            try:
                hits, inspected = core._compare_with_window(
                    self.conn, "2026-05-02", target_map, "2026-04-25", "2026-05-09"
                )
            finally:
                self.conn.set_progress_handler(None, 100)
            return hits, inspected, counter["steps"]

        # A candidate neighbor with 100 unrelated tickets: it is inspected
        # (at the floor) but cannot reach 95% of the incoming report.
        self.upload("2026-05-03", [(f"Z-{i:05d}", i % 100) for i in range(100)])
        small_hits, small_inspected, small_steps = vm_steps()

        # The same neighbor grows to 5000 unrelated tickets. Candidate
        # selection is metadata-based and the probe only looks up the incoming
        # keys, so both the inspected-day count and the SQLite work are flat.
        self.conn.executemany(
            "INSERT INTO tickets(slip_date, ticket_no, net_weight) VALUES (?, ?, ?)",
            [("2026-05-03", f"Y-{i:05d}", (i + 7) % 100) for i in range(4900)],
        )
        self.conn.execute(
            "UPDATE day_reports SET ticket_count = ticket_count + 4900 WHERE slip_date = ?",
            ("2026-05-03",),
        )
        large_hits, large_inspected, large_steps = vm_steps()

        self.assertEqual(small_inspected, large_inspected)
        self.assertEqual(small_steps, large_steps)
        self.assertEqual(small_hits, large_hits)
        self.assertEqual([m["date"] for m in large_hits], ["2026-05-01"])

        self.conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS precheck_incoming (
                ticket_no TEXT PRIMARY KEY, net_weight INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        plan = self.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COUNT(*) FROM precheck_incoming AS i
            WHERE EXISTS (
                SELECT 1 FROM tickets AS t
                WHERE t.slip_date = ? AND t.ticket_no = i.ticket_no
                  AND t.net_weight = i.net_weight
            )
            """,
            ("2026-05-01",),
        ).fetchall()
        text = "\n".join(row[3] for row in plan)
        self.assertIn("PRIMARY KEY", text.upper())
        self.assertNotIn("SCAN t", text.upper())


class CommitAndStaleTests(PrecheckBase):
    def test_hit_is_not_stored_until_confirmed(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.assertTrue(result["hit"])
        self.assertEqual(core.get_day_readback(self.conn, "2026-05-02")["tickets"], [])

        confirmed = core.confirm_precheck(self.conn, result["precheck_id"])
        self.assertEqual(confirmed["new_count"], len(base))
        readback = core.get_day_readback(self.conn, "2026-05-02")
        self.assertEqual(len(readback["tickets"]), len(base))
        rank = core.query_rank(self.conn, "2026-05-02", base[0][0])
        self.assertTrue(rank["found"])

    def test_no_hit_commits_directly(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-06-01", batch("Q"))
        self.assertFalse(result["hit"])
        summary = core.commit_reported_day(
            self.conn, "2026-06-01", batch("Q"), submission_seq=result["submission_seq"]
        )
        self.assertEqual(summary["new_count"], 120)
        self.assertEqual(len(core.get_day_readback(self.conn, "2026-06-01")["tickets"]), 120)

    def test_target_replacement_after_precheck_requires_recheck(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.upload("2026-05-02", batch("B", count=100))
        with self.assertRaises(core.PrecheckStale) as ctx:
            core.confirm_precheck(self.conn, result["precheck_id"])
        self.assertEqual(ctx.exception.stale_dates, ["2026-05-02"])

    def test_window_replacement_after_precheck_requires_recheck(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.upload("2026-05-05", [("X", 1), ("Y", 2)])
        with self.assertRaises(core.PrecheckStale) as ctx:
            core.confirm_precheck(self.conn, result["precheck_id"])
        self.assertEqual(ctx.exception.stale_dates, ["2026-05-05"])

    def test_outside_window_report_does_not_invalidate(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        # Nine days past the target is outside the seven-day window.
        self.upload("2026-05-11", [("P", 1)])
        confirmed = core.confirm_precheck(self.conn, result["precheck_id"])
        self.assertEqual(confirmed["date"], "2026-05-02")

    def test_reprecheck_after_invalidation_can_confirm(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        first = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.upload("2026-05-05", [("X", 1)])
        with self.assertRaises(core.PrecheckStale):
            core.confirm_precheck(self.conn, first["precheck_id"])

        second = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.assertNotEqual(second["precheck_id"], first["precheck_id"])
        core.confirm_precheck(self.conn, second["precheck_id"])
        self.assertEqual(len(core.get_day_readback(self.conn, "2026-05-02")["tickets"]), 120)

    def test_double_confirmation_is_refused(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        core.confirm_precheck(self.conn, result["precheck_id"])
        with self.assertRaises(core.ValidationError):
            core.confirm_precheck(self.conn, result["precheck_id"])

    def test_stale_confirmation_keeps_previous_report_and_subtotals(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        first = core.precheck_tickets(self.conn, "2026-05-02", base)
        self.upload("2026-05-03", batch("C"))
        with self.assertRaises(core.PrecheckStale):
            core.confirm_precheck(self.conn, first["precheck_id"])
        self.assertEqual(core.get_day_readback(self.conn, "2026-05-02")["tickets"], [])

    def test_failed_confirm_rolls_back_tickets_subtotals_and_evidence(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        original_before = core.query_rank(self.conn, "2026-05-01", base[0][0])

        original_apply = core.apply_delta

        def fail(conn, delta):
            raise RuntimeError("injected subtotal failure during confirm")

        core.apply_delta = fail
        try:
            with self.assertRaises(RuntimeError):
                core.confirm_precheck(self.conn, result["precheck_id"])
        finally:
            core.apply_delta = original_apply

        # Precheck evidence remains pending, target has no tickets, and the
        # neighbor's original report and subtotals are untouched.
        pending = core.list_pending_prechecks(self.conn)
        self.assertEqual([item["id"] for item in pending], [result["precheck_id"]])
        self.assertEqual(core.get_day_readback(self.conn, "2026-05-02")["tickets"], [])
        after = core.query_rank(self.conn, "2026-05-01", base[0][0])
        self.assertEqual(original_before["all"], after["all"])

    def test_confirmed_evidence_is_retained(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        core.confirm_precheck(self.conn, result["precheck_id"])
        row = self.conn.execute(
            "SELECT state, match_count, matches_json FROM precheck_reports WHERE id = ?",
            (result["precheck_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "confirmed")
        self.assertEqual(row["match_count"], 1)
        self.assertIn("2026-05-01", row["matches_json"])


class OrderingTests(PrecheckBase):
    def test_later_submission_wins_even_if_it_writes_first(self):
        first_seq = core.allocate_submission(self.conn)
        second_seq = core.allocate_submission(self.conn)
        core.commit_reported_day(
            self.conn, "2026-07-01", [("NEW", 5)], submission_seq=second_seq
        )
        with self.assertRaises(core.PrecheckStale):
            core.commit_reported_day(
                self.conn, "2026-07-01", [("OLD", 9)], submission_seq=first_seq
            )
        rows = core.get_day_readback(self.conn, "2026-07-01")["tickets"]
        self.assertEqual([row["ticket_no"] for row in rows], ["NEW"])

    def test_older_confirmation_cannot_overwrite_newer_report(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        newer = core.allocate_submission(self.conn)
        core.commit_reported_day(
            self.conn, "2026-05-02", [("NEWER", 1)], submission_seq=newer
        )
        with self.assertRaises(core.PrecheckStale):
            core.confirm_precheck(self.conn, result["precheck_id"])
        rows = core.get_day_readback(self.conn, "2026-05-02")["tickets"]
        self.assertEqual([row["ticket_no"] for row in rows], ["NEWER"])

    def test_window_outside_report_commits_while_precheck_pending(self):
        base = batch("A")
        self.upload("2026-05-01", base)
        result = core.precheck_tickets(self.conn, "2026-05-02", base)
        # Same calendar window, different date nine days out: outside ±7.
        summary = core.commit_reported_day(
            self.conn, "2026-05-20", [("OUT", 3)]
        )
        self.assertEqual(summary["new_count"], 1)
        self.assertEqual(len(core.get_day_readback(self.conn, "2026-05-20")["tickets"]), 1)
        core.confirm_precheck(self.conn, result["precheck_id"])


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="precheck-race-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_concurrent_reports_on_window_neighbour_and_outside(self):
        setup = core.connect(self.db_path)
        base = batch("A")
        core.replace_day_tickets(setup, "2026-05-01", base)
        precheck = core.precheck_tickets(setup, "2026-05-02", base)
        setup.close()

        errors = []

        def worker(day, tickets):
            conn = core.connect(self.db_path)
            try:
                core.commit_reported_day(conn, day, tickets)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append((day, exc))
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=("2026-05-05", [("W", 1)])),
            threading.Thread(target=worker, args=("2026-05-20", [("O", 1)])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        verify = core.connect(self.db_path)
        try:
            self.assertEqual(len(core.get_day_readback(verify, "2026-05-05")["tickets"]), 1)
            self.assertEqual(len(core.get_day_readback(verify, "2026-05-20")["tickets"]), 1)
            # The window report invalidated the pending precheck.
            with self.assertRaises(core.PrecheckStale):
                core.confirm_precheck(verify, precheck["precheck_id"])
        finally:
            verify.close()

    def test_concurrent_same_day_submissions_keep_the_later_one(self):
        setup = core.connect(self.db_path)
        core.replace_day_tickets(setup, "2026-05-01", batch("A"))
        setup.close()

        barrier = threading.Barrier(2)
        outcomes = []

        def worker(label):
            conn = core.connect(self.db_path)
            try:
                seq = core.allocate_submission(conn)
                barrier.wait(timeout=5)
                try:
                    core.commit_reported_day(
                        conn, "2026-05-04", [(label, 10)], submission_seq=seq
                    )
                    outcomes.append((label, seq, "ok"))
                except core.PrecheckStale:
                    outcomes.append((label, seq, "stale"))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(f"T{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        verify = core.connect(self.db_path)
        try:
            rows = core.get_day_readback(verify, "2026-05-04")["tickets"]
            self.assertEqual(len(rows), 1)
            winner = rows[0]["ticket_no"]
            winner_seq = next(seq for label, seq, _ in outcomes if label == winner)
            loser = next(outcome for outcome in outcomes if outcome[0] != winner)
            self.assertEqual(loser[2], "stale")
            self.assertGreater(winner_seq, loser[1])
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
