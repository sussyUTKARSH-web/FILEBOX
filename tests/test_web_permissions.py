"""MySQL-backed end-to-end checks. Uses disposable example.invalid accounts."""
import io
import re
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import database
import web_app


class WebPermissionsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            database.initialize()
        except Exception as exc:
            raise unittest.SkipTest(f"Local MySQL is unavailable: {exc}")
        web_app.app.config.update(TESTING=True)

    def setUp(self):
        suffix = uuid.uuid4().hex[:12]
        self.email_a = f"filebox-a-{suffix}@example.invalid"
        self.email_b = f"filebox-b-{suffix}@example.invalid"
        self.user_ids = []
        self.client_a = web_app.app.test_client()
        self.client_b = web_app.app.test_client()
        self._register(self.client_a, f"TesterA{suffix}", self.email_a)
        self._register(self.client_b, f"TesterB{suffix}", self.email_b)
        with self.client_a.session_transaction() as session:
            self.user_a = int(session["user_id"])
        with self.client_b.session_transaction() as session:
            self.user_b = int(session["user_id"])
        self.user_ids = [self.user_a, self.user_b]
        token = self._csrf(self.client_a, "/dashboard")
        response = self.client_a.post(
            "/upload",
            data={"csrf_token": token, "parent_folder_id": "",
                  "file": (io.BytesIO(b"first version\n"), "team-notes.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        conn = database.connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT file_id,relative_path FROM files WHERE owner_id=%s AND file_name=%s",
                           (self.user_a, "team-notes.txt"))
            self.file = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()
        self.assertIsNotNone(self.file)
        self.file_id = int(self.file["file_id"])

    def tearDown(self):
        if not self.user_ids:
            return
        web_app._audit_queue.join()
        conn = database.connect()
        try:
            cursor = conn.cursor()
            marks = ",".join(["%s"] * len(self.user_ids))
            cursor.execute(f"DELETE FROM activity_log WHERE user_id IN ({marks})", tuple(self.user_ids))
            cursor.execute(f"DELETE FROM users WHERE user_id IN ({marks})", tuple(self.user_ids))
            conn.commit()
            cursor.close()
        finally:
            conn.close()
        for user_id in self.user_ids:
            shutil.rmtree(web_app.STORAGE_ROOT / str(user_id), ignore_errors=True)

    def _csrf(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match, "CSRF token is present in rendered forms")
        return match.group(1).decode()

    def _register(self, client, username, email):
        token = self._csrf(client, "/register")
        response = client.post("/register", data={
            "csrf_token": token,
            "username": username,
            "email": email,
            "password": "test-password-FileBox-2026",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn(b"Account created. You\xe2\x80\x99re signed in.", response.data)
        with client.session_transaction() as session:
            self.assertIn("user_id", session)
            self.user_ids.append(int(session["user_id"]))

    def _share_with_b(self, permission, allow_download=False):
        token = self._csrf(self.client_a, f"/files/{self.file_id}/share")
        response = self.client_a.post(f"/files/{self.file_id}/share", data={
            "csrf_token": token, "user_query": self.email_b,
        })
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="user_id" value="(\d+)"', response.data)
        self.assertIsNotNone(match)
        token = self._csrf(self.client_a, f"/files/{self.file_id}/share")
        form = {"csrf_token": token, "user_id": match.group(1).decode(), "permission": permission}
        if allow_download:
            form["allow_download"] = "on"
        response = self.client_a.post(f"/files/{self.file_id}/share/user", data=form)
        self.assertEqual(response.status_code, 302)

    def test_private_read_write_revoke_and_authenticated_access(self):
        # Private files are denied before an individual permission exists.
        self.assertEqual(self.client_b.get(f"/files/{self.file_id}").status_code, 403)

        # READ permits preview, but the owner-controlled download setting applies.
        self._share_with_b("READ", allow_download=False)
        preview = self.client_b.get(f"/files/{self.file_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b'class="text-content"', preview.data)
        self.assertIn(b"first version", preview.data)
        preview.close()
        self.assertEqual(self.client_b.get(f"/files/{self.file_id}/download").status_code, 403)
        token = self._csrf(self.client_b, "/dashboard")
        response = self.client_b.post(f"/files/{self.file_id}/edit", data={
            "csrf_token": token, "content": "reader should not edit this\n",
        })
        self.assertEqual(response.status_code, 403)

        # Change the individual grant to WRITE; text edits update the managed file.
        self._share_with_b("WRITE", allow_download=True)
        preview = self.client_b.get(f"/files/{self.file_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b'class="text-editor"', preview.data)
        self.assertIn(b"first version", preview.data)
        edit_token = re.search(rb'name="edit_token" value="([^"]+)"', preview.data).group(1).decode()
        preview.close()
        token = self._csrf(self.client_b, f"/files/{self.file_id}")
        response = self.client_b.post(f"/files/{self.file_id}/edit", data={
            "csrf_token": token, "edit_token": edit_token,
            "priority": "2", "content": "edited by reader-writer test\n",
        })
        self.assertEqual(response.status_code, 302)
        path = web_app.safe_storage_path(web_app.STORAGE_ROOT, self.user_a, self.file["relative_path"])
        self.assertEqual(path.read_text(encoding="utf-8"), "edited by reader-writer test\n")
        preview = self.client_b.get(f"/files/{self.file_id}")
        self.assertIn(b"edited by reader-writer test", preview.data)
        preview.close()

        # Revocation immediately blocks the next request.
        token = self._csrf(self.client_a, f"/files/{self.file_id}/share")
        response = self.client_a.post(f"/files/{self.file_id}/share/revoke/{self.user_b}",
                                      data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client_b.get(f"/files/{self.file_id}").status_code, 403)

        # General access applies only to authenticated FileBox users.
        token = self._csrf(self.client_a, f"/files/{self.file_id}/share")
        response = self.client_a.post(f"/files/{self.file_id}/share/access-mode", data={
            "csrf_token": token, "access_mode": "AUTH_WRITE", "allow_download": "on",
        })
        self.assertEqual(response.status_code, 302)
        preview = self.client_b.get(f"/files/{self.file_id}")
        self.assertEqual(preview.status_code, 200)
        preview.close()
        self.assertEqual(self.client_b.get(f"/files/{self.file_id}/download").status_code, 302)
        self.assertEqual(web_app.app.test_client().get(f"/files/{self.file_id}").status_code, 302)

        # Return to private before cleanup.
        token = self._csrf(self.client_a, f"/files/{self.file_id}/share")
        self.client_a.post(f"/files/{self.file_id}/share/access-mode", data={
            "csrf_token": token, "access_mode": "PRIVATE",
        })

    def test_one_writer_holds_exclusive_edit_lease_until_leaving(self):
        self._share_with_b("WRITE", allow_download=True)

        owner_page = self.client_a.get(f"/files/{self.file_id}")
        self.assertEqual(owner_page.status_code, 200)
        match = re.search(rb'name="edit_token" value="([^"]+)"', owner_page.data)
        self.assertIsNotNone(match, "the first writer receives an edit lease token")
        owner_token = match.group(1).decode()
        self.assertIn(b"Exclusive edit lock active for you", owner_page.data)

        other_page = self.client_b.get(f"/files/{self.file_id}")
        self.assertEqual(other_page.status_code, 200)
        self.assertIn(b"TesterA", other_page.data)
        self.assertIn(b"is editing this file", other_page.data)
        self.assertIn(b'class="text-content"', other_page.data)
        self.assertNotIn(b'class="text-editor"', other_page.data)

        token_b = self._csrf(self.client_b, "/dashboard")
        rejected = self.client_b.post(f"/files/{self.file_id}/edit", data={
            "csrf_token": token_b, "edit_token": "not-the-lock-owner", "content": "overlap\n",
        })
        self.assertEqual(rejected.status_code, 409)

        token_a = self._csrf(self.client_a, "/dashboard")
        saved = self.client_a.post(f"/files/{self.file_id}/edit", data={
            "csrf_token": token_a, "edit_token": owner_token,
            "content": "saved under exclusive lease\n",
        })
        self.assertEqual(saved.status_code, 302)

        # The other user can claim the editor after the first writer leaves.
        release_token = self._csrf(self.client_a, "/dashboard")
        released = self.client_a.post(f"/files/{self.file_id}/edit-lock/release", data={
            "csrf_token": release_token, "edit_token": owner_token,
        })
        self.assertEqual(released.status_code, 200)
        next_editor_page = self.client_b.get(f"/files/{self.file_id}")
        self.assertIn(b'class="text-editor"', next_editor_page.data)
        next_editor_page.close()

    def test_stale_form_explains_separate_browser_profiles(self):
        response = web_app.app.test_client().post("/login", data={"csrf_token": "stale-token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"one account per browser profile", response.data)

    def test_folder_move_replace_download_and_delete(self):
        token = self._csrf(self.client_a, "/dashboard")
        response = self.client_a.post("/folders", data={
            "csrf_token": token, "name": "Archive", "parent_folder_id": "",
        })
        self.assertEqual(response.status_code, 302)
        conn = database.connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT folder_id,relative_path FROM folders WHERE owner_id=%s AND folder_name=%s",
                           (self.user_a, "Archive"))
            folder = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()
        self.assertIsNotNone(folder)

        token = self._csrf(self.client_a, "/dashboard")
        response = self.client_a.post(f"/folders/{folder['folder_id']}/rename", data={
            "csrf_token": token, "name": "Saved",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue((web_app.STORAGE_ROOT / str(self.user_a) / "Saved").is_dir())

        token = self._csrf(self.client_a, "/dashboard")
        response = self.client_a.post(f"/files/{self.file_id}/move", data={
            "csrf_token": token, "name": "renamed-notes.txt", "folder_id": str(folder["folder_id"]),
        })
        self.assertEqual(response.status_code, 302)
        moved_relative = "Saved/renamed-notes.txt"
        self.assertTrue((web_app.STORAGE_ROOT / str(self.user_a) / moved_relative).is_file())

        token = self._csrf(self.client_a, f"/files/{self.file_id}")
        response = self.client_a.post(f"/files/{self.file_id}/replace", data={
            "csrf_token": token,
            "file": (io.BytesIO(b"replacement bytes\n"), "replacement.txt"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        moved_path = web_app.safe_storage_path(web_app.STORAGE_ROOT, self.user_a, moved_relative)
        self.assertEqual(moved_path.read_bytes(), b"replacement bytes\n")

        download = self.client_a.get(f"/files/{self.file_id}/download", follow_redirects=True,
                                     buffered=False)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.iter_encoded()), b"replacement bytes\n")
        download.close()

        token = self._csrf(self.client_a, "/dashboard")
        response = self.client_a.post(f"/files/{self.file_id}/delete", data={
            "csrf_token": token,
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(moved_path.exists())
        self.assertEqual(self.client_a.get(f"/files/{self.file_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
