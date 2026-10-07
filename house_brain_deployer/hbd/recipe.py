"""SHA-enforced recipe builds (independent re-implementation).

A recipe is a JSON object with ``baseline_sha256``, ``target_sha256`` and an
``edits`` list of ``{"old": str, "new": str}`` hunks. Hunks are applied in
reverse order, each must match exactly once, and both the baseline and the
output hash are enforced. This mirrors ``tools/cooling_arbiter_v2/
build_from_recipe.py`` and ``build_evap.py``; the test suite proves the two
implementations produce identical bytes on the real recipes.
"""
from __future__ import annotations

import hashlib
import json

MAX_RECIPE_BYTES = 4 * 1024 * 1024
MAX_EDITS = 500


class RecipeError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build(recipe_bytes: bytes, baseline_bytes: bytes, baseline_path: str, expected_sha256: str) -> bytes:
    if len(recipe_bytes) > MAX_RECIPE_BYTES:
        raise RecipeError("RECIPE_TOO_LARGE")
    try:
        spec = json.loads(recipe_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise RecipeError("RECIPE_JSON") from err
    if not isinstance(spec, dict):
        raise RecipeError("RECIPE_JSON")
    base_sha = spec.get("baseline_sha256")
    target_sha = spec.get("target_sha256")
    edits = spec.get("edits")
    if not (isinstance(base_sha, str) and isinstance(target_sha, str) and isinstance(edits, list)):
        raise RecipeError("RECIPE_SHAPE")
    if "baseline_path" in spec and spec["baseline_path"] != baseline_path:
        raise RecipeError("RECIPE_BASELINE_PATH")
    if target_sha != expected_sha256:
        raise RecipeError("RECIPE_TARGET_MISMATCH")
    if not 1 <= len(edits) <= MAX_EDITS:
        raise RecipeError("RECIPE_EDITS")
    if sha256(baseline_bytes) != base_sha:
        raise RecipeError("BASELINE_SHA_MISMATCH")
    try:
        text = baseline_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        raise RecipeError("BASELINE_ENCODING") from err
    for index in range(len(edits) - 1, -1, -1):
        edit = edits[index]
        if not (isinstance(edit, dict) and isinstance(edit.get("old"), str)
                and isinstance(edit.get("new"), str) and edit["old"]):
            raise RecipeError("RECIPE_EDIT_SHAPE", str(index + 1))
        count = text.count(edit["old"])
        if count != 1:
            raise RecipeError("HUNK_MATCH", f"hunk {index + 1} matched {count}x")
        text = text.replace(edit["old"], edit["new"])
    out = text.encode("utf-8")
    if sha256(out) != expected_sha256:
        raise RecipeError("OUTPUT_SHA_MISMATCH")
    return out
