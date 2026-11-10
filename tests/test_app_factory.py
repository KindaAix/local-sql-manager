import unittest

from local_sql_manager.app import create_app


class TestAppFactory(unittest.TestCase):
    def test_healthz(self):
        client = create_app().test_client()
        r = client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(as_text=True), "ok")


if __name__ == "__main__":
    unittest.main()
