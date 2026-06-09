from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server import (
    app as asgi_app,
    download_assignment_file,
    get_announcements,
    get_course_work,
    get_deadlines,
    list_courses,
    mcp,
    profile_scraper,
    submit_assignment,
)


if not isinstance(mcp, FastMCP):
    raise TypeError("Expected server.mcp to be a FastMCP instance.")


app = asgi_app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)
