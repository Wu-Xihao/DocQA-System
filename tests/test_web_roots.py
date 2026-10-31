"""Browser authorization of Service source roots edits YAML safely and reversibly."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docqa.domain.errors import DocQAError
from docqa.service.config import load_options
from docqa.web.roots import add_root, check_root, remove_root


def service_yaml(tmp_path: Path) -> Path:
    config = tmp_path / "configs" / "service.yaml"
    config.parent.mkdir()
    (tmp_path / "uploads").mkdir()
    config.write_text(
        "# Local service\nallowed_source_roots:\n- ../uploads\ntypes:\n- pdf\n"
        "settle_seconds: 0\n",
        encoding="utf-8",
    )
    return config


def test_yaml_edit_keeps_comments_and_round_trips(tmp_path):
    config = service_yaml(tmp_path)
    folder = tmp_path / "资料 库"
    folder.mkdir()
    add_root(config, folder)
    text = config.read_text(encoding="utf-8")
    assert text.startswith("# Local service") and "types:\n- pdf" in text
    roots = load_options(config).allowed_source_roots
    assert Path(folder) in [Path(r) for r in roots] and len(roots) == 2
    remove_root(config, folder)
    assert len(load_options(config).allowed_source_roots) == 1


@pytest.fixture(autouse=True)
def temp_not_protected(monkeypatch):
    # pytest's tmp_path lives under LOCALAPPDATA, which real requests may not authorize.
    monkeypatch.setattr("docqa.web.roots._protected", lambda: [])


def test_protected_folders_are_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("docqa.web.roots._protected", lambda: [tmp_path])
    (tmp_path / "x").mkdir()
    with pytest.raises(DocQAError) as error:
        check_root(str(tmp_path / "x"), [])
    assert error.value.code == "ROOT_PROTECTED"


def test_root_guards(tmp_path):
    with pytest.raises(DocQAError) as error:
        check_root(str(Path(tmp_path.anchor)), [])
    assert error.value.code == "ROOT_TOO_BROAD"
    with pytest.raises(DocQAError) as error:
        check_root(str(Path.home()), [])
    assert error.value.code == "ROOT_TOO_BROAD"
    state = tmp_path / "state"
    state.mkdir()
    with pytest.raises(DocQAError) as error:
        check_root(str(tmp_path), [state])
    assert error.value.code == "SOURCE_ROOT_OVERLAP"
    with pytest.raises(DocQAError):
        check_root(str(tmp_path / "missing"), [])


def test_web_authorizes_then_monitors_and_revokes(tmp_path):
    from docqa.agent.config import AgentOptions
    from docqa.docbase.config import DocBaseOptions
    from docqa.web.app import create_app
    from docqa.web.config import WebOptions

    config = service_yaml(tmp_path)
    retrieval = DocBaseOptions(mode="demo")
    text = config.read_text(encoding="utf-8") + "retrieval:\n  mode: demo\n"
    config.write_text(text, encoding="utf-8")
    folder = tmp_path / "external"
    folder.mkdir()
    options = WebOptions(
        index=tmp_path / "service" / "index",
        state=tmp_path / "web",
        service_state=tmp_path / "service",
        service_config=config,
        upload_root=tmp_path / "uploads",
        service_mode="external",
        agent=AgentOptions(mode="demo", retrieval=retrieval),
    )
    with TestClient(create_app(options), base_url="http://127.0.0.1:8765") as client:
        client.headers["X-DocQA-Token"] = client.get("/api/v1/bootstrap").json()["csrf_token"]
        body = {"path": str(folder), "types": ["pdf"], "interval_seconds": 1800}
        denied = client.post("/api/v1/maintenance/sources", json=body)
        assert denied.json()["error"]["code"] == "SOURCE_ROOT_NOT_ALLOWED"
        granted = client.post("/api/v1/maintenance/roots", json={"path": str(folder)})
        assert granted.status_code == 201 and granted.json()["added"] is True
        again = client.post("/api/v1/maintenance/roots", json={"path": str(folder)})
        assert again.json()["added"] is False
        created = client.post("/api/v1/maintenance/sources", json=body)
        assert created.status_code == 201, created.text
        listing = client.get("/api/v1/maintenance/sources").json()
        assert listing["can_authorize"] and str(folder) in listing["web_authorized_roots"]
        busy = client.request("DELETE", "/api/v1/maintenance/roots", json={"path": str(folder)})
        assert busy.json()["error"]["code"] == "ROOT_IN_USE"
        source_id = created.json()["source_id"]
        assert client.delete(f"/api/v1/maintenance/sources/{source_id}").status_code == 202
        revoked = client.request("DELETE", "/api/v1/maintenance/roots", json={"path": str(folder)})
        assert revoked.status_code == 200, revoked.text
        uploads = client.request(
            "DELETE", "/api/v1/maintenance/roots", json={"path": str(tmp_path / "uploads")}
        )
        assert uploads.status_code == 404  # Roots from YAML are never removed via the Web.
    assert len(load_options(config).allowed_source_roots) == 1
