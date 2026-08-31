# Ai-Project — Project Generator

Turns a **project-documentation `.md` file** (the kind an AI writes out that
describes a project and its file structure) into **real, saveable files**.

Paste or upload your docs, and it generates:

- the full **folder/file scaffold** described in the doc's directory tree,
- **backend SQL** — migrations, RLS policies, seed data and Edge Functions
  (built from the doc's "Database Schema" tables),
- a **React + Vite frontend** scaffold (routes, layouts, stores, hooks, pages),
- **docs** (schema, API, deployment, security, user guide) and **tests**,
- downloadable as individual files or a single **ZIP**.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
# open http://localhost:5000
```

Or without a virtualenv (system Flask): `python3 app.py`.

## How it works

- `generator/parser.py` — parses the markdown: project name/purpose, the
  fenced directory tree, the database-schema bullet list, and the quick-start
  block.
- `generator/schema.py` — declarative Postgres schema (tables + RLS policies)
  for the Freelance Dashboard, used to emit real SQL.
- `generator/scaffold.py` — builds all file contents from the parsed spec.
- `app.py` — a small Flask UI: paste/upload `.md` → preview the tree → download
  files or a ZIP.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | the UI |
| POST | `/api/generate` | accepts `{ "text": "…" }` or a file upload; returns the file tree |
| GET | `/api/file?path=…` | returns a single file's content |
| GET | `/api/download?path=…` | downloads a single file |
| GET | `/api/zip` | downloads the whole project as a ZIP |
