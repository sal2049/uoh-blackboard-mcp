from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

import app as mcp_entry
from server import (
    download_assignment_file,
    get_course_work,
    get_deadlines,
    list_courses,
    profile_scraper,
    submit_assignment,
)


def parse_json(label: str, text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{label} did not return JSON: {text[:500]}") from exc


def print_json(label: str, value: Any) -> None:
    print(f"\n===== {label} =====")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def require_credentials() -> tuple[str, str]:
    load_dotenv()
    username = os.getenv("UOH_USER")
    password = os.getenv("UOH_PASS")
    if not username or not password:
        raise SystemExit(
            "Missing local Blackboard credentials. Create a .env file in this project with "
            "UOH_USER and UOH_PASS before running this test."
        )
    return username, password


def assert_structured_non_empty(label: str, value: Any) -> None:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return
    if isinstance(value, dict) and value:
        return
    raise AssertionError(f"{label} returned no structured data.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Blackboard MCP integration checks.")
    parser.add_argument("--allow-submit", action="store_true", help="Actually call submit_assignment using UOH_TEST_SUBMIT_* env vars.")
    args = parser.parse_args()

    username, password = require_credentials()

    registered = [tool.name for tool in mcp_entry.mcp._tool_manager.list_tools()]
    print_json("registered_tools", registered)
    for required in ("get_deadlines", "download_assignment_file", "submit_assignment"):
        if required not in registered:
            raise AssertionError(f"{required} is not registered in FastMCP.")

    deadlines = parse_json("get_deadlines", get_deadlines(username=username, password=password))
    print_json("get_deadlines", deadlines)

    if not deadlines:
        courses = parse_json("list_courses", list_courses(username=username, password=password))
        print_json("list_courses_fallback", courses)
        profile = parse_json("profile_scraper", profile_scraper(username=username, password=password))
        print_json("profile_scraper_fallback", profile)
        work = parse_json("get_course_work", get_course_work(username=username, password=password))
        print_json("get_course_work_fallback", work)
        if not courses and not work:
            raise AssertionError("Blackboard login worked, but no courses, work items, or deadlines were discovered.")
        raise AssertionError(
            "get_deadlines returned an empty list. Fallback discovery found structured data above, "
            "but deadline extraction still needs scraper tuning."
        )

    assert_structured_non_empty("get_deadlines", deadlines)

    course_id = os.getenv("UOH_TEST_COURSE_ID")
    content_id = os.getenv("UOH_TEST_CONTENT_ID")
    file_id = os.getenv("UOH_TEST_FILE_ID")
    if course_id and content_id and file_id:
        downloaded = parse_json(
            "download_assignment_file",
            download_assignment_file(
                course_id=course_id,
                content_id=content_id,
                file_id=file_id,
                username=username,
                password=password,
            ),
        )
        print_json("download_assignment_file", {**downloaded, "file_base64_content": "<base64 omitted>"})
        assert downloaded.get("file_base64_content")
    else:
        print("\n===== download_assignment_file =====")
        print("SKIPPED: set UOH_TEST_COURSE_ID, UOH_TEST_CONTENT_ID, and UOH_TEST_FILE_ID to test a real file download.")

    if args.allow_submit:
        submit_course_id = os.getenv("UOH_TEST_SUBMIT_COURSE_ID")
        submit_content_id = os.getenv("UOH_TEST_SUBMIT_CONTENT_ID")
        submit_file_name = os.getenv("UOH_TEST_SUBMIT_FILE_NAME", "mcp-test.txt")
        submit_text = os.getenv("UOH_TEST_SUBMIT_TEXT", "Blackboard MCP local submission test.")
        if not submit_course_id or not submit_content_id:
            raise AssertionError("Set UOH_TEST_SUBMIT_COURSE_ID and UOH_TEST_SUBMIT_CONTENT_ID before using --allow-submit.")
        submitted = parse_json(
            "submit_assignment",
            submit_assignment(
                course_id=submit_course_id,
                content_id=submit_content_id,
                file_name=submit_file_name,
                file_base64_content=base64.b64encode(submit_text.encode("utf-8")).decode("ascii"),
                username=username,
                password=password,
            ),
        )
        print_json("submit_assignment", submitted)
        if submitted.get("status") != "success":
            raise AssertionError("submit_assignment did not report success. See response above.")
    else:
        print("\n===== submit_assignment =====")
        print("SKIPPED: pass --allow-submit and UOH_TEST_SUBMIT_* env vars to test a safe real submission target.")

    print("\nLOCAL TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
