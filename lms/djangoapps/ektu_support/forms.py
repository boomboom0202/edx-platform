"""
The support form.

Who is asking decides which topics make sense, so every topic belongs to one
role. The page only shows the topics of the chosen role, but that is a
convenience: the form checks the pair itself, because the browser can send
anything.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

ROLE_STUDENT = "student"
ROLE_TEACHER = "teacher"

ROLES = [
    (ROLE_STUDENT, _("I am a learner")),
    (ROLE_TEACHER, _("I am a teacher")),
]

# (value, label) per role. A value is "<role>:<topic>" so it can never be
# mistaken for the same topic of the other role.
TOPICS = {
    ROLE_STUDENT: [
        ("student:sign_in", _("Signing in, registration and my account")),
        ("student:enrollment", _("Enrolling in a course and access to it")),
        ("student:payment", _("Paying for a course")),
        ("student:content", _("Videos, materials or assignments do not open")),
        ("student:grades", _("Grades, deadlines and assignment attempts")),
        ("student:certificate", _("Certificates")),
        ("student:other", _("Something else")),
    ],
    ROLE_TEACHER: [
        ("teacher:studio", _("Creating a course and access to Studio")),
        ("teacher:content", _("Uploading videos and course materials")),
        ("teacher:assessment", _("Assignments, quizzes and grading")),
        ("teacher:learners", _("Learners, enrolment and cohorts")),
        ("teacher:certificate", _("Course certificates")),
        ("teacher:design", _("Methodological advice and instructional design")),
        ("teacher:other", _("Something else")),
    ],
}

MAX_SCREENSHOTS = 3
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
SCREENSHOT_FORMATS = {"PNG", "JPEG", "GIF", "WEBP"}


def topic_label(value):
    for topics in TOPICS.values():
        for topic, label in topics:
            if topic == value:
                return label
    return value


class SupportForm(forms.Form):
    role = forms.ChoiceField(choices=ROLES, widget=forms.RadioSelect)
    topic = forms.ChoiceField(
        choices=[topic for topics in TOPICS.values() for topic in topics],
    )
    name = forms.CharField(max_length=150)
    email = forms.EmailField(max_length=254)
    course = forms.CharField(max_length=300, required=False)
    message = forms.CharField(max_length=5000, widget=forms.Textarea)
    # Hidden from people by CSS; bots that fill every field give themselves
    # away here.
    website = forms.CharField(required=False)

    def clean_topic(self):
        topic = self.cleaned_data["topic"]
        role = self.data.get("role")
        if not topic.startswith(f"{role}:"):
            raise forms.ValidationError(_("Choose a topic from the list for your role."))
        return topic

    def clean(self):
        cleaned = super().clean()
        cleaned["screenshots"] = self._clean_screenshots()
        return cleaned

    def _clean_screenshots(self):
        from PIL import Image

        files = self.files.getlist("screenshots") if self.files else []
        files = [f for f in files if f and f.size]
        if len(files) > MAX_SCREENSHOTS:
            self.add_error(None, _("You can attach up to {count} screenshots.").format(
                count=MAX_SCREENSHOTS))
            return []

        checked = []
        for upload in files:
            if upload.size > MAX_SCREENSHOT_BYTES:
                self.add_error(None, _("{name} is larger than 5 MB.").format(name=upload.name))
                continue
            try:
                image = Image.open(upload)
                image_format = image.format
                image.verify()
            except Exception:  # pylint: disable=broad-except
                image_format = None
            if image_format not in SCREENSHOT_FORMATS:
                self.add_error(None, _("{name} is not an image (PNG, JPG, GIF or WebP).").format(
                    name=upload.name))
                continue
            upload.seek(0)
            checked.append(upload)
        return checked
