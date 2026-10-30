"""URLs for the course log, mounted under /course-log/."""
from django.urls import path, re_path

from . import views

app_name = "ektu_audit"

urlpatterns = [
    path("", views.feed, name="feed"),
    re_path(r"^change/(?P<version_id>[0-9a-f]{24})/$", views.change, name="change"),
    path("uploads/", views.uploads, name="uploads"),
    path("people/", views.people, name="people"),
]
