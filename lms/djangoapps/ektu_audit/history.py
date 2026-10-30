"""
Reading the history of course content.

Studio never edits a course in place. Every save in the split modulestore
writes a whole new *structure*, a snapshot of the course's blocks, which points
back at the one before it (``previous_version``) and records who made it and
when (``edited_by``, ``edited_on``). Inside a structure, each block's
``edit_info.update_version`` names the structure in which that block last
changed, so the blocks a save touched are exactly those whose
``update_version`` is that save.

Every course has two such chains: the draft branch, which moves on every save
in Studio, and the published branch, which moves only on publish. The course
index points at the head of each; walking back from the heads tells which
course and branch every structure belongs to.

Block content (the HTML of a text block, the XML of a problem) is kept apart
from the structure, in *definitions*; a block points at its definition, and a
content edit points it at a new one.

Everything here only reads.
"""
import difflib
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from bson import ObjectId

DRAFT = "draft"
PUBLISHED = "published"
LIBRARY = "library"

CREATED = "created"
CHANGED = "changed"
DELETED = "deleted"

# ModuleStoreEnum.UserID: changes made by commands and the platform itself.
SYSTEM_USER_IDS = {-1, -2, -3, -4}

CACHE_SECONDS = 120
MAX_DIFF_LINES = 400
MAX_VALUE_CHARS = 500


@dataclass
class Version:
    id: ObjectId
    previous: Optional[ObjectId]
    edited_by: Optional[int]
    edited_on: datetime
    course_key: Optional[str]
    branch: Optional[str]

    @property
    def is_system(self):
        return self.edited_by is None or self.edited_by in SYSTEM_USER_IDS


@dataclass
class BlockChange:
    kind: str
    block_type: str
    block_id: str
    name: str = ""


@dataclass
class FieldChange:
    name: str
    before: str = ""
    after: str = ""
    diff: list = field(default_factory=list)


def _connection():
    from xmodule.modulestore import ModuleStoreEnum
    from xmodule.modulestore.django import modulestore

    return modulestore()._get_modulestore_by_type(  # pylint: disable=protected-access
        ModuleStoreEnum.Type.split
    ).db_connection


def _heads():
    """{structure id: (course key, branch)} for the head of every branch."""
    from common.djangoapps.split_modulestore_django.models import SplitModulestoreCourseIndex

    heads = {}
    rows = SplitModulestoreCourseIndex.objects.values_list(
        "course_id", "draft_version", "published_version", "library_version",
    )
    for course_id, draft, published, library in rows:
        for branch, head in ((DRAFT, draft), (PUBLISHED, published), (LIBRARY, library)):
            if head:
                heads[ObjectId(head)] = (str(course_id), branch)
    return heads


def assign_versions(docs, heads):
    """
    Turn raw structure documents into Versions that know their course and branch.

    Walks back from every branch head. A structure no head reaches (the
    history of a course that was re-imported, say) still gets the course of
    any structure sharing its ``original_version``, without a branch.
    """
    by_id = {doc["_id"]: doc for doc in docs}
    owner = {}
    for head, (course_key, branch) in heads.items():
        current = head
        while current is not None and current in by_id and current not in owner:
            owner[current] = (course_key, branch)
            current = by_id[current].get("previous_version")

    course_of_lineage = {}
    for structure_id, (course_key, _branch) in owner.items():
        course_of_lineage.setdefault(by_id[structure_id].get("original_version"), course_key)

    versions = []
    for doc in docs:
        course_key, branch = owner.get(
            doc["_id"], (course_of_lineage.get(doc.get("original_version")), None)
        )
        versions.append(Version(
            id=doc["_id"],
            previous=doc.get("previous_version"),
            edited_by=doc.get("edited_by"),
            edited_on=doc["edited_on"],
            course_key=course_key,
            branch=branch,
        ))
    versions.sort(key=lambda version: version.edited_on, reverse=True)
    return versions


_cache = {"at": 0.0, "versions": None}
_cache_lock = threading.Lock()


def all_versions():
    """Every structure, newest first. Cached briefly: there are tens of thousands."""
    with _cache_lock:
        if _cache["versions"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            docs = _connection().structures.find(
                {}, {"previous_version": 1, "original_version": 1, "edited_by": 1, "edited_on": 1},
            )
            _cache["versions"] = assign_versions(list(docs), _heads())
            _cache["at"] = time.monotonic()
        return _cache["versions"]


def get_version(version_id):
    for version in all_versions():
        if version.id == version_id:
            return version
    return None


_SUMMARY_FIELDS = {
    "blocks.block_type": 1,
    "blocks.block_id": 1,
    "blocks.fields.display_name": 1,
    "blocks.edit_info.update_version": 1,
}


def _blocks(structure_id, projection=None):
    if structure_id is None:
        return {}
    doc = _connection().structures.find_one({"_id": structure_id}, projection)
    if not doc:
        return {}
    return {(block["block_type"], block["block_id"]): block for block in doc.get("blocks", [])}


def diff_blocks(version_id, current, previous):
    """The blocks a save created, changed or removed, given both snapshots."""
    changes = []
    for key, block in current.items():
        if key not in previous:
            kind = CREATED
        elif block.get("edit_info", {}).get("update_version") == version_id:
            kind = CHANGED
        else:
            continue
        changes.append(BlockChange(kind, key[0], key[1], block.get("fields", {}).get("display_name") or ""))
    for key, block in previous.items():
        if key not in current:
            changes.append(BlockChange(DELETED, key[0], key[1], block.get("fields", {}).get("display_name") or ""))
    order = {CREATED: 0, CHANGED: 1, DELETED: 2}
    changes.sort(key=lambda change: (order[change.kind], change.block_type != "course"))
    return changes


def summarize(version):
    return diff_blocks(
        version.id,
        _blocks(version.id, _SUMMARY_FIELDS),
        _blocks(version.previous, _SUMMARY_FIELDS),
    )


def _text(value):
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def diff_fields(before, after, names=None):
    """Field-by-field changes between two field dicts; long text gets a line diff."""
    changes = []
    for name in sorted(set(before) | set(after)):
        if names is not None and name not in names:
            continue
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if isinstance(old, (str, type(None))) and isinstance(new, (str, type(None))) \
                and ("\n" in (old or "") or "\n" in (new or "") or len(old or "") + len(new or "") > 200):
            lines = list(difflib.unified_diff(
                (old or "").splitlines(), (new or "").splitlines(), lineterm="", n=2,
            ))[2:]
            if len(lines) > MAX_DIFF_LINES:
                lines = lines[:MAX_DIFF_LINES] + ["…"]
            changes.append(FieldChange(name, diff=lines))
        else:
            changes.append(FieldChange(name, before="" if old is None else _text(old),
                                       after="" if new is None else _text(new)))
    return changes


def _definition_fields(definition_id):
    if definition_id is None:
        return {}
    doc = _connection().definitions.find_one({"_id": definition_id}, {"fields": 1})
    return (doc or {}).get("fields", {})


def detail(version):
    """
    Each touched block with what changed in it: settings, content, children.

    Returns a list of (BlockChange, [FieldChange]).
    """
    current = _blocks(version.id)
    previous = _blocks(version.previous)
    result = []
    for change in diff_blocks(version.id, current, previous):
        key = (change.block_type, change.block_id)
        old, new = previous.get(key, {}), current.get(key, {})
        old_fields, new_fields = dict(old.get("fields", {})), dict(new.get("fields", {}))
        old_children = old_fields.pop("children", None) or []
        new_children = new_fields.pop("children", None) or []

        fields = diff_fields(old_fields, new_fields)
        if old.get("definition") != new.get("definition"):
            fields += diff_fields(
                _definition_fields(old.get("definition")),
                _definition_fields(new.get("definition")),
            )
        if [tuple(c) for c in old_children] != [tuple(c) for c in new_children]:
            names = {}
            for blocks in (previous, current):
                for child_key, block in blocks.items():
                    names[child_key] = block.get("fields", {}).get("display_name") or child_key[1]
            added = [names.get(tuple(c), c[1]) for c in new_children if c not in old_children]
            removed = [names.get(tuple(c), c[1]) for c in old_children if c not in new_children]
            fields.append(FieldChange(
                "children",
                before=", ".join(removed),
                after=", ".join(added),
            ))
        result.append((change, fields))
    return result
