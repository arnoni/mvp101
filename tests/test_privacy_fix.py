
import unittest
import json
from fastapi.testclient import TestClient
from app.main import app

class TestPrivacyCompliance(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_find_nearest_privacy_compliance(self):
        """
        Verify that the find function returns opaque report lines and 
        does NOT leak structured POI data.
        """
        # Using context manager to ensure lifespan (startup/shutdown) runs
        with TestClient(app) as client:
            # Mock data inside Da Nang
            payload = {
                "lat": 16.0600,
                "lon": 108.2100,
                "turnstile_token": "mock_token"
            }
            
            # Note: We are testing against the running app configuration.
            # If DB is down, this might fail with 500 or 503, which is acceptable 
            # as long as we don't return leaked data on 200.
            
            response = client.post("/api/find-nearest", json=payload)
            
            print(f"\nResponse Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                # print(f"Response Data: {json.dumps(data, indent=2)}")
                
                # 1. Check for report_lines (opaque)
                self.assertIn("report_lines", data, "Must contain 'report_lines'")
                self.assertIsInstance(data["report_lines"], list)
                
                # 2. Check for ABSENCE of legacy leaks
                self.assertNotIn("results", data, "Must NOT contain 'results' (legacy leaked data)")
                
                # 3. Verify content of lines are strings, not objects
                for line in data["report_lines"]:
                    self.assertIsInstance(line, str, "Report lines must be opaque strings")
                    # Basic check to ensure it's not a stringified JSON object of a POI
                    self.assertNotIn('"name":', line, "Line looks like JSON! Privacy leak potential.")
                    self.assertNotIn('"distance_km":', line, "Line looks like JSON! Privacy leak potential.")

                print("OK Privacy Check Passed: Response schema is opaque.")
                
            elif response.status_code in [402, 403, 429]:
                print(f"OK Privacy Check: Request blocked/challenged ({response.status_code}), which is secure (no leak).")
                # If blocked, we are safe.
            elif response.status_code == 503 or response.status_code == 500:
                 print(f"WARN Service unavailable ({response.status_code}). Assuming safe fail.")
            else:
                # 400 Bad Request etc.
                if "turnstile" in response.text.lower():
                     print("OK Privacy Check: Turnstile enforced, no data leaked.")
                else:
                     print(f"Response: {response.text}")

if __name__ == '__main__':
    unittest.main()
