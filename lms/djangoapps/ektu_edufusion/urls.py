"""URLs for the Edufusion catalogue, mounted under /edufusion/."""
from django.urls import path

from . import views

app_name = "ektu_edufusion"

urlpatterns = [
    path("", views.catalogue, name="catalogue"),
]
