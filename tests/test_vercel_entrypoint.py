import importlib.util
import json
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_vercels_entrypoint_is_a_module_file_at_the_project_root_and_serves_the_app():
    entrypoint = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["vercel"]["entrypoint"]
    module, _, attr = entrypoint.partition(":")
    path = REPO / f"{module.replace('.', '/')}.py"
    assert path.is_file(), f"Vercel looks for {path.relative_to(REPO)} from the project root"
    spec = importlib.util.spec_from_file_location(module, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    from rcm_ai import web
    assert getattr(loaded, attr) is web.app


def test_vercel_json_configures_the_entrypoints_function():
    entrypoint = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["vercel"]["entrypoint"]
    functions = json.loads((REPO / "vercel.json").read_text())["functions"]
    assert list(functions) == [f"{entrypoint.partition(':')[0].replace('.', '/')}.py"]
