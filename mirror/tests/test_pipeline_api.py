import asyncio, io, time
import httpx, pytest
from fastapi.testclient import TestClient
from mirror.app import main, pipeline, security
from mirror.app.providers import Candidate, Provider, ProviderError
from .helpers import scene, to_bytes

QUERY = scene(10)
IMAGES = {
    "https://a.example/full.jpg": to_bytes(QUERY.resize((400, 300))),
    "https://b.example/crop.jpg": to_bytes(QUERY.crop((80, 60, 520, 400))),
    "https://c.example/other.jpg": to_bytes(scene(77)),
}

class FakeProvider(Provider):
    name, label, key_env = "fake", "Fake (test only)", "FAKE"
    def configured(self): return True
    async def search(self, client, inp):
        return [
            Candidate("fake", "exact", "https://www.a.example/post?utm_source=x", "Post A", "https://a.example/full.jpg"),
            Candidate("fake", "exact", "https://a.example/post", None, "https://a.example/full.jpg"),  # duplicate
            Candidate("fake", "partial", "https://news.b.example/2024/05/story", "Story B", "https://b.example/crop.jpg"),
            Candidate("fake", "similar", None, None, "https://c.example/other.jpg"),
            Candidate("fake", "similar", "https://d.example/x", "D", "https://d.example/missing.jpg"),
        ]

class FailingProvider(Provider):
    name, label, key_env = "flaky", "Flaky", "FLAKY"
    calls = 0
    def configured(self): return True
    async def search(self, client, inp):
        FailingProvider.calls += 1
        raise ProviderError("flaky: boom", retryable=False)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pipeline, "ALL_PROVIDERS", [FakeProvider(), FailingProvider()])
    monkeypatch.setattr(main, "ALL_PROVIDERS", [FakeProvider(), FailingProvider()])
    async def fake_get(client, url, max_bytes, timeout=8.0):
        if url in IMAGES:
            return httpx.Response(200, content=IMAGES[url])
        return httpx.Response(404)
    monkeypatch.setattr(pipeline, "safe_get", fake_get)
    with TestClient(main.app) as c:
        yield c

def wait(c, sid):
    for _ in range(200):
        s = c.get(f"/api/searches/{sid}").json()
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.05)
    raise AssertionError("timeout")

def test_requires_consent(client):
    r = client.post("/api/searches", files={"file": ("q.jpg", to_bytes(QUERY), "image/jpeg")})
    assert r.status_code == 400

def test_rejects_fake_image(client):
    r = client.post("/api/searches", files={"file": ("q.jpg", b"GIF89a not really", "image/jpeg")}, data={"consent": "true"})
    assert r.status_code == 415

def test_full_flow(client):
    r = client.post("/api/searches", files={"file": ("q.png", to_bytes(QUERY, "PNG"), "image/png")}, data={"consent": "true"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    s = wait(client, sid)
    assert s["status"] == "done"
    assert s["provider_status"]["flaky"]["state"] == "failed"
    res = {x["domain"]: x for x in s["results"]}
    assert len([x for x in s["results"] if x["domain"] == "a.example"]) == 1  # deduped
    assert res["a.example"]["match_type"] in ("exact", "near_duplicate") and res["a.example"]["verified"]
    assert res["a.example"]["page_title"] == "Post A"
    assert res["news.b.example"]["match_type"] == "cropped" and res["news.b.example"]["is_news"]
    assert res["c.example"]["match_type"] == "similar"
    assert res["d.example"]["verified"] is False
    assert s["image_retained"]  # kept because a provider failed -> retry possible

    rid = res["a.example"]["id"]
    assert client.patch(f"/api/results/{rid}", json={"saved": True}).json()["saved"]
    assert len(client.get("/api/saved").json()["results"]) == 1
    assert client.patch(f"/api/results/{rid}", json={"hidden": True}).json()["hidden"]
    assert "a.example" not in client.get(f"/api/searches/{sid}/export?format=csv").text
    assert "Post A" not in client.get(f"/api/searches/{sid}/export?format=html").text
    assert client.get(f"/api/searches/{sid}/export?format=json&include_hidden=true").json()["results"]

    # retry failed providers only
    r = client.post(f"/api/searches/{sid}/retry")
    assert r.json()["retrying"] == ["flaky"]
    s2 = wait(client, sid)
    assert len(s2["results"]) == len(s["results"])

    # another browser can't see it
    other = TestClient(main.app)
    assert other.get(f"/api/searches/{sid}").status_code == 404
    assert other.get(f"/api/searches/{sid}/thumb").status_code == 404

    assert client.delete(f"/api/searches/{sid}").status_code == 204
    assert client.get(f"/api/searches/{sid}").status_code == 404
    assert client.get("/api/saved").json()["results"] == []

def test_signed_url(client):
    r = client.post("/api/searches", files={"file": ("q.jpg", to_bytes(QUERY), "image/jpeg")}, data={"consent": "true"})
    sid = r.json()["id"]; wait(client, sid)
    exp = int(time.time()) + 60
    assert client.get(f"/api/u/{sid}?exp={exp}&sig=bad").status_code == 403
    assert client.get(f"/api/u/{sid}?exp={exp}&sig={security.sign_upload(sid, exp)}").status_code == 200
    old = int(time.time()) - 1
    assert client.get(f"/api/u/{sid}?exp={old}&sig={security.sign_upload(sid, old)}").status_code == 403

def test_ssrf_blocked():
    for u in ("http://127.0.0.1/x", "http://169.254.169.254/latest", "http://10.0.0.1/", "file:///etc/passwd", "http://localhost/"):
        with pytest.raises(security.UnsafeURL):
            asyncio.run(security.assert_public_url(u))
