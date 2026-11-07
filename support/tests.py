from django.test import SimpleTestCase
from django.urls import reverse


class HealthTests(SimpleTestCase):
    def test_health_returns_plain_text_ok(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"OK")
        self.assertEqual(response.headers["Content-Type"], "text/plain")

    def test_health_rejects_post(self):
        response = self.client.post(reverse("health"))

        self.assertEqual(response.status_code, 405)
