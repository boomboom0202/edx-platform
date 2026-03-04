"""
Decorators for courseware views.
"""
import functools

from django.shortcuts import redirect
from opaque_keys.edx.keys import CourseKey
from openedx_filters.learning.filters import CoursewareViewStarted


def courseware_view_hooks(view_func):
    """
    Decorator that calls the CoursewareViewStarted filter before rendering a courseware view.

    If any pipeline step raises ``CoursewareViewStarted.RedirectToUrl``, the user is
    redirected to that URL. Otherwise, the original view is rendered normally.

    Usage::

        @courseware_view_hooks
        def my_view(request, course_id, ...):
            ...

    Works with both function-based views and ``method_decorator``-wrapped class-based views.
    The decorator reads the ``course_id`` keyword argument from the view's URL kwargs.
    """
    @functools.wraps(view_func)
    def _wrapper(*args, **kwargs):
        course_id = kwargs.get('course_id')
        try:
            course_key = CourseKey.from_string(str(course_id)) if course_id else None
        except Exception:  # pylint: disable=broad-except
            course_key = None

        if course_key is not None:
            try:
                CoursewareViewStarted.run_filter(course_key=course_key)
            except CoursewareViewStarted.RedirectToUrl as exc:
                return redirect(exc.url)

        return view_func(*args, **kwargs)

    return _wrapper
