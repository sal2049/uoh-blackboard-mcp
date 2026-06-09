---
title: Blackboard MCP
emoji: 🎓
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Blackboard MCP

Private Model Context Protocol server for University of Ha'il Blackboard.

This Docker Space exposes a FastMCP SSE endpoint for Poke or another personal assistant to read courses, discover assignment-like content, download files, and submit assignment files with credentials passed per request or stored privately as Space secrets.

## MCP Endpoint

After the Hugging Face Space finishes building, connect Poke to:

```text
https://<space-subdomain>.hf.space/sse
```

The container listens on `0.0.0.0:7860`, and Hugging Face routes public traffic to that port through the Docker Space metadata above.

## Tools

### `list_courses`

Lists active Blackboard courses and returns `course_id` values that the assistant can use in later calls.

### `profile_scraper`

Checks what the scraper can currently see across courses, assignment-like content, files, and announcements. This is useful when `get_deadlines` looks empty and the assistant needs to diagnose where Blackboard is hiding content.

### `get_deadlines`

Reads active courses and upcoming assignments/deadlines from Blackboard using calendar APIs, course content APIs, announcements, and HTML fallback scans.

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

Items may include `content_id` and `files` when the deadline came from course content scanning.

### `get_course_work`

Discovers assignment-like course content, available dates, due dates, content IDs, and attachment metadata. Use this before downloading a file when the assistant does not already know the `content_id` or `file_id`.

Arguments:

```json
{
  "course_id": "optional",
  "include_files": true,
  "username": "optional Blackboard username",
  "password": "optional Blackboard password"
}
```

Returns items like:

```json
{
  "course_id": "...",
  "course_name": "...",
  "content_id": "...",
  "title": "...",
  "kind": "assignment",
  "content_type": "...",
  "due_date": "...",
  "available_date": "...",
  "description": "...",
  "url": "...",
  "source": "content-api or html-content",
  "files": [
    {
      "file_id": "...",
      "file_name": "instructions.pdf",
      "mime_type": "application/pdf",
      "url": "..."
    }
  ]
}
```

### `get_announcements`

Reads Blackboard announcements, optionally for one course. Assignment reminders often appear here even when the calendar is empty.

### `download_assignment_file`

Downloads a Blackboard assignment attachment and returns it as Base64.

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

Submits a Base64-encoded file to a Blackboard assignment endpoint.

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

If Blackboard rejects the submission endpoint or requires a different workflow, the tool returns `status: "failed"` with Blackboard's response details.

## Secrets

Do not commit Blackboard credentials. For Hugging Face Spaces, add these as private secrets:

```text
UOH_USER=your_blackboard_username
UOH_PASS=your_blackboard_password
```

Optional secrets:

```text
UOH_TIMEOUT=25
UOH_LOOKAHEAD_DAYS=120
```

The tools can also accept `username` and `password` directly from the MCP client. Explicit tool arguments take priority over secrets.

## Local Test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PORT=7860 HOST=0.0.0.0 python app.py
```

Local SSE endpoint:

```text
http://localhost:7860/sse
```

## Docker

```bash
docker build -t blackboard-mcp .
docker run --rm -p 7860:7860 \
  -e UOH_USER=your_blackboard_username \
  -e UOH_PASS=your_blackboard_password \
  blackboard-mcp
```

## Files

```text
app.py            Hugging Face Docker entrypoint
server.py         FastMCP server and Blackboard scraper tools
requirements.txt Python runtime dependencies
Dockerfile        Hugging Face Docker Space image
```

This is not an official University of Ha'il or Blackboard product. Use it only with accounts and courses you are authorized to access.
