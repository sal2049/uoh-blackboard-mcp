---
title: UoH Blackboard MCP
emoji: 🎓
colorFrom: blue
colorTo: green
sdk: docker
app_port: 8000
pinned: false
---

# UoH Blackboard MCP

Private Model Context Protocol server for University of Ha'il Blackboard.

This project lets a personal assistant, such as Poke, read Blackboard deadlines, retrieve assignment files, and submit assignment files through a small FastMCP server. It is designed for private student use and runs as a Docker Hugging Face Space.

## Cloud Endpoint

Deploy this project as a private or public Hugging Face Docker Space.

```text
https://huggingface.co/spaces/<hf-username>/<space-name>
```

MCP SSE endpoint:

```text
https://<space-subdomain>.hf.space/sse
```

## Tools

### `get_deadlines`

Reads active courses and upcoming assignments/deadlines from Blackboard.

Arguments:

```json
{
  "username": "optional Blackboard username",
  "password": "optional Blackboard password"
}
```

Returns a JSON list with:

```json
{
  "course_id": "...",
  "course_name": "...",
  "assignment_title": "...",
  "due_date": "...",
  "description": "...",
  "url": "...",
  "source": "api or html"
}
```

### `download_assignment_file`

Downloads a Blackboard assignment attachment and returns it as Base64 so the assistant can inspect, summarize, translate, or process it.

Arguments:

```json
{
  "course_id": "required",
  "content_id": "required",
  "file_id": "required",
  "username": "optional Blackboard username",
  "password": "optional Blackboard password"
}
```

Returns:

```json
{
  "file_name": "...",
  "mime_type": "...",
  "file_extension": "...",
  "size_bytes": 12345,
  "file_base64_content": "..."
}
```

### `submit_assignment`

Submits a Base64-encoded file to a Blackboard assignment submission endpoint.

Arguments:

```json
{
  "course_id": "required",
  "content_id": "required",
  "file_name": "solution.pdf",
  "file_base64_content": "...",
  "username": "optional Blackboard username",
  "password": "optional Blackboard password"
}
```

Returns:

```json
{
  "status": "success or failed",
  "submission_id": "...",
  "confirmation_code": "...",
  "timestamp": "...",
  "http_status": 200,
  "response": {}
}
```

If Blackboard rejects the endpoint or requires a different submission workflow, the tool returns `status: "failed"` with Blackboard's official response details.

## Safety Model

This server is intentionally small and private-use focused.

- Credentials are never hardcoded in the repo.
- `.env` is ignored by git.
- The tools accept credentials dynamically from the assistant.
- If dynamic credentials are not passed, the server falls back to `UOH_USER` and `UOH_PASS`.
- On Hugging Face Spaces, `UOH_USER` and `UOH_PASS` must be configured as **Secrets**, not public variables.
- File download output is returned as Base64 only to the requesting MCP client.
- Assignment submission returns Blackboard's real response instead of pretending success.

Recommended Hugging Face Secrets:

```text
UOH_USER=your_blackboard_username
UOH_PASS=your_blackboard_password
UOH_TIMEOUT=25
UOH_LOOKAHEAD_DAYS=120
```

## Local Development

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run locally:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

Local MCP endpoint:

```text
http://localhost:8000/sse
```

Run with a Cloudflare Quick Tunnel:

```bash
python run_tunnel.py
```

The script prints a temporary public URL like:

```text
https://example.trycloudflare.com/sse
```

## Hugging Face Deployment

This repo is Docker Space ready.

Important files:

- `README.md` declares `sdk: docker` and `app_port: 8000`.
- `Dockerfile` starts `uvicorn server:app --host 0.0.0.0 --port 8000`.
- `server.py` exposes `app = mcp.sse_app()`.

Deploy helper:

```bash
python3 deploy_to_huggingface.py
```

## GitHub Push Helper

If SSH or HTTPS credentials are not configured locally, use:

```bash
python push_to_github.py
```

It asks for a GitHub Personal Access Token without echoing it, creates or reuses the target repo, commits tracked project files, and pushes `main`.

## Project Structure

```text
server.py                 FastMCP server with the three Blackboard tools
requirements.txt          Python runtime dependencies
Dockerfile                Hugging Face Docker Space image
run_tunnel.py             Local uvicorn + Cloudflare Quick Tunnel runner
deploy_to_huggingface.py  Docker Space upload helper
push_to_github.py         GitHub PAT push helper
```

## Notes

- Hugging Face free CPU Spaces may sleep when unused. If the endpoint is slow at first, it may be waking up.
- This is not an official University of Ha'il or Blackboard product.
- Use only with accounts and courses you are authorized to access.
