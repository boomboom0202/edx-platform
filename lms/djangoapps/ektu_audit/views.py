"""
The course log pages. Staff only: they show who edited what across all courses.
"""
from datetime import datetime, time, timedelta, timezone
from functools import wraps

from bson import ObjectId
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

from common.djangoapps.edxmako.shortcuts import render_to_response
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from . import history, uploads as uploads_reader

# Times are shown in Kazakhstan time (UTC+5 since 2024), where the staff are.
LOCAL_TIME = timezone(timedelta(hours=5), "UTC+5")
PAGE_SIZE = 30


def staff_only(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def block_type_label(block_type):
    labels = {
        "course": _("Course settings"),
        "chapter": _("Section"),
        "sequential": _("Subsection"),
        "vertical": _("Unit"),
        "html": _("Text"),
        "video": _("Video"),
        "problem": _("Problem"),
        "discussion": _("Discussion"),
        "about": _("About page"),
        "static_tab": _("Custom page"),
        "course_info": _("Course updates"),
        "pdf": _("PDF"),
        "openassessment": _("Open response assessment"),
        "library_content": _("Library content"),
        "drag-and-drop-v2": _("Drag and drop"),
    }
    return labels.get(block_type, block_type)


def kind_label(kind, branch):
    # Translators: what happened to a block of a course, shown in the staff course log.
    if branch == history.PUBLISHED:
        return {history.CREATED: pgettext("course log", "published (new)"),
                history.CHANGED: pgettext("course log", "published"),
                history.DELETED: pgettext("course log", "published removal")}[kind]
    return {history.CREATED: pgettext("course log", "created"),
            history.CHANGED: pgettext("course log", "changed"),
            history.DELETED: pgettext("course log", "deleted")}[kind]


def local(moment):
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TIME).strftime("%d.%m.%Y %H:%M")


def people_by_id(user_ids):
    ids = {user_id for user_id in user_ids if user_id is not None and user_id > 0}
    users = get_user_model().objects.filter(id__in=ids).select_related("profile")
    people = {}
    for user in users:
        name = getattr(getattr(user, "profile", None), "name", "") or ""
        people[user.id] = {"name": name or user.username, "username": user.username}
    return people


def person(people, user_id):
    if user_id is None or user_id in history.SYSTEM_USER_IDS:
        return {"name": _("System"), "username": "", "system": True}
    return people.get(user_id, {"name": _("User {id}").format(id=user_id), "username": ""})


def course_names(course_keys):
    keys = [key for key in course_keys if key and key.startswith("course-v1:")]
    return dict(
        (str(course_id), name) for course_id, name in
        CourseOverview.objects.filter(id__in=keys).values_list("id", "display_name")
    )


def parse_day(value, end=False):
    try:
        day = datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return None
    moment = datetime.combine(day + timedelta(days=1) if end else day, time.min, LOCAL_TIME)
    return moment.astimezone(timezone.utc)


def page_number(request):
    try:
        return max(1, int(request.GET.get("page", 1)))
    except ValueError:
        return 1


def page_links(request, page, has_next):
    params = request.GET.copy()
    links = {}
    if page > 1:
        params["page"] = page - 1
        links["newer"] = "?" + params.urlencode()
    if has_next:
        params["page"] = page + 1
        links["older"] = "?" + params.urlencode()
    return links


def course_choices(versions):
    keys = {version.course_key for version in versions if version.course_key}
    names = course_names(keys)
    choices = [(key, names.get(key, key)) for key in keys if key.startswith("course-v1:")]
    return sorted(choices, key=lambda choice: choice[1].lower())


@staff_only
def feed(request):
    versions = history.all_versions()
    course = request.GET.get("course") or ""
    try:
        editor = int(request.GET.get("user") or 0) or None
    except ValueError:
        editor = None
    branch = request.GET.get("branch") or ""
    hide_system = request.GET.get("system") != "1"
    date_from = parse_day(request.GET.get("from"))
    date_to = parse_day(request.GET.get("to"), end=True)

    def wanted(version):
        if version.branch == history.LIBRARY:
            return False
        if course and version.course_key != course:
            return False
        if editor and version.edited_by != editor:
            return False
        if branch and version.branch != branch:
            return False
        if hide_system and version.is_system:
            return False
        if date_from and version.edited_on.replace(tzinfo=timezone.utc) < date_from:
            return False
        if date_to and version.edited_on.replace(tzinfo=timezone.utc) >= date_to:
            return False
        return True

    matching = [version for version in versions if wanted(version)]
    page = page_number(request)
    shown = matching[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    editors = people_by_id({version.edited_by for version in versions})
    names = course_names({version.course_key for version in shown})
    rows = []
    for version in shown:
        changes = history.summarize(version)
        rows.append({
            "version": version,
            "when": local(version.edited_on),
            "who": person(editors, version.edited_by),
            "course": names.get(version.course_key, version.course_key or _("Unknown course")),
            "course_key": version.course_key,
            "changes": [
                {"kind": kind_label(change.kind, version.branch), "kind_code": change.kind,
                 "type": block_type_label(change.block_type), "name": change.name}
                for change in changes
            ],
        })

    editor_choices = sorted(
        ((user_id, info["name"]) for user_id, info in editors.items()),
        key=lambda choice: choice[1].lower(),
    )
    return render_to_response("ektu/course_log.html", {
        "tab": "feed",
        "rows": rows,
        "total": len(matching),
        "page": page,
        "links": page_links(request, page, page * PAGE_SIZE < len(matching)),
        "courses": course_choices(versions),
        "editors": editor_choices,
        "params": request.GET,
        "branch_labels": {history.DRAFT: _("Draft"), history.PUBLISHED: _("Published")},
    }, request=request)


@staff_only
def change(request, version_id):
    version = history.get_version(ObjectId(version_id))
    if version is None:
        raise Http404
    blocks = history.detail(version)
    editors = people_by_id({version.edited_by})
    names = course_names({version.course_key})
    return render_to_response("ektu/course_log.html", {
        "tab": "change",
        "version": version,
        "when": local(version.edited_on),
        "who": person(editors, version.edited_by),
        "course_name": names.get(version.course_key, version.course_key or _("Unknown course")),
        "course_key": version.course_key,
        "blocks": [
            {"kind": kind_label(change.kind, version.branch), "kind_code": change.kind,
             "type": block_type_label(change.block_type), "name": change.name,
             "block_id": change.block_id, "fields": fields}
            for change, fields in blocks
        ],
        "branch_labels": {history.DRAFT: _("Draft"), history.PUBLISHED: _("Published")},
    }, request=request)


@staff_only
def uploads(request):
    course = request.GET.get("course") or ""
    page = page_number(request)
    items, total = uploads_reader.list_uploads(
        course_key=course or None,
        date_from=parse_day(request.GET.get("from")),
        date_to=parse_day(request.GET.get("to"), end=True),
        offset=(page - 1) * PAGE_SIZE,
        limit=PAGE_SIZE,
    )
    uploaders = people_by_id({item.uploader_id for item in items})
    names = course_names({item.course_key for item in items})
    rows = [{
        "upload": item,
        "when": local(item.uploaded_at),
        "who": uploaders.get(item.uploader_id) if item.uploader_id else None,
        "course": names.get(item.course_key, item.course_key or ""),
        "size": size_label(item.size),
    } for item in items]
    return render_to_response("ektu/course_log.html", {
        "tab": "uploads",
        "rows": rows,
        "total": total,
        "page": page,
        "links": page_links(request, page, page * PAGE_SIZE < total),
        "courses": course_choices(history.all_versions()),
        "params": request.GET,
    }, request=request)


@staff_only
def people(request):
    versions = [version for version in history.all_versions()
                if not version.is_system and version.branch in (history.DRAFT, history.PUBLISHED)]
    since = parse_day(request.GET.get("from"))
    if since:
        versions = [version for version in versions if version.edited_on.replace(tzinfo=timezone.utc) >= since]
    stats = {}
    for version in versions:
        entry = stats.setdefault(version.edited_by, {
            "saves": 0, "publishes": 0, "courses": set(), "last": version.edited_on,
        })
        if version.branch == history.PUBLISHED:
            entry["publishes"] += 1
        else:
            entry["saves"] += 1
        if version.course_key:
            entry["courses"].add(version.course_key)
        entry["last"] = max(entry["last"], version.edited_on)
    editors = people_by_id(stats)
    names = course_names({key for entry in stats.values() for key in entry["courses"]})
    rows = sorted(({
        "user_id": user_id,
        "who": person(editors, user_id),
        "saves": entry["saves"],
        "publishes": entry["publishes"],
        "courses": sorted(names.get(key, key) for key in entry["courses"]),
        "last": local(entry["last"]),
        "last_sort": entry["last"],
    } for user_id, entry in stats.items()), key=lambda row: row["last_sort"], reverse=True)
    return render_to_response("ektu/course_log.html", {
        "tab": "people",
        "rows": rows,
        "params": request.GET,
    }, request=request)


def size_label(size):
    if size >= 1024 * 1024:
        return _("{size} MB").format(size=f"{size / 1024 / 1024:.1f}")
    return _("{size} KB").format(size=max(1, round(size / 1024)))
