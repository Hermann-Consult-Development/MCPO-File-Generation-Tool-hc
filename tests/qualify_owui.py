"""Explicit isolated-server qualification, never part of automatic unit tests.

Requires a private synthetic fixture-auth.json and the approved disposable OWUI.
No model/provider inference. Creates/reuses one HC security-test identity.
"""
import json
import os
import time
from pathlib import Path
import requests
from test_secure_exports import CASES, validate_artifact

WEBUI = os.environ.get("QUALIFY_OWUI_URL", "http://webui:8080")
MCPO = os.environ.get("QUALIFY_MCPO_URL", "http://127.0.0.1:8000/file_export")
fixture = json.loads(Path(os.environ["QUALIFY_AUTH_FILE"]).read_text())


def call(method, url, **kwargs):
    return requests.request(method, url, timeout=(5, 60), allow_redirects=False, **kwargs)


def signin(email):
    r = call("POST", WEBUI + "/api/v1/auths/signin", json={"email": email, "password": fixture["password"]})
    assert r.status_code == 200, f"sign-in failed HTTP {r.status_code}"
    return {"Authorization": "Bearer " + r.json()["token"]}


# A newly started MCPO needs to finish its stdio tool handshake first.
for attempt in range(20):
    try:
        if call("GET", MCPO + "/openapi.json").status_code == 200:
            break
    except requests.RequestException:
        pass
    time.sleep(0.5)
else:
    raise RuntimeError("MCPO did not become ready within the qualification startup window")

owner = signin(fixture["user_email"])
admin = signin(fixture["admin_email"])
other_email = "hc-file-security-test@example.invalid"
r = call("POST", WEBUI + "/api/v1/auths/add", headers=admin,
         json={"name": "HC file security test", "email": other_email, "password": fixture["password"], "role": "user"})
assert r.status_code in (200, 400), f"HC test identity creation HTTP {r.status_code}"
other = signin(other_email)
assert other != owner

schema = call("GET", MCPO + "/openapi.json")
assert schema.status_code == 200
results = []
for case in CASES:
    response = call("POST", MCPO + "/create_file", headers=owner, json={"data": case})
    assert response.status_code == 200, f"{case['format']} tool HTTP {response.status_code}"
    data = response.json()
    assert data.get("success") is True, f"{case['format']} export failed; response keys {list(data)}"
    link = data["url"]
    assert link.startswith("/api/v1/files/") and link.endswith("/content")
    owned = call("GET", WEBUI + link, headers=owner)
    assert owned.status_code == 200, f"owner HTTP {owned.status_code}"
    validate_artifact(case["format"], owned.content)
    foreign = call("GET", WEBUI + link, headers=other)
    anonymous = call("GET", WEBUI + link)
    assert foreign.status_code in (401, 403, 404), f"other user received HTTP {foreign.status_code}"
    assert anonymous.status_code in (401, 403, 404), f"anonymous received HTTP {anonymous.status_code}"
    results.append({"format": case["format"], "owner": owned.status_code,
                    "other_user": foreign.status_code, "anonymous": anonymous.status_code,
                    "bytes": len(owned.content)})

unauth = call("POST", MCPO + "/create_file", json={"data": CASES[0]})
assert unauth.status_code == 200 and unauth.json().get("success") is False
print(json.dumps({"five_formats": results, "no_auth_generation": "denied", "provider_calls": 0}))
