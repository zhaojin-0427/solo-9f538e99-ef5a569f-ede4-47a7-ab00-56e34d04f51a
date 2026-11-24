import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import flask  # noqa: F401
    from app import create_app
except ImportError:  # pragma: no cover
    flask = None


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class FlaskPageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-flask-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload_csv(self, content="单号,净重\nA,100\nB,90\nC,90\nD,80\n", day="2026-05-01"):
        data = {
            "date": day,
            "file": (io.BytesIO(content.encode("utf-8")), "tickets.csv"),
        }
        return self.client.post("/upload", data=data, content_type="multipart/form-data")

    def test_health_page_upload_query_and_tie_display(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = self.upload_csv()
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-05-01", resp.headers["Location"])

        resp = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("第 2-3 名", html)
        match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', html)
        self.assertIsNotNone(match)
        accessed = int(match.group(1))
        self.assertLessEqual(accessed, 576)
        self.assertIn("576", html)
        self.assertIn("小计键访问数", html)

        # A balanced-tree worst interval plus weight zero reaches the proven
        # 540-key upper bound for two calibers.
        self.client.post(
            "/upload",
            data={"date": "2026-06-01", "csv_text": "X,0"},
        )
        worst = self.client.get(
            "/query?date=2026-06-01&ticket_no=X&start=2026-01-02&end=2026-12-30"
        ).get_data(as_text=True)
        worst_match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', worst)
        self.assertEqual(int(worst_match.group(1)), 540)

        js = self.client.get(
            "/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_json()
        self.assertTrue(js["ok"])
        self.assertTrue(js["result"]["meter"]["within_limit"])
        self.assertLessEqual(js["result"]["meter"]["accessed"], 576)

    def test_hold_shows_held_note_and_blank_rank_then_release(self):
        self.upload_csv()
        resp = self.client.post(
            "/hold", data={"date": "2026-05-01", "ticket_no": "B", "reason": "tie check"}
        )
        self.assertEqual(resp.status_code, 303)

        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertIn("目标单已暂扣", html)
        self.assertIn("留空", html)
        # All-caliber rank remains visible.
        self.assertIn("第 2-3 名", html)

        resp = self.client.post("/release", data={"date": "2026-05-01", "ticket_no": "B"})
        self.assertEqual(resp.status_code, 303)
        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertNotIn("目标单已暂扣；未暂扣口径只显示总数", html)

    def test_reupload_preserves_hold(self):
        self.upload_csv()
        self.client.post("/hold", data={"date": "2026-05-01", "ticket_no": "A"})
        # Replacement CSV omits A.
        self.upload_csv("单号,净重\nB,95\nC,90\nD,80\n", "2026-05-01")
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn("原单暂缺但仍有效的暂扣记录", day_html)
        self.assertIn(">A<", day_html)

    def test_invalid_upload_does_not_replace_day(self):
        self.upload_csv()
        bad = "单号,净重\nX,10\nX,11\nY,70000\n"
        resp = self.upload_csv(bad)
        # HTML error path redirects back and flashes; follow it.
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)
        self.assertIn(">B<", day_html)


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class PrecheckPageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-precheck-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    @staticmethod
    def csv_text(prefix, count=120):
        rows = ["单号,净重"]
        rows += [
            f"{prefix}-{i:04d},{(i * 131) % 65536}" for i in range(count)
        ]
        return "\n".join(rows)

    def upload(self, day, prefix="A", count=120):
        return self.client.post(
            "/upload", data={"date": day, "csv_text": self.csv_text(prefix, count)}
        )

    def test_hit_returns_202_and_is_not_stored(self):
        self.assertEqual(self.upload("2026-05-01").status_code, 303)
        resp = self.client.post(
            "/api/upload",
            data={"date": "2026-05-02", "csv_text": self.csv_text("A")},
        )
        self.assertEqual(resp.status_code, 202)
        payload = resp.get_json()
        self.assertFalse(payload["stored"])
        self.assertTrue(payload["precheck"]["hit"])
        self.assertEqual(payload["precheck"]["matches"][0]["date"], "2026-05-01")
        day_html = self.client.get("/day?date=2026-05-02").get_data(as_text=True)
        self.assertIn("当天没有秤单", day_html)

    def test_hit_page_lists_all_days_and_ratios(self):
        self.upload("2026-05-01")
        self.upload("2026-05-03", prefix="A", count=118)
        resp = self.upload("2026-05-04")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/precheck/", resp.headers["Location"])
        html = self.client.get(resp.headers["Location"]).get_data(as_text=True)
        self.assertIn("2026-05-01", html)
        self.assertIn("2026-05-03", html)
        self.assertIn("单号与净重均相同", html)
        self.assertIn("尚未入库", html)

    def test_confirm_page_then_stored_and_dashboard_cleared(self):
        self.upload("2026-05-01")
        resp = self.upload("2026-05-02")
        precheck_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        dash = self.client.get("/").get_data(as_text=True)
        self.assertIn("待质控员核对的命中预检", dash)

        resp = self.client.post("/confirm", data={"precheck_id": precheck_id})
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-05-02", resp.headers["Location"])
        day_html = self.client.get("/day?date=2026-05-02").get_data(as_text=True)
        self.assertIn("A-0000", day_html)
        dash = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("待质控员核对的命中预检", dash)

    def test_stale_confirm_returns_409_and_old_report_survives(self):
        self.upload("2026-05-01")
        resp = self.upload("2026-05-02")
        precheck_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        self.upload("2026-05-05", prefix="Z", count=2)

        resp = self.client.post(
            "/api/confirm", data={"precheck_id": precheck_id}
        )
        self.assertEqual(resp.status_code, 409)
        payload = resp.get_json()
        self.assertIn("2026-05-05", payload["stale_dates"])
        self.assertIn("必须重新核对", payload["error"])
        day_html = self.client.get("/day?date=2026-05-02").get_data(as_text=True)
        self.assertIn("当天没有秤单", day_html)
        pending = self.client.get("/api/precheck").get_json()["pending"]
        self.assertEqual([item["id"] for item in pending], [precheck_id])
        self.assertTrue(pending[0]["stale_dates"])

    def test_reprecheck_after_stale_lists_only_new_pending(self):
        self.upload("2026-05-01")
        resp = self.upload("2026-05-02")
        first_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        self.upload("2026-05-05", prefix="Z", count=2)
        resp = self.upload("2026-05-02")
        second_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        self.assertNotEqual(first_id, second_id)
        pending = self.client.get("/api/precheck?date=2026-05-02").get_json()["pending"]
        self.assertEqual([item["id"] for item in pending], [second_id])
        resp = self.client.post("/api/confirm", data={"precheck_id": second_id})
        self.assertEqual(resp.status_code, 200)

    def test_no_hit_upload_still_commits_normally(self):
        resp = self.upload("2026-09-01", prefix="Q")
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-09-01", resp.headers["Location"])
        day_html = self.client.get("/day?date=2026-09-01").get_data(as_text=True)
        self.assertIn("Q-0000", day_html)

    def test_hold_does_not_change_precheck_verdict(self):
        self.upload("2026-05-01")
        self.client.post("/hold", data={"date": "2026-05-01", "ticket_no": "A-0000"})
        resp = self.client.post(
            "/api/upload",
            data={"date": "2026-05-02", "csv_text": self.csv_text("A")},
        )
        self.assertEqual(resp.status_code, 202)
        match = resp.get_json()["precheck"]["matches"][0]
        self.assertEqual((match["matched"], match["other_count"]), (120, 120))


if __name__ == "__main__":
    unittest.main()
