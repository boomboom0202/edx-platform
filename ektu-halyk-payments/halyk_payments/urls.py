"""URLs for the Halyk payment flow, mounted by the Tutor plugin under /halyk/."""
from django.urls import path, re_path

from . import views

app_name = "halyk_payments"

urlpatterns = [
    re_path(r"^checkout/(?P<course_id>[^/]+)/$", views.checkout, name="checkout"),
    path("postlink/", views.postlink, name="postlink"),
    re_path(r"^result/(?P<invoice_id>[\w-]+)/$", views.result, name="result"),
    re_path(r"^receipt/(?P<invoice_id>[\w-]+)/$", views.receipt, name="receipt"),
]
