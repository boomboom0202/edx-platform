"""
Set "Certificates Display Behavior" on every course at once.

The setting lives in the modulestore, not in MySQL. The
``course_overviews_courseoverview`` table has a column of the same name, but it
is a cache rebuilt from the modulestore whenever a course is published, so
editing it directly looks like it worked and then quietly reverts. This command
writes the real value and refreshes that cache afterwards.

    ./manage.py cms set_certificates_display_behavior --dry-run
    ./manage.py cms set_certificates_display_behavior
    ./manage.py cms set_certificates_display_behavior --behavior end
    ./manage.py cms set_certificates_display_behavior --courses course-v1:ENV+HYD_01+2022

Only existing courses are touched. What new courses start with is the field's
default in ``xmodule/course_block.py``.
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey

from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from xmodule.data import CertificatesDisplayBehaviors
from xmodule.modulestore import ModuleStoreEnum
from xmodule.modulestore.django import modulestore

log = logging.getLogger(__name__)

BEHAVIOURS = [behaviour.value for behaviour in CertificatesDisplayBehaviors]


class Command(BaseCommand):
    help = "Set the certificates display behavior on every course."

    def add_arguments(self, parser):
        parser.add_argument(
            "--behavior", default=CertificatesDisplayBehaviors.EARLY_NO_INFO.value,
            choices=BEHAVIOURS,
            help="What to set it to. Default: early_no_info.",
        )
        parser.add_argument(
            "--courses", nargs="+",
            help="Only these course keys. Default: every course.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would change without touching anything.",
        )

    def handle(self, *args, **options):
        behaviour = options["behavior"]
        store = modulestore()

        course_keys = self._course_keys(options.get("courses"), store)
        if not course_keys:
            self.stdout.write("No courses found.")
            return

        changed, unchanged, failed = [], 0, []

        for course_key in course_keys:
            try:
                course = store.get_course(course_key)
            except Exception as exc:  # pylint: disable=broad-except
                failed.append((course_key, exc))
                self.stderr.write(f"{course_key}: could not be read — {exc}")
                continue

            if course is None:
                failed.append((course_key, "not in the modulestore"))
                self.stderr.write(f"{course_key}: not in the modulestore")
                continue

            current = course.certificates_display_behavior
            if current == behaviour:
                unchanged += 1
                continue

            self.stdout.write(f"{course_key}: {current} → {behaviour}")
            if options["dry_run"]:
                changed.append(course_key)
                continue

            try:
                self._apply(store, course, behaviour)
            except Exception as exc:  # pylint: disable=broad-except
                failed.append((course_key, exc))
                self.stderr.write(self.style.ERROR(f"{course_key}: failed — {exc}"))
                continue
            changed.append(course_key)

        if changed and not options["dry_run"]:
            # update_item on a course block publishes it, which refreshes the
            # overview through a signal. Doing it again here is cheap and means
            # the cache is right even if that signal is disconnected.
            CourseOverview.update_select_courses(changed, force_update=True)

        self._report(changed, unchanged, failed, options["dry_run"])

    def _course_keys(self, given, store):
        if given:
            keys = []
            for course_id in given:
                try:
                    keys.append(CourseKey.from_string(course_id))
                except InvalidKeyError as exc:
                    raise CommandError(f"{course_id} is not a course key") from exc
            return keys
        return [summary.id for summary in store.get_course_summaries()]

    def _apply(self, store, course, behaviour):
        """
        Write the setting, keeping the availability date consistent with it.

        "early_no_info" means the certificate shows as soon as it is earned, so
        a certificate_available_date left behind would contradict it — the
        platform ignores the date in that case anyway, and leaving stale data
        in the modulestore only misleads whoever reads it next.
        """
        course.certificates_display_behavior = behaviour
        if behaviour == CertificatesDisplayBehaviors.EARLY_NO_INFO.value:
            if course.certificate_available_date:
                del course.certificate_available_date
        store.update_item(course, ModuleStoreEnum.UserID.mgmt_command)

    def _report(self, changed, unchanged, failed, dry_run):
        self.stdout.write("")
        summary = (
            f"{len(changed)} changed, {unchanged} already set, {len(failed)} failed"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(summary + " (dry run, nothing written)"))
        elif failed:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
