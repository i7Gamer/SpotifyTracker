import ast
import importlib
from pathlib import Path

import Database.queries._base as queryBase
import Database.repository as repositoryModule


QUERY_MODULE_NAMES = (
    "bios",
    "email_queries",
    "genres",
    "merges",
    "milestones",
    "plays",
    "schema",
    "settings",
    "shares",
    "tags",
    "tracks",
    "trends",
    "users",
    "wrapped",
)

QUERY_MODULE_COMPATIBILITY_EXPORTS = {
    "merges": ("time",),
    "plays": ("time",),
    "shares": ("time",),
    "tracks": ("time",),
    "users": ("keyFingerprint",),
}

REPOSITORY_BASE_EXPORTS = (
    "IMAGE_KIND_TRACK",
    "IMAGE_STATUS_PENDING",
    "TRACK_ISRC_RETRY_SECONDS",
    "SKIP_MODE_SECONDS",
)

REPOSITORY_QUERY_EXPORTS = (
    "EmailQueries",
    "PlayQueries",
    "Repository",
    "TrackQueries",
)


def _queryPath(moduleName: str) -> Path:
    return Path("Database") / "queries" / f"{moduleName}.py"


def test_query_modules_import_named_base_dependencies():
    offenders = []
    for moduleName in QUERY_MODULE_NAMES:
        tree = ast.parse(_queryPath(moduleName).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "Database.queries._base"
                and any(alias.name == "*" for alias in node.names)
            ):
                offenders.append(moduleName)

    assert offenders == []


def test_query_module_patch_targets_keep_base_object_identity():
    for moduleName, names in QUERY_MODULE_COMPATIBILITY_EXPORTS.items():
        module = importlib.import_module(f"Database.queries.{moduleName}")
        for name in names:
            assert getattr(module, name) is getattr(queryBase, name)


def test_repository_keeps_base_and_query_facade_exports():
    for name in REPOSITORY_BASE_EXPORTS:
        assert getattr(repositoryModule, name) is getattr(queryBase, name)

    for name in REPOSITORY_QUERY_EXPORTS:
        assert getattr(repositoryModule, name) is not None


def test_app_keeps_repository_facade_exports():
    appModule = importlib.import_module("app")

    assert appModule.Repository is repositoryModule.Repository
