"""
The support form page.

A filled-in form becomes one email to CONTACT_EMAIL, the support mailbox, with
the learner's address as Reply-To so the center can simply answer it. Nothing
is stored. The learner gets no copy: sending mail to whatever address a form
was given would make the page a way to spam strangers from the university.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from common.djangoapps.edxmako.shortcuts import render_to_response

from .forms import ROLES, TOPICS, SupportForm, topic_label

log = logging.getLogger(__name__)

# The center reads these letters in Russian, whatever language the page was in.
STAFF_LANGUAGE = "ru"


@require_http_methods(["GET", "POST"])
@ratelimit(key="openedx.core.djangoapps.util.ratelimit.real_ip", rate="5/h", method="POST", block=False)
def ask(request):
    user = request.user if request.user.is_authenticated else None
    error = None

    if request.method == "POST":
        form = SupportForm(request.POST, request.FILES)
        if getattr(request, "limited", False):
            error = _("Too many requests from your network. Please try again in an hour.")
        elif form.is_valid():
            if form.cleaned_data["website"]:
                log.info("Support form: dropped a submission that filled the honeypot")
                return redirect(reverse("ektu_support:ask") + "?sent=1")
            try:
                _send(form.cleaned_data, user, request)
            except Exception:  # pylint: disable=broad-except
                log.exception("Support form: could not send the email")
                error = _("The message could not be sent. Please try again later or write to us by email.")
            else:
                return redirect(reverse("ektu_support:ask") + "?sent=1")
    else:
        initial = {}
        if user is not None:
            initial = {"name": user.profile.name if hasattr(user, "profile") else "", "email": user.email}
        form = SupportForm(initial=initial)

    return render_to_response("ektu/support_form.html", {
        "form": form,
        "roles": ROLES,
        "topics": TOPICS,
        "error": error,
        "sent": request.method == "GET" and request.GET.get("sent") == "1",
        "signed_in": user is not None,
    }, request=request)


def _send(data, user, request):
    with translation.override(STAFF_LANGUAGE):
        role = dict(ROLES)[data["role"]]
        topic = topic_label(data["topic"])
        subject = f"[{settings.PLATFORM_NAME}] {topic} — {data['name']}"
        lines = [
            f"{_('Who')}: {role}",
            f"{_('Topic')}: {topic}",
            f"{_('Name')}: {data['name']}",
            f"{_('Email')}: {data['email']}",
        ]
        if user is not None:
            lines.append(f"{_('Account')}: {user.username} (id {user.id}, {user.email})")
        else:
            lines.append(f"{_('Account')}: {_('not signed in')}")
        if data["course"]:
            lines.append(f"{_('Course')}: {data['course']}")
        lines += [
            f"{_('Language of the page')}: {request.LANGUAGE_CODE}",
            f"{_('Browser')}: {request.META.get('HTTP_USER_AGENT', '')[:300]}",
            "",
            data["message"],
        ]

    message = EmailMessage(
        subject=subject[:200],
        body="\n".join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[settings.CONTACT_EMAIL],
        reply_to=[data["email"]],
    )
    for upload in data["screenshots"]:
        message.attach(upload.name, upload.read(), upload.content_type)
    message.send(fail_silently=False)
    log.info("Support form: sent %s from %s", data["topic"], user.id if user else "anonymous")
