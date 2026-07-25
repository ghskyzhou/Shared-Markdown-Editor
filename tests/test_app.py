import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main


class MarkdownEditorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "test.db"
        main.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            DATABASE=str(database_path),
        )
        main.document_states.clear()
        main.initialize_database(migrate_legacy=False)
        self.document_id = main.create_document("测试文档")

    def tearDown(self):
        main.document_states.clear()
        self.temporary_directory.cleanup()

    def login(self, client):
        response = client.post(
            "/login",
            data={"password": main.LOGIN_PASSWORD},
        )
        self.assertEqual(response.status_code, 302)
        return response

    def joined_socket(self, http_client, document_id=None):
        socket_client = main.socketio.test_client(
            main.app, flask_test_client=http_client
        )
        self.assertTrue(socket_client.is_connected())
        socket_client.emit(
            "document:join",
            {"document_id": document_id or self.document_id},
        )
        received = socket_client.get_received()
        self.assertEqual(received[-1]["name"], "document:init")
        return socket_client

    def test_unauthenticated_page_and_socket_are_rejected(self):
        client = main.app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

        socket_client = main.socketio.test_client(
            main.app, flask_test_client=client
        )
        self.assertFalse(socket_client.is_connected())

    def test_login_is_permanent_and_wrong_key_is_rejected(self):
        client = main.app.test_client()
        wrong = client.post("/login", data={"password": "wrong"})
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("密钥不正确".encode(), wrong.data)

        response = self.login(client)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Expires=", cookie)
        self.assertEqual(client.get("/").status_code, 200)

    def test_home_lists_documents_and_can_create_a_new_one(self):
        client = main.app.test_client()
        self.login(client)

        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("共享 Markdown 编辑器".encode(), home.data)
        self.assertIn("测试文档".encode(), home.data)
        self.assertIn("上次编辑时间".encode(), home.data)

        created = client.post("/documents")
        self.assertEqual(created.status_code, 302)
        self.assertRegex(created.location, r"/documents/\d+$")
        self.assertEqual(len(main.list_document_records()), 2)

    def test_document_title_can_be_changed(self):
        client = main.app.test_client()
        self.login(client)
        response = client.post(
            f"/documents/{self.document_id}/title",
            json={"title": "  新的   标题  "},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["title"], "新的 标题")
        self.assertEqual(
            main.get_document_record(self.document_id)["title"],
            "新的 标题",
        )

    def test_document_can_be_deleted_from_home(self):
        client = main.app.test_client()
        self.login(client)
        main.get_document_state(self.document_id)

        response = client.post(
            f"/documents/{self.document_id}/delete",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/")
        self.assertIsNone(main.get_document_record(self.document_id))
        self.assertNotIn(self.document_id, main.document_states)

    def test_concurrent_insertions_are_merged_and_broadcast(self):
        with main.connect_database() as connection:
            connection.execute(
                "UPDATE documents SET content = 'abc' WHERE id = ?",
                (self.document_id,),
            )
        main.document_states.clear()

        first_http = main.app.test_client()
        second_http = main.app.test_client()
        self.login(first_http)
        self.login(second_http)
        first = self.joined_socket(first_http)
        second = self.joined_socket(second_http)

        first.emit(
            "document:update",
            {
                "document_id": self.document_id,
                "content": "Xabc",
                "base_version": 0,
            },
        )
        first_state = [
            event
            for event in first.get_received()
            if event["name"] == "document:state"
        ][-1]["args"][0]
        self.assertEqual(first_state["content"], "Xabc")
        second.get_received()

        second.emit(
            "document:update",
            {
                "document_id": self.document_id,
                "content": "abcY",
                "base_version": 0,
            },
        )
        second_state = [
            event
            for event in second.get_received()
            if event["name"] == "document:state"
        ][-1]["args"][0]
        self.assertEqual(second_state["content"], "XabcY")
        self.assertEqual(
            main.get_document_state(self.document_id).snapshot(),
            ("XabcY", 2),
        )
        self.assertEqual(
            main.get_document_record(self.document_id)["content"],
            "XabcY",
        )

        first.disconnect()
        second.disconnect()

    def test_documents_use_separate_socket_rooms(self):
        other_document_id = main.create_document("另一篇")
        first_http = main.app.test_client()
        second_http = main.app.test_client()
        self.login(first_http)
        self.login(second_http)
        first = self.joined_socket(first_http, self.document_id)
        second = self.joined_socket(second_http, other_document_id)

        first.emit(
            "document:update",
            {
                "document_id": self.document_id,
                "content": "only first",
                "base_version": 0,
            },
        )
        self.assertFalse(
            any(
                event["name"] == "document:state"
                for event in second.get_received()
            )
        )

        first.disconnect()
        second.disconnect()

    def test_editor_has_offline_lock_and_no_manual_save_controls(self):
        client = main.app.test_client()
        self.login(client)
        response = client.get(f"/documents/{self.document_id}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("断网状态禁止编辑", page)
        self.assertNotIn('id="saveBtn"', page)
        self.assertNotIn('id="toggleEdit"', page)
        self.assertEqual(
            client.post(f"/documents/{self.document_id}/save").status_code,
            404,
        )

    def test_deletion_preserves_a_concurrent_insertion_inside_it(self):
        document = main.CollaborativeDocument(self.document_id, "abcdef")
        document.merge("abcXdef", 0)
        merged = document.merge("aef", 0)
        self.assertEqual(merged["content"], "aXef")

    def test_concurrent_chinese_insertions_are_preserved(self):
        document = main.CollaborativeDocument(self.document_id, "")
        document.merge("你好", 0)
        merged = document.merge("世界", 0)
        self.assertEqual(merged["content"], "你好世界")

    def test_home_time_is_formatted_in_local_timezone(self):
        shanghai_timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")
        now = datetime(2026, 7, 24, 20, 0, tzinfo=shanghai_timezone)
        group, value = main.display_document_time(
            "2026-07-24T11:32:00+00:00",
            now=now,
        )
        self.assertEqual((group, value), ("今天", "19:32"))


if __name__ == "__main__":
    unittest.main()
