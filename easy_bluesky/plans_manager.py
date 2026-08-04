"""plans_manager.py — Plan type catalog and user-plan-folder configuration."""

import ast
import json
from pathlib import Path

# ── Plan type constants ──────────────────────────────────────────────────────────

PLAN_TYPE_BUILTIN = "builtin"   # bluesky library plans
PLAN_TYPE_PROFILE = "profile"   # plans from ~/.easy_bluesky/scripts/<slug>_plans/
PLAN_TYPE_SESSION = "session"   # uploaded from local user folders (script_upload only)

PLAN_COLORS = {
    PLAN_TYPE_BUILTIN: "#5b9bd5",   # blue
    PLAN_TYPE_PROFILE: "#70c670",   # green
    PLAN_TYPE_SESSION: "#e8a44a",   # amber
}

PLAN_TYPE_LABELS = {
    PLAN_TYPE_BUILTIN: "Bluesky",
    PLAN_TYPE_PROFILE: "Profile",
    PLAN_TYPE_SESSION: "Session",
}


def plan_type_from_module(module: str) -> str:
    """Classify a plan by its __module__ string (fallback when no catalog)."""
    if (module or "").startswith("bluesky."):
        return PLAN_TYPE_BUILTIN
    return PLAN_TYPE_PROFILE


# ── Global catalog singleton ─────────────────────────────────────────────────────

_catalog = None   # set by main.py via set_global_catalog()


def set_global_catalog(catalog) -> None:
    global _catalog
    _catalog = catalog


def get_catalog():
    """Return the global PlanCatalog, or None if not yet initialised."""
    return _catalog


# ── PlanCatalog ──────────────────────────────────────────────────────────────────

class PlanCatalog:
    """Tracks plan types and source file paths; populated at connect time."""

    def __init__(self):
        self._types:   dict = {}   # plan_name → PLAN_TYPE_* constant
        self._sources: dict = {}   # plan_name → file path or module string

    # ── Population ──────────────────────────────────────────────────────────────

    def classify_from_plans_dict(self, plans: dict) -> None:
        """Seed type info from a plans_allowed() response using the module field.

        Only sets PLAN_TYPE_BUILTIN; profile/session types require explicit
        register_file() calls.  Plans already registered as session/profile
        are left unchanged so later script_upload registrations are not lost.
        """
        for name, info in plans.items():
            module = (info.get("module") or "")
            if module.startswith("bluesky."):
                self._types[name]   = PLAN_TYPE_BUILTIN
                self._sources[name] = module
            elif name not in self._types:
                self._types[name]   = PLAN_TYPE_PROFILE
                self._sources[name] = module

    def register_file(self, file_path: str, plan_type: str) -> None:
        """Parse a .py file with ast and register every top-level function."""
        try:
            src  = Path(file_path).read_text(encoding="utf-8")
            tree = ast.parse(src, filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._types[node.name]   = plan_type
                    self._sources[node.name] = file_path
        except Exception:
            pass

    def register_code(self, code: str, source_label: str, plan_type: str) -> None:
        """Like register_file but from an in-memory code string."""
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._types[node.name]   = plan_type
                    self._sources[node.name] = source_label
        except Exception:
            pass

    def clear_tier(self, plan_type: str) -> None:
        keys = [k for k, v in self._types.items() if v == plan_type]
        for k in keys:
            del self._types[k]
            self._sources.pop(k, None)

    def clear(self) -> None:
        self._types.clear()
        self._sources.clear()

    # ── Queries ──────────────────────────────────────────────────────────────────

    def get_type(self, plan_name: str) -> str:
        return self._types.get(plan_name, PLAN_TYPE_PROFILE)

    def get_source(self, plan_name: str) -> str:
        return self._sources.get(plan_name, "")


# ── User plan folder config ──────────────────────────────────────────────────────

_CFG_PATH = Path.home() / ".easy_bluesky" / "plans_config.json"


def _load_cfg() -> dict:
    try:
        if _CFG_PATH.exists():
            return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cfg(cfg: dict) -> None:
    try:
        _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    except Exception:
        pass


def get_user_dirs(profile_name: str) -> list:
    """Return list of local folder paths registered for this profile."""
    return list(_load_cfg().get(profile_name, {}).get("user_dirs", []))


def add_user_dir(profile_name: str, path: str) -> bool:
    """Add a folder path.  Returns True if actually added (was not already present)."""
    cfg   = _load_cfg()
    entry = cfg.setdefault(profile_name, {"user_dirs": []})
    if path not in entry["user_dirs"]:
        entry["user_dirs"].append(path)
        _save_cfg(cfg)
        return True
    return False


def remove_user_dir(profile_name: str, path: str) -> None:
    """Remove a folder path from the profile's user-plan dirs."""
    cfg   = _load_cfg()
    entry = cfg.get(profile_name, {})
    dirs  = entry.get("user_dirs", [])
    if path in dirs:
        dirs.remove(path)
        _save_cfg(cfg)
