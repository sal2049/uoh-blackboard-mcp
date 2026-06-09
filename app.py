from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from server import (
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


app = mcp.sse_app()


if __name__ == "__main__":
    mcp.run(transport="sse")
