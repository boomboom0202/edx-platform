"""
Move courses and learners off the audit track, which cannot earn a certificate.

``audit`` is the one enrolment mode Open edX refuses to issue a certificate for
(see ``CourseMode.is_eligible_for_certificate``). A free course offered as audit
therefore lets learners pass and then quietly gives them nothing — no error, no
log line, just no certificate. ``honor`` is equally free and does earn one.

Two things have to change, and doing only one of them fixes nothing:

* the course's mode, or new enrolments keep landing in audit;
* the existing enrolments, which a change to the course does not touch.

    ./manage.py lms audit_to_honor                      # report only
    ./manage.py lms audit_to_honor --commit
    ./manage.py lms audit_to_honor --courses course-v1:LNG+RYaI_RU_01+2026_FEB --commit

Courses that also sell something are left alone unless ``--include-paid`` is
given: moving their audit learners to honor would hand out the certificate the
course exists to sell.

Certificates are not generated here. Once this has run, learners who already
passed need ``cert_generation``; anyone who passes later is handled
automatically.
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from common.djangoapps.course_modes.models import CourseMode
from common.djangoapps.student.models import CourseEnrollment

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Move courses and their learners from the audit track to honor."

    def add_arguments(self, parser):
        parser.add_argument(
            "--courses", nargs="+",
            help="Only these course keys. Default: every course.",
        )
        parser.add_argument(
            "--include-paid", action="store_true",
            help=(
                "Also change courses that sell a mode. Off by default, because "
                "that gives away the certificate such a course exists to sell."
            ),
        )
        parser.add_argument(
            "--commit", action="store_true",
            help="Save the changes. Without it nothing is written.",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        course_keys = self._course_keys(options.get("courses"))

        modes_renamed, modes_dropped, skipped = 0, 0, []
        learners_on_audit, courses_touched = 0, 0

        for course_key in course_keys:
            if not options["include_paid"] and self._sells_something(course_key):
                skipped.append(course_key)
                continue

            renamed, dropped = self._fix_mode(course_key, commit)
            modes_renamed += renamed
            modes_dropped += dropped

            learners = self._audit_enrollments(course_key)
            if not (renamed or dropped or learners):
                continue

            courses_touched += 1
            learners_on_audit += learners
            self.stdout.write(
                f"{course_key}: "
                f"{'mode audit→honor, ' if renamed else ''}"
                f"{'dropped duplicate audit mode, ' if dropped else ''}"
                f"{learners} learner(s) on audit"
            )
            if commit:
                self._move_learners(course_key)

        self._report(courses_touched, modes_renamed, modes_dropped,
                     learners_on_audit, skipped, commit)

    # -- the two halves ---------------------------------------------------

    def _fix_mode(self, course_key, commit):
        """
        Rename the course's audit mode to honor, or drop it if honor is there.

        Renaming into an existing honor row would break the unique index on
        (course, mode_slug, currency), and a course has no use for two free
        tracks anyway.
        """
        audit = CourseMode.objects.filter(course_id=course_key, mode_slug=CourseMode.AUDIT)
        if not audit.exists():
            return 0, 0

        if CourseMode.objects.filter(course_id=course_key, mode_slug=CourseMode.HONOR).exists():
            count = audit.count()
            if commit:
                audit.delete()
            return 0, count

        count = audit.count()
        if commit:
            audit.update(mode_slug=CourseMode.HONOR, mode_display_name="Honor")
        return count, 0

    def _audit_enrollments(self, course_key):
        return CourseEnrollment.objects.filter(
            course_id=course_key, is_active=True, mode=CourseMode.AUDIT,
        ).count()

    def _move_learners(self, course_key):
        """
        Move each learner individually through update_enrollment.

        A bulk UPDATE would be faster and wrong: the platform's enrolment
        events would never fire, so nothing downstream would learn that these
        learners can now earn a certificate.
        """
        moved = 0
        enrollments = CourseEnrollment.objects.filter(
            course_id=course_key, is_active=True, mode=CourseMode.AUDIT,
        )
        for enrollment in enrollments.iterator():
            try:
                enrollment.update_enrollment(mode=CourseMode.HONOR)
            except Exception as exc:  # pylint: disable=broad-except
                self.stderr.write(f"  {enrollment.user.username}: failed — {exc}")
                continue
            moved += 1
        return moved

    # -- plumbing ---------------------------------------------------------

    def _course_keys(self, given):
        if given:
            keys = []
            for course_id in given:
                try:
                    keys.append(CourseKey.from_string(course_id))
                except InvalidKeyError as exc:
                    raise CommandError(f"{course_id} is not a course key") from exc
            return keys

        # Every course that has an audit mode or an audit enrolment; a course
        # can easily have one without the other. The two queries can hand back
        # the same course as a string from one and a CourseKey from the other,
        # so both are normalised or the course gets processed twice.
        from_modes = CourseMode.objects.filter(
            mode_slug=CourseMode.AUDIT,
        ).values_list("course_id", flat=True)
        from_enrollments = CourseEnrollment.objects.filter(
            mode=CourseMode.AUDIT, is_active=True,
        ).values_list("course_id", flat=True).distinct()

        keys = {}
        for course_id in list(from_modes) + list(from_enrollments):
            try:
                keys[str(course_id)] = CourseKey.from_string(str(course_id))
            except InvalidKeyError:
                self.stderr.write(f"skipping unreadable course id {course_id!r}")
        return [keys[course_id] for course_id in sorted(keys)]

    def _sells_something(self, course_key):
        return CourseMode.objects.filter(course_id=course_key, min_price__gt=0).exists()

    def _report(self, courses, renamed, dropped, learners, skipped, commit):
        self.stdout.write("")
        if skipped:
            self.stdout.write(
                f"{len(skipped)} course(s) left alone because they sell a mode; "
                f"pass --include-paid to change them too."
            )
        verb = "moved" if commit else "to move"
        summary = (
            f"{courses} course(s): {renamed} mode(s) renamed, "
            f"{dropped} duplicate audit mode(s) dropped, {learners} learner(s) {verb}"
        )
        if not commit:
            self.stdout.write(self.style.WARNING(
                summary + " — nothing written. Add --commit to carry it out."
            ))
            return

        self.stdout.write(self.style.SUCCESS(summary))
        self.stdout.write(
            "Learners who already passed need their certificates generating:\n"
            "  ./manage.py lms cert_generation -u <user ids> -c <course key>"
        )
