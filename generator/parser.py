"""Parse a project-documentation markdown file into structured data.

Handles the common shape of an AI-generated project spec:
  - a `**Name**` / `**Purpose**` overview section
  - a fenced `file tree` block
  - a database schema bullet list
  - a `Quick Start` bash block
"""

import re

# Names of tables we know how to generate migrations for.
from .schema import TABLES

FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", re.DOTALL)
NAME_RE = re.compile(r"\*\*Name\*\*\s*:?\s*(.+)")
PURPOSE_RE = re.compile(r"\*\*Purpose\*\*\s*:?\s*(.+)", re.IGNORECASE)
TABLE_RE = re.compile(r"^\s*-\s*\*\*([a-z_]+)\*\*\s*[—-]\s*(.+)$", re.MULTILINE)

TREE_BOX = set("├└│─")
# Box-drawing characters that mark a tree depth (│ = connector, ├/└ = branch).
DEPTH_BOX = "│├└"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def _strip_comment(entry: str) -> str:
    """Remove an inline `# comment` from a tree entry, keeping the path part."""
    # '#' preceded by whitespace starts a comment in these docs.
    return re.split(r"\s+#\s", entry)[0].strip()


def parse_tree_block(text: str):
    """Extract a directory tree from the markdown and return (root_name, dirs, files).

    `dirs` and `files` are lists of absolute paths (relative to the project root,
    not including the root folder itself).
    """
    blocks = FENCE_RE.findall(text)
    tree_block = None
    for b in blocks:
        if any(c in TREE_BOX for c in b):
            tree_block = b
            break
    if tree_block is None:
        return None, [], []

    lines = tree_block.splitlines()
    root_name = None
    dirs = []
    files = []
    # stack of (column, path); column = indentation of the ├/└ branch marker
    stack = []

    for raw in lines:
        if not raw.strip():
            continue

        # Depth is the visual indentation of the branch marker (├ or └).
        # The │ connectors line up with ancestor branch markers, so the column
        # of the *branch* char (not the connectors) is what groups siblings.
        branch_col = -1
        for idx, ch in enumerate(raw):
            if ch in "├└":
                branch_col = idx
                break

        if branch_col == -1:
            # Root line, e.g. "freelance-dashboard/"
            root_candidate = raw.strip().rstrip("/").strip()
            if root_candidate and root_name is None:
                root_name = root_candidate
            continue

        entry = _strip_comment(raw[branch_col:].lstrip("├└─ ").strip())
        if not entry:
            continue
        is_dir = entry.endswith("/")
        name = entry.rstrip("/").strip()
        if not name:
            continue

        # pop stack entries at or deeper (same-or-greater column) than current
        while stack and stack[-1][0] >= branch_col:
            stack.pop()
        parent = stack[-1][1] if stack else ""

        path = f"{parent}/{name}".strip("/") if parent else name
        if is_dir:
            dirs.append(path)
            stack.append((branch_col, path))
        else:
            files.append(path)

    return root_name, dirs, files


def parse(md_text: str) -> dict:
    name_m = NAME_RE.search(md_text)
    purpose_m = PURPOSE_RE.search(md_text)
    name = name_m.group(1).strip() if name_m else "Freelance Dashboard"
    purpose = purpose_m.group(1).strip() if purpose_m else ""

    root_name, dirs, files = parse_tree_block(md_text)
    if not root_name:
        root_name = _slug(name)

    # Database tables that appear in the doc AND that we can generate SQL for.
    tables = []
    for m in TABLE_RE.finditer(md_text):
        table_name, desc = m.group(1).strip(), m.group(2).strip()
        if table_name in TABLES:
            tables.append({"name": table_name, "description": desc})

    # Quick start bash block (last fenced block that looks like shell).
    quick_start = ""
    for b in reversed(FENCE_RE.findall(md_text)):
        if "npm" in b or "cd " in b:
            quick_start = b.strip()
            break

    return {
        "name": name,
        "purpose": purpose,
        "root_name": root_name,
        "slug": _slug(root_name or name),
        "dirs": dirs,
        "files": files,
        "tables": tables,
        "quick_start": quick_start,
        "raw": md_text,
    }
