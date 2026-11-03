"""
The Edufusion course catalogue.

A course belongs here by being in the EDUFUSION organization. It stays out of
the main catalog and the home page through the platform's own setting, "Course
Visibility In Catalog" set to "about" in Studio's Advanced Settings. That also
hides it from get_courses(), so this page asks for the organization directly.
A course set to "none" stays hidden here too.

A project course that lives in another organization is listed explicitly in
OTHER_COURSES, with the course number whose place it takes in the order.
"""
from common.djangoapps.edxmako.shortcuts import render_to_response
from lms.djangoapps import branding
from xmodule.course_block import CATALOG_VISIBILITY_NONE

EDUFUSION_ORG = "EDUFUSION"
OTHER_COURSES = {
    "course-v1:VKTU+PBLaMP+2026_C1": "EF07",
}


def _position(course):
    return OTHER_COURSES.get(str(course.id), course.display_number_with_default)


def catalogue(request):
    courses = (
        branding.get_visible_courses(org=EDUFUSION_ORG)
        | branding.get_visible_courses(course_keys=list(OTHER_COURSES))
    ).exclude(catalog_visibility=CATALOG_VISIBILITY_NONE)
    return render_to_response("ektu/edufusion_catalogue.html", {
        # Course numbers (EF01, EF02...) carry the order of the catalogue.
        "courses": sorted(courses, key=_position),
    }, request=request)
