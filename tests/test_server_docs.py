import json

from fastapi.testclient import TestClient


def make_app(tmp_path, prefix=""):
    from image_agent.server.app import create_app
    from image_agent.server.settings import ServerSettings
    return create_app(ServerSettings("sqlite+aiosqlite://", "docs-token-private", "unused", tmp_path,
                                     root_path=prefix))


def test_documentation_public_and_business_security(tmp_path):
    client = TestClient(make_app(tmp_path))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["ServerBearer"]["scheme"] == "bearer"
    operation_ids = []
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_ids.append(operation["operationId"])
            assert operation.get("description") and operation.get("tags")
            if path.startswith("/api/v1/"):
                assert operation["security"] == [{"ServerBearer": []}]
            else:
                assert not operation.get("security")
    assert len(operation_ids) == len(set(operation_ids))
    assert "docs-token-private" not in json.dumps(schema)
    assert client.get("/api/v1/services").status_code == 401


def test_openapi_has_actual_routes_upload_download_and_write_only_keys(tmp_path):
    schema = TestClient(make_app(tmp_path)).get("/openapi.json").json()
    paths = schema["paths"]
    expected = {
        "/api/v1/services": {"get", "post"}, "/api/v1/services/{identifier}": {"get", "patch", "delete"},
        "/api/v1/model-configs": {"get", "post"}, "/api/v1/model-configs/{identifier}": {"get", "patch", "delete"},
        "/api/v1/agent-configs": {"get", "post"}, "/api/v1/agent-configs/{identifier}": {"get", "patch", "delete"},
        "/api/v1/media": {"post"}, "/api/v1/tasks": {"post"}, "/api/v1/tasks/{id}": {"get"},
        "/api/v1/tasks/{id}/result": {"get"}, "/api/v1/tasks/{id}/blobs/{blob_id}": {"get"},
        "/api/v1/tasks/{id}/resume": {"post"}, "/api/v1/tasks/{id}/accept": {"post"},
    }
    for path, methods in expected.items():
        assert methods <= paths[path].keys()
    assert "multipart/form-data" in paths["/api/v1/media"]["post"]["requestBody"]["content"]
    submit = paths["/api/v1/tasks"]["post"]
    assert "202" in submit["responses"]
    assert any(p["name"].lower() == "idempotency-key" and p["required"] for p in submit["parameters"])
    assert "image/png" in paths["/api/v1/tasks/{id}/blobs/{blob_id}"]["get"]["responses"]["200"]["content"]
    for definition in schema["components"]["schemas"].values():
        props = definition.get("properties", {})
        assert "api_key_encrypted" not in props
        if "api_key" in props:
            assert props["api_key"]["writeOnly"] is True


def test_proxy_prefix_used_for_docs_schema_and_try_it_out(tmp_path):
    client = TestClient(make_app(tmp_path, "/image-agent"))
    docs = client.get("/docs").text
    redoc = client.get("/redoc").text
    assert "/image-agent/openapi.json" in docs and "/image-agent/openapi.json" in redoc
    schema = client.get("/openapi.json").json()
    assert schema["servers"][0]["url"] == "/image-agent"
