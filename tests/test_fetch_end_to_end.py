"""A real fetch in the real window against a fake Arctic Shift server: rows appear one by one and fade in,
only the requested posts get comments, and nothing is left running afterwards."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment import arctic_shift  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

T0 = int(time.time()) - 3 * 86400
POSTS = [{"id": f"x{i}", "subreddit": "Python", "title": f"post {i} great", "selftext": "", "author": "a",
          "created_utc": T0 + i * 600, "permalink": f"/r/Python/comments/x{i}/t/", "url": "", "score": 1,
          "num_comments": 1, "retrieved_on": T0 + i * 600 + 5} for i in range(30)]


@pytest.fixture
def server(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            seen.append((url.path, q))
            time.sleep(0.05)
            status, data = 200, []
            if url.path == "/api/posts/search":
                limit = int(q.get("limit", 25))
                if not 1 <= limit <= 100:  # the API's own bounds
                    status = 400
                else:
                    lo, hi = float(q.get("after", 0)), float(q.get("before", 1e12))
                    data = [p for p in POSTS if lo < p["created_utc"] < hi][:limit]
            elif url.path == "/api/posts/ids":
                ids = q["ids"].split(",")
                if len(ids) > 500:
                    status = 400
                else:
                    data = [p for p in POSTS if p["id"] in ids]
            elif url.path == "/api/comments/tree":
                pid = q["link_id"]
                data = [{"kind": "t1", "data": {"id": f"c{pid}{j}", "body": f"lovely {j}", "author": "b",
                         "created_utc": T0 + 10, "parent_id": "t3_" + pid, "score": 1, "retrieved_on": T0 + 20,
                         "replies": ""}} for j in range(3)]
            if "fields" in q:  # like the API: only the named fields, and no _meta
                keep = q["fields"].split(",")
                data = [{k: d[k] for k in keep if k in d} for d in data]
            body = json.dumps({"data": data} if status == 200 else {"error": "bad request"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(arctic_shift, "BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    yield seen
    srv.shutdown()


def test_rows_arrive_one_by_one_fading_in_and_only_for_the_fetched_posts(tmp_path, server):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    db = tmp_path / "e.db"
    s = Store(db)
    for i in range(10):  # saved earlier, comments never requested
        s.save_post(Post(f"old{i}", "Python", "old", "", "a", 1, None, 1, T0 + 100 + i, "p", ""), 0.0, "neutral")
    s.commit()
    s.close()
    w = mw.MainWindow(str(db))
    try:
        assert not hasattr(w, "live_btn")
        w.subreddit.setText("Python")
        w.limit.setValue(6)
        fg = Qt.ItemDataRole.ForegroundRole
        steps, fading = [], 0
        w._start_fetch()
        deadline = time.time() + 30
        while w.fetch_job.busy and time.time() < deadline:
            app.processEvents()
            pm, cm = w.posts_tab.model, w.comments_tab.model
            state = (pm.rowCount(), cm.rowCount())
            if not steps or steps[-1] != state:
                steps.append(state)
            fading = max(fading, sum(pm.data(pm.index(i, 0), fg) is not None for i in range(pm.rowCount())))
            time.sleep(0.005)
        for _ in range(100):
            app.processEvents()
            time.sleep(0.01)
        assert not w.fetch_job.busy
        posts_seen = sorted({p for p, _ in steps})
        assert posts_seen[-1] == 16 and len(posts_seen) >= 4  # grew in several steps, not one jump
        assert len({c for _, c in steps}) >= 4  # so did the comments
        assert fading >= 1  # rows were mid-fade while they arrived
        comment_requests = [q["link_id"] for path, q in server if path == "/api/comments/tree"]
        assert len(comment_requests) == 6 and not any(i.startswith("old") for i in comment_requests)
        assert not (w._live.isActive() or w._drip.isActive() or w.posts_tab.model._fade.isActive())
        assert len(w.store.posts("Python")) == 16 and len(w.store.comments("Python")) == 18
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)
