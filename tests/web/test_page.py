import re

import pytest
from fastapi.testclient import TestClient

from langfuse_to_snowflake.api import create_app
from langfuse_to_snowflake.api.schemas import ConfigUpdate
from langfuse_to_snowflake.config import ApiSettings
from langfuse_to_snowflake.config.store import FIELDS
from langfuse_to_snowflake.entities import ENTITY_NAMES, OBSERVATION_FIELD_GROUPS
from langfuse_to_snowflake.web import STATIC

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPTS = {
    path.relative_to(STATIC).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(STATIC.rglob("*.js"))
}
SCRIPT = "\n".join(SCRIPTS.values())
STYLES = "\n".join(path.read_text(encoding="utf-8") for path in sorted(STATIC.rglob("*.css")))


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
    # Everything the page links to is there, and of the right kind.
    linked = re.findall(r'(?:src|href)="(/ui/[^"]+)"', page.text)
    assert len(linked) > 4
    for address in linked:
        asset = client.get(address)
        assert asset.status_code == 200, address
        if address.endswith(".js"):
            # A browser refuses a module that is not served as JavaScript.
            assert "javascript" in asset.headers["content-type"], address
        if address.endswith(".css"):
            assert asset.headers["content-type"].startswith("text/css"), address
    # The page carries no data; that all comes from the API, which does need the key.
    assert client.get("/config").status_code == 401


def test_a_browser_checks_for_newer_scripts_and_styles_on_every_load(client):
    # They only work together, so none may be kept from before an upgrade.
    for address in ("/", "/ui/js/main.js", "/ui/css/base.css"):
        assert client.get(address).headers["cache-control"] == "no-cache", address


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
    # Nor do the styles or scripts reach for anything elsewhere.
    assert "url(" not in STYLES and "@import" not in STYLES
    assert not re.search(r"""["'`]https?://""", SCRIPT.replace('"http://www.w3.org/2000/svg"', ""))


def test_every_module_a_script_imports_exists():
    # One missing file and the page stays blank.
    assert "js/main.js" in SCRIPTS
    for name, script in SCRIPTS.items():
        imports = re.findall(r'^import .*? from "([^"]+)";$', script, re.MULTILINE | re.DOTALL)
        for target in imports:
            assert target.startswith("."), f"{name} imports {target}"
            assert (STATIC / name).parent.joinpath(target).resolve().is_file(), f"{name}: {target}"


def test_hidden_elements_stay_hidden_whatever_their_class():
    # Classes such as .error set display, which would undo the hidden attribute.
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", STYLES)


def test_the_scripts_never_write_api_text_as_html():
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in SCRIPT
    # The key lives for the tab only.
    assert "localStorage" not in SCRIPT and "sessionStorage" in SCRIPT


def test_the_form_has_a_control_and_an_error_slot_for_every_editable_setting():
    assert set(ConfigUpdate.model_fields) == set(FIELDS)
    for name in FIELDS:
        assert f'data-changed="{name}"' in PAGE, name
        assert f'data-error="{name}"' in PAGE, name
        # Which also gives it a name in the history of changes.
        assert name in SCRIPT, name
    # And one for problems that are not about a single setting.
    assert 'data-error=""' in PAGE


def test_the_scripts_describe_every_entity_and_field_group():
    for name in (*ENTITY_NAMES, *OBSERVATION_FIELD_GROUPS):
        assert re.search(rf"^\s+{name}: \"", SCRIPT, re.MULTILINE), name


def test_every_element_the_scripts_look_up_exists_in_the_page():
    ids = set(re.findall(r'\$\("([a-z-]+)"\)', SCRIPT))
    ids |= set(re.findall(r'^\s+[a-z_]+: "([a-z]+(?:-[a-z]+)+)",$', SCRIPT, re.MULTILINE))
    assert len(ids) > 50
    for element_id in ids:
        assert f'id="{element_id}"' in PAGE, element_id


def test_every_view_and_section_that_is_linked_to_exists():
    views = set(re.findall(r'data-view="([a-z]+)"', PAGE))
    assert views == {"overview", "settings", "runs", "deployment"}
    for view in views:
        assert f'id="view-{view}"' in PAGE, view
        assert f'"{view}"' in SCRIPTS["js/router.js"], view
    sections = set(re.findall(r"#settings/([a-z]+)", PAGE + SCRIPT))
    assert len(sections) > 5
    for section in sections:
        assert f'id="section-{section}"' in PAGE, section


def test_every_drawing_that_is_used_is_in_the_page():
    drawn = set(re.findall(r'<symbol id="i-([a-z]+)"', PAGE))
    used = set(re.findall(r'href="#i-([a-z]+)"', PAGE))
    used |= set(re.findall(r'icon\("([a-z]+)"', SCRIPT))
    used |= set(re.findall(r'drawing: "([a-z]+)"', SCRIPT))
    # The drawing for each status of a run, and for each kind of message.
    used |= set(re.findall(r'^\s+[a-z]+: \["[A-Za-z]+", "([a-z]+)"\],$', SCRIPT, re.MULTILINE))
    used |= set(re.findall(r'(?:done|problem): "([a-z]+)"', SCRIPT))
    assert len(used) > 10
    assert used <= drawn, used - drawn
