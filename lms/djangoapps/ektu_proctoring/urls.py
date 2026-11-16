"""URLs of the proctoring service, mounted under /proctoring/."""
from django.urls import path

from . import views_api, views_capture, views_review

app_name = "ektu_proctoring"

urlpatterns = [
    # The recording page and what it sends.
    path("resume/", views_capture.resume, name="resume"),
    path("s/<str:token>/", views_capture.capture, name="capture"),
    path("s/<str:token>/start/", views_capture.start, name="start"),
    path("s/<str:token>/chunk/", views_capture.chunk, name="chunk"),
    path("s/<str:token>/photo/", views_capture.photo, name="photo"),
    path("s/<str:token>/event/", views_capture.event, name="event"),
    path("s/<str:token>/heartbeat/", views_capture.heartbeat, name="heartbeat"),
    path("s/<str:token>/finish/", views_capture.finish, name="finish"),
    # Review by staff.
    path("review/", views_review.review_list, name="review_list"),
    path("review/<uuid:key>/", views_review.review, name="review"),
    path("review/<uuid:key>/media/<int:recording_id>/", views_review.media, name="media"),
    # REST API for other systems.
    path("api/v1/sessions/", views_api.sessions, name="api_sessions"),
    path("api/v1/sessions/<uuid:key>/", views_api.session, name="api_session"),
    path("api/v1/sessions/<uuid:key>/exam-started/", views_api.exam_started, name="api_exam_started"),
    path("api/v1/sessions/<uuid:key>/finish/", views_api.finish, name="api_finish"),
    path("api/v1/sessions/<uuid:key>/review/", views_api.review, name="api_review"),
]
