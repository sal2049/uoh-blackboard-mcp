from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is optional at import time.
    load_dotenv = None


BASE_URL = "https://uoh.blackboard.com/"
LOGIN_URL = urljoin(BASE_URL, "webapps/login/")
LOOKAHEAD_DAYS = int(os.getenv("UOH_LOOKAHEAD_DAYS", "120"))
REQUEST_TIMEOUT = int(os.getenv("UOH_TIMEOUT", "25"))
MAX_CONTENT_ITEMS_PER_COURSE = int(os.getenv("UOH_MAX_CONTENT_ITEMS_PER_COURSE", "250"))
WORK_KEYWORDS = (
    "assignment",
    "assessment",
    "homework",
    "quiz",
    "test",
    "exam",
    "turnitin",
    "submission",
    "submit",
    "due",
    "deadline",
    "واجب",
    "اختبار",
    "تسليم",
    "موعد",
)


class BlackboardError(RuntimeError):
    """Raised for Blackboard authentication, parsing, and API failures."""


def json_response(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or ""))).strip()


def first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    text = clean_text(value)
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%m/%d/%y %I:%M %p",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def find_date_like_text(text: str) -> str | None:
    for pattern in (
        r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2})?\b",
        r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)?\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    return None


def extract_list(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []

    for key in ("results", "items", "calendarItems", "courses", "contents", "attachments", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    nested = body.get("body")
    if isinstance(nested, dict):
        return extract_list(nested)
    return []


def safe_json(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except ValueError:
        return None


def short_response(response: requests.Response) -> str:
    body = safe_json(response)
    if body is not None:
        return clean_text(json.dumps(body, ensure_ascii=False))[:2000]
    return clean_text(response.text)[:2000]


class BlackboardClient:
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        if load_dotenv is not None:
            load_dotenv()

        self.username = username or os.getenv("UOH_USER")
        self.password = password or os.getenv("UOH_PASS")
        if not self.username or not self.password:
            raise BlackboardError("Missing Blackboard credentials. Pass username/password or set UOH_USER and UOH_PASS.")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            }
        )

    def login(self) -> None:
        landing = self.get(LOGIN_URL)
        form = self.find_login_form(landing.text)
        if form is None:
            form = self.find_login_form(self.get(BASE_URL).text)
        if form is None:
            raise BlackboardError("Could not find a Blackboard login form.")

        action = form.get("action") or LOGIN_URL
        login_url = urljoin(landing.url, action)
        payload = self.build_login_payload(form)
        headers = {
            "Referer": landing.url,
            "Origin": f"{urlparse(login_url).scheme}://{urlparse(login_url).netloc}",
        }

        method = (form.get("method") or "post").lower()
        if method == "get":
            response = self.get(login_url, params=payload, headers=headers)
        else:
            response = self.post(login_url, data=payload, headers=headers)

        if self.login_failed(response) or (self.find_login_form(response.text) and not self.looks_authenticated(response)):
            raise BlackboardError("Blackboard rejected the login or returned another login form.")

        # Validate by touching a Blackboard API/page that requires the authenticated session.
        probe = self.session.get(urljoin(BASE_URL, "learn/api/public/v1/courses?limit=1"), timeout=REQUEST_TIMEOUT)
        if probe.status_code in {401, 403}:
            raise BlackboardError(f"Authenticated session validation failed with HTTP {probe.status_code}.")

    def get_deadlines(self) -> list[dict[str, Any]]:
        self.login()
        courses = self.fetch_courses()
        deadlines = self.fetch_calendar_deadlines(courses)
        deadlines.extend(self.deadlines_from_course_work(courses))
        deadlines.extend(self.deadlines_from_announcements(courses))
        deadlines.extend(self.fetch_html_deadlines(courses))
        deadlines = self.dedupe_deadlines(deadlines)

        deadlines.sort(
            key=lambda item: (
                parse_datetime(item.get("due_date")) is None,
                parse_datetime(item.get("due_date")) or datetime.max.replace(tzinfo=UTC),
                item.get("course_name") or "",
                item.get("assignment_title") or "",
            )
        )
        return deadlines

    def list_courses(self) -> list[dict[str, Any]]:
        self.login()
        return self.fetch_courses()

    def get_course_work(self, course_id: str | None = None, include_files: bool = True) -> list[dict[str, Any]]:
        self.login()
        courses = self.select_courses(course_id)
        work: list[dict[str, Any]] = []
        for course in courses:
            work.extend(self.fetch_course_work(course, include_files=include_files))
        return self.dedupe_work(work)

    def get_announcements(self, course_id: str | None = None) -> list[dict[str, Any]]:
        self.login()
        announcements: list[dict[str, Any]] = []
        for course in self.select_courses(course_id):
            announcements.extend(self.fetch_course_announcements(course))
        return announcements

    def profile_scraper(self) -> dict[str, Any]:
        self.login()
        courses = self.fetch_courses()
        course_summaries = []
        for course in courses:
            work = self.fetch_course_work(course, include_files=True)
            announcements = self.fetch_course_announcements(course)
            course_summaries.append(
                {
                    "course_id": course.get("course_id"),
                    "course_name": course.get("course_name"),
                    "work_items_found": len(work),
                    "items_with_due_dates": sum(1 for item in work if item.get("due_date")),
                    "files_found": sum(len(item.get("files") or []) for item in work),
                    "announcements_found": len(announcements),
                }
            )
        return {
            "status": "ok",
            "courses_found": len(courses),
            "lookahead_days": LOOKAHEAD_DAYS,
            "max_content_items_per_course": MAX_CONTENT_ITEMS_PER_COURSE,
            "courses": course_summaries,
        }

    def download_assignment_file(self, course_id: str, content_id: str, file_id: str) -> dict[str, Any]:
        self.login()
        content = self.fetch_content(course_id, content_id)
        attachment = self.find_attachment(content, file_id)
        if attachment is None:
            attachment = self.find_attachment_from_html(course_id, content_id, file_id)
        if attachment is None:
            raise BlackboardError("Could not locate that file attachment in the Blackboard content item.")

        file_url = attachment["url"]
        response = self.get(file_url)
        file_bytes = response.content
        mime_type = response.headers.get("content-type", "").split(";")[0] or attachment.get("mime_type")
        file_name = self.file_name_from_response(response, attachment.get("file_name") or file_id)
        extension = Path(file_name).suffix or (mimetypes.guess_extension(mime_type or "") or "")

        return {
            "file_name": file_name,
            "mime_type": mime_type or "application/octet-stream",
            "file_extension": extension,
            "size_bytes": len(file_bytes),
            "file_base64_content": base64.b64encode(file_bytes).decode("ascii"),
        }

    def submit_assignment(
        self,
        course_id: str,
        content_id: str,
        file_name: str,
        file_base64_content: str,
    ) -> dict[str, Any]:
        self.login()
        try:
            file_bytes = base64.b64decode(file_base64_content, validate=True)
        except ValueError as exc:
            raise BlackboardError("file_base64_content is not valid base64.") from exc

        submit_url = urljoin(BASE_URL, f"learn/api/public/v1/courses/{course_id}/contents/{content_id}/submissions")
        mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        files = {"file": (file_name, file_bytes, mime_type)}
        data = {"fileName": file_name}
        response = self.session.post(submit_url, data=data, files=files, timeout=REQUEST_TIMEOUT)
        body = safe_json(response)
        ok = 200 <= response.status_code < 300

        return {
            "status": "success" if ok else "failed",
            "submission_id": self.find_value(body, ("id", "submissionId", "submission_id")) if body is not None else None,
            "confirmation_code": self.find_value(body, ("confirmationCode", "confirmation_code", "receiptId")) if body is not None else None,
            "timestamp": datetime.now(UTC).isoformat(),
            "http_status": response.status_code,
            "response": body if body is not None else short_response(response),
        }

    def fetch_courses(self) -> list[dict[str, Any]]:
        for path in (
            "learn/api/public/v1/courses?availability.available=Yes&limit=100",
            "learn/api/public/v1/courses?limit=100",
        ):
            response = self.get(urljoin(BASE_URL, path), accept_json=True)
            body = safe_json(response)
            courses = []
            for item in extract_list(body):
                course_id = first_present(item.get("id"), item.get("courseId"), item.get("externalId"))
                if not course_id:
                    continue
                courses.append(
                    {
                        "course_id": str(course_id),
                        "course_name": clean_text(first_present(item.get("name"), item.get("courseId"), item.get("externalId"))),
                    }
                )
            if courses:
                return courses

        return self.fetch_courses_from_html()

    def fetch_courses_from_html(self) -> list[dict[str, Any]]:
        pages = [
            BASE_URL,
            urljoin(BASE_URL, "ultra/course"),
            urljoin(BASE_URL, "webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"),
        ]
        seen: set[str] = set()
        courses: list[dict[str, Any]] = []
        for url in pages:
            try:
                page = self.get(url)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(page.text, "html.parser")
            for link in soup.find_all("a", href=True):
                course_id = self.extract_course_id(link["href"])
                if not course_id or course_id in seen:
                    continue
                seen.add(course_id)
                courses.append({"course_id": course_id, "course_name": clean_text(link.get_text(" ")) or course_id})
        return courses

    def select_courses(self, course_id: str | None = None) -> list[dict[str, Any]]:
        courses = self.fetch_courses()
        if not course_id:
            return courses
        selected = [course for course in courses if str(course.get("course_id")) == str(course_id)]
        if selected:
            return selected
        return [{"course_id": course_id, "course_name": course_id}]

    def fetch_calendar_deadlines(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        course_names = {course["course_id"]: course.get("course_name") for course in courses}
        start = datetime.now(UTC)
        end = start + timedelta(days=LOOKAHEAD_DAYS)
        start_iso = start.isoformat().replace("+00:00", "Z")
        end_iso = end.isoformat().replace("+00:00", "Z")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        candidates = (
            f"learn/api/public/v1/calendarItems?{urlencode({'start': start_iso, 'end': end_iso, 'limit': 250})}",
            f"learn/api/public/v1/calendarItems?{urlencode({'since': start_iso, 'until': end_iso, 'limit': 250})}",
            f"webapps/calendar/calendarData/selectedCalendarEvents?{urlencode({'start': start_ms, 'end': end_ms})}",
        )

        deadlines: list[dict[str, Any]] = []
        for path in candidates:
            try:
                response = self.get(urljoin(BASE_URL, path), accept_json=True)
            except requests.RequestException:
                continue
            body = safe_json(response)
            for item in extract_list(body):
                normalized = self.normalize_deadline(item, course_names)
                if normalized:
                    deadlines.append(normalized)
        return self.dedupe_deadlines(deadlines)

    def fetch_html_deadlines(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deadlines: list[dict[str, Any]] = []
        for course in courses:
            course_id = course["course_id"]
            urls = (
                urljoin(BASE_URL, f"ultra/courses/{course_id}/outline"),
                urljoin(BASE_URL, f"webapps/blackboard/execute/courseMain?course_id={course_id}"),
                urljoin(BASE_URL, f"webapps/blackboard/content/listContent.jsp?course_id={course_id}"),
            )
            for url in urls:
                try:
                    response = self.get(url)
                except requests.RequestException:
                    continue
                deadlines.extend(self.parse_deadlines_from_html(response.text, response.url, course))
        return self.dedupe_deadlines(deadlines)

    def deadlines_from_course_work(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deadlines: list[dict[str, Any]] = []
        for course in courses:
            for item in self.fetch_course_work(course, include_files=True):
                if not item.get("due_date") and item.get("kind") not in {"assignment", "assessment"}:
                    continue
                deadlines.append(
                    {
                        "course_id": item.get("course_id"),
                        "course_name": item.get("course_name"),
                        "assignment_title": item.get("title"),
                        "due_date": item.get("due_date"),
                        "description": item.get("description"),
                        "url": item.get("url"),
                        "source": item.get("source"),
                        "content_id": item.get("content_id"),
                        "content_type": item.get("content_type"),
                        "files": item.get("files", []),
                    }
                )
        return deadlines

    def deadlines_from_announcements(self, courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deadlines: list[dict[str, Any]] = []
        for course in courses:
            for announcement in self.fetch_course_announcements(course):
                text = f"{announcement.get('title', '')} {announcement.get('body', '')}"
                if not self.looks_like_work(text):
                    continue
                deadlines.append(
                    {
                        "course_id": announcement.get("course_id"),
                        "course_name": announcement.get("course_name"),
                        "assignment_title": announcement.get("title"),
                        "due_date": find_date_like_text(text),
                        "description": clean_text(announcement.get("body"))[:1200],
                        "url": announcement.get("url"),
                        "source": "announcement",
                    }
                )
        return deadlines

    def fetch_course_work(self, course: dict[str, Any], include_files: bool = True) -> list[dict[str, Any]]:
        api_items = self.fetch_course_content_tree(course, include_files=include_files)
        html_items = self.fetch_course_work_from_html(course, include_files=include_files)
        return self.dedupe_work(api_items + html_items)

    def fetch_course_content_tree(self, course: dict[str, Any], include_files: bool = True) -> list[dict[str, Any]]:
        course_id = str(course["course_id"])
        candidates = (
            f"learn/api/public/v1/courses/{course_id}/contents?limit=100",
            f"learn/api/public/v1/courses/{course_id}/contents?recursive=true&limit=100",
            f"learn/api/v1/courses/{course_id}/contents?limit=100",
        )
        raw_items: list[dict[str, Any]] = []
        queue: list[tuple[str | None, str]] = [(None, path) for path in candidates]
        seen_urls: set[str] = set()
        seen_content_ids: set[str] = set()

        while queue and len(raw_items) < MAX_CONTENT_ITEMS_PER_COURSE:
            parent_id, path = queue.pop(0)
            url = urljoin(BASE_URL, path)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                response = self.get(url, accept_json=True)
            except requests.RequestException:
                continue
            body = safe_json(response)
            items = extract_list(body)
            for item in items:
                content_id = self.extract_content_id_from_value(item) or parent_id
                if content_id and content_id in seen_content_ids:
                    continue
                if content_id:
                    seen_content_ids.add(content_id)
                raw_items.append(item)
                if content_id:
                    queue.extend(
                        [
                            (
                                content_id,
                                f"learn/api/public/v1/courses/{course_id}/contents/{content_id}/children?limit=100",
                            ),
                            (
                                content_id,
                                f"learn/api/v1/courses/{course_id}/contents/{content_id}/children?limit=100",
                            ),
                        ]
                    )

        normalized = []
        for item in raw_items:
            work_item = self.normalize_content_item(item, course, include_files=include_files)
            if work_item and (self.looks_like_work(json.dumps(item, ensure_ascii=False)) or work_item.get("files")):
                normalized.append(work_item)
        return normalized

    def fetch_course_work_from_html(self, course: dict[str, Any], include_files: bool = True) -> list[dict[str, Any]]:
        course_id = str(course["course_id"])
        urls = (
            urljoin(BASE_URL, f"ultra/courses/{course_id}/outline"),
            urljoin(BASE_URL, f"webapps/blackboard/execute/courseMain?course_id={course_id}"),
            urljoin(BASE_URL, f"webapps/blackboard/content/listContent.jsp?course_id={course_id}"),
        )
        items: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        pages_to_scan = list(urls)
        while pages_to_scan and len(seen_pages) < 25:
            url = pages_to_scan.pop(0)
            if url in seen_pages:
                continue
            seen_pages.add(url)
            try:
                response = self.get(url)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            page_items = self.parse_work_from_html(response.text, response.url, course, include_files=include_files)
            items.extend(page_items)
            for link in soup.find_all("a", href=True):
                href = urljoin(response.url, unescape(link["href"]))
                if self.extract_content_id(href) and href not in seen_pages:
                    pages_to_scan.append(href)
        return items

    def parse_work_from_html(
        self,
        html: str,
        page_url: str,
        course: dict[str, Any],
        include_files: bool = True,
    ) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[dict[str, Any]] = []
        for node in soup.find_all(["li", "tr", "article", "section", "div"], limit=1500):
            text = clean_text(node.get_text(" "))
            link = node.find("a", href=True)
            href = urljoin(page_url, unescape(link["href"])) if link else page_url
            if not text or (not self.looks_like_work(text) and not self.looks_like_file_link(href, text)):
                continue
            files = self.extract_files_from_html_node(node, page_url) if include_files else []
            title = clean_text(link.get_text(" ")) if link else text[:100]
            items.append(
                {
                    "course_id": course["course_id"],
                    "course_name": course.get("course_name"),
                    "content_id": self.extract_content_id(href),
                    "title": title or "Course content",
                    "kind": self.classify_work(text, href),
                    "content_type": "html",
                    "due_date": find_date_like_text(text),
                    "available_date": None,
                    "description": text[:1600],
                    "url": href,
                    "source": "html-content",
                    "files": files,
                }
            )
        return items

    def normalize_content_item(
        self,
        item: dict[str, Any],
        course: dict[str, Any],
        include_files: bool = True,
    ) -> dict[str, Any] | None:
        title = first_present(item.get("title"), item.get("name"), item.get("displayName"), item.get("label"))
        description = first_present(item.get("description"), item.get("body"), item.get("text"), item.get("instructions"), "")
        text_blob = clean_text(BeautifulSoup(str(description), "html.parser").get_text(" "))
        content_id = self.extract_content_id_from_value(item)
        url = self.content_url(item, course["course_id"], content_id)
        if not title and not text_blob and not url:
            return None

        due_date = first_present(
            item.get("dueDate"),
            item.get("due_date"),
            item.get("deadline"),
            item.get("dateDue"),
            self.find_value(item, ("dueDate", "due_date", "deadline", "dateDue")),
            find_date_like_text(f"{title or ''} {text_blob}"),
        )
        available_date = first_present(
            item.get("created"),
            item.get("createdAt"),
            item.get("start"),
            item.get("availableFrom"),
            self.find_value(item, ("availableFrom", "created", "createdAt", "start")),
        )
        raw_type = clean_text(first_present(item.get("contentHandler"), item.get("contentType"), item.get("type"), item.get("resourceType"), ""))
        files = self.extract_attachments(item) if include_files else []
        return {
            "course_id": course["course_id"],
            "course_name": course.get("course_name"),
            "content_id": content_id,
            "title": clean_text(title or text_blob[:100] or "Course content"),
            "kind": self.classify_work(f"{title or ''} {text_blob} {raw_type}", url or ""),
            "content_type": raw_type or None,
            "due_date": due_date,
            "available_date": available_date,
            "description": text_blob[:1600],
            "url": url,
            "source": "content-api",
            "files": files,
        }

    def fetch_course_announcements(self, course: dict[str, Any]) -> list[dict[str, Any]]:
        course_id = str(course["course_id"])
        announcements: list[dict[str, Any]] = []
        for path in (
            f"learn/api/public/v1/courses/{course_id}/announcements?limit=100",
            f"learn/api/v1/courses/{course_id}/announcements?limit=100",
        ):
            try:
                response = self.get(urljoin(BASE_URL, path), accept_json=True)
            except requests.RequestException:
                continue
            for item in extract_list(safe_json(response)):
                title = first_present(item.get("title"), item.get("subject"), item.get("name"), "Announcement")
                body = first_present(item.get("body"), item.get("message"), item.get("description"), "")
                url = first_present(item.get("url"), item.get("webUrl"), self.attachment_url(item))
                announcements.append(
                    {
                        "course_id": course_id,
                        "course_name": course.get("course_name"),
                        "announcement_id": first_present(item.get("id"), item.get("announcementId")),
                        "title": clean_text(title),
                        "body": clean_text(BeautifulSoup(str(body), "html.parser").get_text(" "))[:2000],
                        "created": first_present(item.get("created"), item.get("createdAt"), item.get("postedDate")),
                        "url": urljoin(BASE_URL, str(url)) if url else None,
                        "source": "announcements-api",
                    }
                )
        if announcements:
            return announcements
        return self.fetch_announcements_from_html(course)

    def fetch_announcements_from_html(self, course: dict[str, Any]) -> list[dict[str, Any]]:
        course_id = str(course["course_id"])
        urls = (
            urljoin(BASE_URL, f"webapps/blackboard/execute/announcement?method=search&context=course_entry&course_id={course_id}"),
            urljoin(BASE_URL, f"ultra/courses/{course_id}/announcements"),
        )
        announcements: list[dict[str, Any]] = []
        for url in urls:
            try:
                response = self.get(url)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for node in soup.find_all(["li", "article", "div"], limit=700):
                text = clean_text(node.get_text(" "))
                if len(text) < 20:
                    continue
                link = node.find("a", href=True)
                title = clean_text(link.get_text(" ")) if link else text[:100]
                if not re.search(r"\b(announcement|posted|course|assignment|واجب|اعلان|إعلان)\b", text, re.I):
                    continue
                announcements.append(
                    {
                        "course_id": course_id,
                        "course_name": course.get("course_name"),
                        "announcement_id": self.extract_content_id(link["href"]) if link else None,
                        "title": title,
                        "body": text[:2000],
                        "created": find_date_like_text(text),
                        "url": urljoin(response.url, link["href"]) if link else response.url,
                        "source": "announcements-html",
                    }
                )
        return announcements

    def parse_deadlines_from_html(self, html: str, page_url: str, course: dict[str, Any]) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        deadlines: list[dict[str, Any]] = []
        for node in soup.find_all(["li", "tr", "article", "div"], limit=1000):
            text = clean_text(node.get_text(" "))
            if not text or not re.search(r"\b(due|deadline|assignment|quiz|test|exam|واجب|اختبار)\b", text, re.I):
                continue
            link = node.find("a", href=True)
            title = clean_text(link.get_text(" ")) if link else text[:100]
            if not title:
                continue
            deadlines.append(
                {
                    "course_id": course["course_id"],
                    "course_name": course.get("course_name"),
                    "assignment_title": title,
                    "due_date": find_date_like_text(text),
                    "description": text[:1200],
                    "url": urljoin(page_url, link["href"]) if link else page_url,
                    "source": "html",
                }
            )
        return deadlines

    def normalize_deadline(self, item: dict[str, Any], course_names: dict[str, str | None]) -> dict[str, Any] | None:
        title = first_present(item.get("title"), item.get("name"), item.get("summary"))
        if not title:
            return None

        course = item.get("course") if isinstance(item.get("course"), dict) else {}
        course_id = first_present(item.get("courseId"), item.get("course_id"), course.get("id"), self.extract_course_id(str(item.get("url") or item.get("href") or "")))
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        self_link = links.get("self", {}) if isinstance(links.get("self"), dict) else {}
        url = first_present(item.get("url"), item.get("href"), item.get("webUrl"), self_link.get("href"))
        description = first_present(item.get("description"), item.get("body"), item.get("details"), "")

        return {
            "course_id": course_id,
            "course_name": first_present(item.get("courseName"), course.get("name"), course_names.get(str(course_id))),
            "assignment_title": clean_text(title),
            "due_date": first_present(item.get("dueDate"), item.get("due"), item.get("end"), item.get("start")),
            "description": clean_text(BeautifulSoup(str(description), "html.parser").get_text(" "))[:1200],
            "url": urljoin(BASE_URL, str(url)) if url else None,
            "source": "api",
        }

    def fetch_content(self, course_id: str, content_id: str) -> Any:
        candidates = (
            f"learn/api/public/v1/courses/{course_id}/contents/{content_id}",
            f"learn/api/v1/courses/{course_id}/contents/{content_id}",
        )
        last_error: Exception | None = None
        for path in candidates:
            try:
                response = self.get(urljoin(BASE_URL, path), accept_json=True)
                body = safe_json(response)
                return body if body is not None else response.text
            except requests.RequestException as exc:
                last_error = exc
        if last_error:
            raise BlackboardError(f"Could not fetch content item: {last_error}") from last_error
        raise BlackboardError("Could not fetch content item.")

    def find_attachment(self, content: Any, file_id: str) -> dict[str, str | None] | None:
        for node in self.walk_json(content):
            if not isinstance(node, dict):
                continue
            values = {str(value) for value in node.values() if value is not None and not isinstance(value, (dict, list))}
            if file_id not in values and not any(file_id in value for value in values):
                continue
            url = self.attachment_url(node)
            if url:
                return {
                    "url": urljoin(BASE_URL, url),
                    "file_name": first_present(node.get("fileName"), node.get("filename"), node.get("name"), node.get("title")),
                    "mime_type": first_present(node.get("mimeType"), node.get("contentType"), node.get("mediaType")),
                }
        return None

    def find_attachment_from_html(self, course_id: str, content_id: str, file_id: str) -> dict[str, str | None] | None:
        urls = (
            urljoin(BASE_URL, f"webapps/blackboard/content/listContent.jsp?course_id={course_id}&content_id={content_id}"),
            urljoin(BASE_URL, f"ultra/courses/{course_id}/outline/assessment/{content_id}"),
            urljoin(BASE_URL, f"ultra/courses/{course_id}/outline/file/{content_id}"),
        )
        for url in urls:
            try:
                response = self.get(url)
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = unescape(link["href"])
                text = clean_text(link.get_text(" "))
                if file_id not in href and file_id not in text:
                    continue
                return {"url": urljoin(response.url, href), "file_name": text or file_id, "mime_type": None}
        return None

    def attachment_url(self, node: dict[str, Any]) -> str | None:
        for key in ("downloadUrl", "download_url", "url", "href", "webUrl", "viewUrl"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        links = node.get("links")
        if isinstance(links, dict):
            for value in links.values():
                if isinstance(value, dict) and isinstance(value.get("href"), str):
                    return value["href"]
        return None

    def walk_json(self, value: Any) -> list[Any]:
        nodes = [value]
        if isinstance(value, dict):
            for child in value.values():
                nodes.extend(self.walk_json(child))
        elif isinstance(value, list):
            for child in value:
                nodes.extend(self.walk_json(child))
        return nodes

    def extract_attachments(self, value: Any) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        seen: set[tuple[str | None, str | None]] = set()
        for node in self.walk_json(value):
            if not isinstance(node, dict):
                continue
            url = self.attachment_url(node)
            file_name = first_present(node.get("fileName"), node.get("filename"), node.get("name"), node.get("title"))
            file_id = first_present(
                node.get("id"),
                node.get("fileId"),
                node.get("file_id"),
                node.get("attachmentId"),
                self.extract_file_id(str(url or "")),
            )
            if not url and not file_name:
                continue
            if not self.looks_like_file_link(str(url or ""), str(file_name or "")):
                continue
            key = (str(file_id) if file_id else None, str(url) if url else None)
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                {
                    "file_id": str(file_id) if file_id else str(file_name or url),
                    "file_name": clean_text(file_name or Path(urlparse(str(url)).path).name or "attachment"),
                    "mime_type": first_present(node.get("mimeType"), node.get("contentType"), node.get("mediaType")),
                    "url": urljoin(BASE_URL, str(url)) if url else None,
                }
            )
        return attachments

    def extract_files_from_html_node(self, node: Any, page_url: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in node.find_all("a", href=True):
            href = urljoin(page_url, unescape(link["href"]))
            text = clean_text(link.get_text(" "))
            if not self.looks_like_file_link(href, text):
                continue
            file_id = self.extract_file_id(href) or text or href
            if str(file_id) in seen:
                continue
            seen.add(str(file_id))
            files.append(
                {
                    "file_id": str(file_id),
                    "file_name": text or Path(urlparse(href).path).name or str(file_id),
                    "mime_type": mimetypes.guess_type(text or href)[0],
                    "url": href,
                }
            )
        return files

    def content_url(self, item: dict[str, Any], course_id: str, content_id: str | None) -> str | None:
        url = first_present(item.get("url"), item.get("href"), item.get("webUrl"), item.get("launchUrl"), self.attachment_url(item))
        if url:
            return urljoin(BASE_URL, str(url))
        if content_id:
            return urljoin(BASE_URL, f"webapps/blackboard/content/listContent.jsp?course_id={course_id}&content_id={content_id}")
        return None

    def extract_content_id_from_value(self, value: Any) -> str | None:
        if isinstance(value, dict):
            content_id = first_present(
                value.get("id"),
                value.get("contentId"),
                value.get("content_id"),
                value.get("itemId"),
                value.get("item_id"),
            )
            if content_id:
                return str(content_id)
            for key in ("url", "href", "webUrl", "launchUrl"):
                if isinstance(value.get(key), str):
                    content_id = self.extract_content_id(value[key])
                    if content_id:
                        return content_id
        elif isinstance(value, str):
            return self.extract_content_id(value)
        return None

    def extract_content_id(self, value: str) -> str | None:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        for key in ("content_id", "contentId", "item_id", "itemId", "assessment_id", "assessmentId"):
            if query.get(key):
                return query[key][0]
        for pattern in (
            r"/contents/([^/?#]+)",
            r"/content/([^/?#]+)",
            r"/outline/(?:assessment|file|content)/([^/?#]+)",
            r"content_id=([^&#]+)",
            r"contentId=([^&#]+)",
        ):
            match = re.search(pattern, value)
            if match:
                return match.group(1)
        return None

    def extract_file_id(self, value: str) -> str | None:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        for key in ("file_id", "fileId", "attachment_id", "attachmentId", "xid"):
            if query.get(key):
                return query[key][0]
        for pattern in (
            r"/files/([^/?#]+)",
            r"/attachments/([^/?#]+)",
            r"file_id=([^&#]+)",
            r"fileId=([^&#]+)",
            r"xid=([^&#]+)",
        ):
            match = re.search(pattern, value)
            if match:
                return match.group(1)
        return None

    def looks_like_work(self, text: str) -> bool:
        return any(keyword.lower() in clean_text(text).lower() for keyword in WORK_KEYWORDS)

    def looks_like_file_link(self, href: str, text: str) -> bool:
        combined = f"{href} {text}".lower()
        if re.search(r"\.(pdf|docx?|pptx?|xlsx?|csv|txt|zip|rar|png|jpe?g)(?:[?#]|$)", combined):
            return True
        return any(marker in combined for marker in ("download", "attachment", "bbcswebdav", "file", "resource/x-bb-file"))

    def classify_work(self, text: str, url: str = "") -> str:
        combined = f"{text} {url}".lower()
        if any(word in combined for word in ("assignment", "homework", "submit", "submission", "turnitin", "واجب", "تسليم")):
            return "assignment"
        if any(word in combined for word in ("quiz", "test", "exam", "assessment", "اختبار")):
            return "assessment"
        if self.looks_like_file_link(url, text):
            return "file"
        return "content"

    def dedupe_work(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, Any, Any, Any]] = set()
        unique: list[dict[str, Any]] = []
        for item in items:
            key = (item.get("course_id"), item.get("content_id"), item.get("title"), item.get("url"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        unique.sort(
            key=lambda item: (
                parse_datetime(item.get("due_date")) is None,
                parse_datetime(item.get("due_date")) or datetime.max.replace(tzinfo=UTC),
                item.get("course_name") or "",
                item.get("title") or "",
            )
        )
        return unique

    def build_login_payload(self, form: Any) -> dict[str, str]:
        payload: dict[str, str] = {}
        for field in form.find_all(["input", "button"]):
            name = field.get("name")
            if not name:
                continue
            field_type = (field.get("type") or "").lower()
            if field_type in {"checkbox", "radio"} and not field.has_attr("checked"):
                continue
            if field_type in {"button", "reset"}:
                continue
            payload[name] = field.get("value", "")

        username_field = self.find_username_field(form)
        password_field = form.find("input", {"type": re.compile("^password$", re.I)})
        payload[username_field] = self.username or ""
        payload[password_field.get("name") if password_field and password_field.get("name") else "password"] = self.password or ""
        return payload

    def find_username_field(self, form: Any) -> str:
        names = ("user_id", "username", "j_username", "login", "userid", "user", "email")
        fields = form.find_all("input")
        by_name = {field.get("name", "").lower(): field.get("name") for field in fields if field.get("name")}
        for name in names:
            if name in by_name:
                return str(by_name[name])

        password_input = form.find("input", {"type": re.compile("^password$", re.I)})
        password_index = fields.index(password_input) if password_input in fields else len(fields)
        for field in reversed(fields[:password_index]):
            field_type = (field.get("type") or "text").lower()
            if field_type in {"text", "email", "search", ""} and field.get("name"):
                return str(field["name"])
        return "user_id"

    def find_login_form(self, html: str) -> Any | None:
        soup = BeautifulSoup(html, "html.parser")
        for form in soup.find_all("form"):
            if form.find("input", {"type": re.compile("^password$", re.I)}):
                return form
        return None

    def login_failed(self, response: requests.Response) -> bool:
        text = clean_text(response.text).lower()
        return any(
            marker in text
            for marker in (
                "invalid username",
                "invalid password",
                "incorrect username",
                "login failed",
                "authentication failed",
                "كلمة المرور غير",
            )
        )

    def looks_authenticated(self, response: requests.Response) -> bool:
        text = response.text.lower()
        return any(marker in text for marker in ("logout", "sign out", "my courses", "activity stream", "ultra"))

    def extract_course_id(self, value: str) -> str | None:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        for key in ("course_id", "courseId", "course_id_string"):
            if query.get(key):
                return query[key][0]
        for pattern in (r"/courses/([^/?#]+)", r"/course/([^/?#]+)", r"course_id=([^&#]+)", r"courseId=([^&#]+)"):
            match = re.search(pattern, value)
            if match:
                return match.group(1)
        return None

    def dedupe_deadlines(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[Any, Any, Any]] = set()
        unique: list[dict[str, Any]] = []
        for item in items:
            key = (item.get("course_id"), item.get("assignment_title"), item.get("due_date"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def file_name_from_response(self, response: requests.Response, fallback: str) -> str:
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
        if match:
            return clean_text(match.group(1))
        path_name = Path(urlparse(response.url).path).name
        return path_name or fallback

    def find_value(self, value: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if key in value:
                    return value[key]
            for child in value.values():
                found = self.find_value(child, keys)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = self.find_value(child, keys)
                if found is not None:
                    return found
        return None

    def get(self, url: str, accept_json: bool = False, **kwargs: Any) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if accept_json:
            headers = {"Accept": "application/json, text/javascript, */*;q=0.8", **headers}
        response = self.session.get(url, timeout=REQUEST_TIMEOUT, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.post(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, **kwargs)
        response.raise_for_status()
        return response


mcp = FastMCP(
    "uoh-blackboard",
    host=os.getenv("HOST", "0.0.0.0"),
    port=int(os.getenv("PORT", "7860")),
    sse_path="/sse",
    message_path="/messages/",
)
app = mcp.sse_app()


@mcp.tool()
def get_deadlines(username: str | None = None, password: str | None = None) -> str:
    """Read active UoH Blackboard deadlines and upcoming assignment-like items."""
    return json_response(BlackboardClient(username, password).get_deadlines())


@mcp.tool()
def list_courses(username: str | None = None, password: str | None = None) -> str:
    """List active Blackboard courses so an assistant can choose a course_id."""
    return json_response(BlackboardClient(username, password).list_courses())


@mcp.tool()
def get_course_work(
    course_id: str | None = None,
    include_files: bool = True,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """List assignment-like course content, due dates, content IDs, and file IDs."""
    return json_response(BlackboardClient(username, password).get_course_work(course_id, include_files))


@mcp.tool()
def get_announcements(course_id: str | None = None, username: str | None = None, password: str | None = None) -> str:
    """Read Blackboard announcements, optionally for one course."""
    return json_response(BlackboardClient(username, password).get_announcements(course_id))


@mcp.tool()
def profile_scraper(username: str | None = None, password: str | None = None) -> str:
    """Report what the scraper can currently see across courses, work items, files, and announcements."""
    return json_response(BlackboardClient(username, password).profile_scraper())


@mcp.tool()
def download_assignment_file(
    course_id: str,
    content_id: str,
    file_id: str,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Download an assignment attachment and return it as base64 JSON."""
    return json_response(BlackboardClient(username, password).download_assignment_file(course_id, content_id, file_id))


@mcp.tool()
def submit_assignment(
    course_id: str,
    content_id: str,
    file_name: str,
    file_base64_content: str,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Submit a base64-encoded solution file to a Blackboard assignment."""
    return json_response(
        BlackboardClient(username, password).submit_assignment(
            course_id,
            content_id,
            file_name,
            file_base64_content,
        )
    )


if __name__ == "__main__":
    mcp.run(transport="sse")
