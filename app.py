"""Flask app: turn a project-documentation .md file into a saveable project.

Endpoints:
  GET  /            -> the single-page UI
  POST /api/generate -> accepts { text } or a file upload; returns the file tree
  GET  /api/download?path=... -> download a single generated file
  GET  /api/zip -> download the whole project as a ZIP
"""

import io
import os
import zipfile

from flask import Flask, jsonify, render_template, request, send_file

from generator.parser import parse
from generator.scaffold import build

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload cap

# In-memory cache of the most recent generation (one at a time is fine for this tool).
_CACHE = {}


def _sort_key(p):
    parts = p.split("/")
    # sort directories (no content) first, then by name
    return parts


def _tree(files: dict) -> list:
    """Build a nested node structure for the UI."""
    nodes = {"__files__": []}

    def ensure(path_parts, node):
        if not path_parts:
            return node
        head = path_parts[0]
        if head not in node:
            node[head] = {"__files__": []}
        return ensure(path_parts[1:], node[head])

    for path, content in files.items():
        parts = path.split("/")
        # descend into dirs, attach file at the leaf dir
        parent = ensure(parts[:-1], nodes)
        parent["__files__"].append((parts[-1], path))

    def serialize(name, node):
        entries = []
        # child dirs
        for child in sorted(k for k in node if k != "__files__"):
            entries.append({"name": child, "type": "dir", "children": serialize(child, node[child])})
        # files
        for fname, fpath in sorted(node["__files__"]):
            entries.append({"name": fname, "type": "file", "path": fpath})
        return entries

    return serialize("", nodes)


def _detect_lang(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {
        "js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
        "sql": "sql", "json": "json", "md": "markdown", "css": "css", "html": "html",
        "py": "python", "sh": "bash", "yml": "yaml", "yaml": "yaml",
    }.get(ext, "")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/generate", methods=["POST"])
def generate():
    text = None
    if "file" in request.files and request.files["file"].filename:
        f = request.files["file"]
        text = f.read().decode("utf-8", errors="replace")
    elif request.json and request.json.get("text"):
        text = request.json["text"]
    elif request.form and request.form.get("text"):
        text = request.form["text"]

    if not text or not text.strip():
        return jsonify({"error": "No markdown provided. Paste a .md file or upload one."}), 400

    try:
        parsed = parse(text)
        files = build(parsed)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"error": f"Failed to generate project: {exc}"}), 500

    _CACHE["files"] = files

    tree = _tree(files)
    return jsonify(
        {
            "root": parsed["root_name"] or parsed["slug"],
            "name": parsed["name"],
            "purpose": parsed["purpose"],
            "tables": [t["name"] for t in parsed["tables"]],
            "fileCount": len(files),
            "tree": tree,
        }
    )


@app.route("/api/file")
def file_content():
    path = request.args.get("path", "")
    files = _CACHE.get("files", {})
    if path not in files:
        return jsonify({"error": "File not found."}), 404
    return jsonify({"path": path, "content": files[path], "lang": _detect_lang(path)})


@app.route("/api/download")
def download_file():
    path = request.args.get("path", "")
    files = _CACHE.get("files", {})
    if path not in files:
        return jsonify({"error": "File not found."}), 404
    name = path.split("/")[-1]
    data = files[path].encode("utf-8")
    return send_file(
        io.BytesIO(data),
        mimetype="text/plain",
        as_attachment=True,
        download_name=name,
    )


@app.route("/api/zip")
def download_zip():
    files = _CACHE.get("files", {})
    if not files:
        return jsonify({"error": "Nothing generated yet."}), 400
    root = next(iter(files)).split("/")[0]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{root}.zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
