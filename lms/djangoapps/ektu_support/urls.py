"""URLs for the support form, mounted under /help/ask/."""
from django.urls import path

from . import views

app_name = "ektu_support"

urlpatterns = [
    path("", views.ask, name="ask"),
]
