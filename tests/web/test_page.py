import re

import pytest
from fastapi.testclient import TestClient

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.api.schemas import ConfigUpdate
from langfuse_to_snowflake.config import ApiSettings
from langfuse_to_snowflake.config.store import FIELDS
from langfuse_to_snowflake.entities import ENTITY_NAMES, OBSERVATION_FIELD_GROUPS
from langfuse_to_snowflake.web import STATIC


@pytest.fixture
def client(settings):
    secured = settings.model_copy(update={"api": ApiSettings(_env_file=None, key="s3cret")})
    with TestClient(create_app(secured, backend=object())) as test_client:
        yield test_client


def test_the_page_and_its_assets_are_served_without_a_key(client):
    page = client.get("/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Langfuse to Snowflake</title>" in page.text
    assert "javascript" in client.get("/ui/app.js").headers["content-type"]
    assert client.get("/ui/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/ui/favicon.svg").status_code == 200
    # The page carries no data; that all comes from the API, which does need the key.
    assert client.get("/config").status_code == 401


def test_the_page_may_only_load_scripts_and_styles_from_this_service(client):
    page = client.get("/")

    policy = page.headers["content-security-policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert page.headers["x-content-type-options"] == "nosniff"
    # Which the page honours: nothing inline, nothing from another host.
    assert not re.search(r"<script(?![^>]*\bsrc=)", page.text)
    assert "<style" not in page.text and " style=" not in page.text
    assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", page.text)
    assert " on" not in " ".join(re.findall(r"<[^>]+>", page.text)).replace(" one", "")


def test_hidden_elements_stay_hidden_whatever_their_class():
    # Classes such as .error set display, which would undo the hidden attribute.
    styles = (STATIC / "app.css").read_text(encoding="utf-8")

    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", styles)


def test_the_script_never_writes_api_text_as_html():
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script
    # The key lives for the tab only.
    assert "localStorage" not in script and "sessionStorage" in script


def test_the_form_has_a_control_and_an_error_slot_for_every_editable_setting():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert set(ConfigUpdate.model_fields) == set(FIELDS)
    for name in FIELDS:
        assert f'data-changed="{name}"' in page, name
        assert f'data-error="{name}"' in page, name
        assert name in script, name
    # And one for problems that are not about a single setting.
    assert 'data-error=""' in page


def test_the_script_describes_every_entity_and_field_group():
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    for name in (*ENTITY_NAMES, *OBSERVATION_FIELD_GROUPS):
        assert re.search(rf"^\s+{name}: \"", script, re.MULTILINE), name


def test_every_element_the_script_looks_up_exists_in_the_page():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    ids = set(re.findall(r'\$\("([a-z-]+)"\)', script))
    ids |= set(re.findall(r'^\s+[a-z_]+: "([a-z]+(?:-[a-z]+)+)",$', script, re.MULTILINE))
    assert len(ids) > 25
    for element_id in ids:
        assert f'id="{element_id}"' in page, element_id
