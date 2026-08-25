"""
Report which courses are actually being sold, and which only look like it.

Two mistakes are easy to make and invisible until someone notices the money is
missing:

1. The course keeps its free ``audit`` or ``honor`` mode, so "Enroll now" still
   lets anyone in without paying. Open edX auto-enrols whenever a free mode
   exists, and it does so before the learner ever reaches a price.
2. The price is in the wrong currency, and checkout refuses to start — a course
   that looks for sale but cannot be bought.

    ./manage.py lms halyk_courses

Reports only; it changes nothing. The fix for the first is to delete the free
mode from the course, which is a decision about what the university sells, not
something a command should make on its own.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

FREE_MODES = ("audit", "honor")


class Command(BaseCommand):
    help = "List courses for sale and flag the ones still reachable for free."

    def handle(self, *args, **options):
        from common.djangoapps.course_modes.models import CourseMode

        slug = getattr(settings, "HALYK_COURSE_MODE", "verified")
        currency = getattr(settings, "HALYK_CURRENCY", "KZT").lower()

        paid = CourseMode.objects.filter(mode_slug=slug, min_price__gt=0).order_by("course_id")
        if not paid.exists():
            self.stdout.write(
                f"No course has a paid '{slug}' mode, so nothing is for sale yet."
            )
            return

        problems = 0

        for mode in paid:
            free = list(
                CourseMode.objects
                .filter(course_id=mode.course_id, mode_slug__in=FREE_MODES)
                .values_list("mode_slug", flat=True)
            )
            wrong_currency = mode.currency.lower() != currency

            faults = []
            if free:
                faults.append(f"free to enrol via '{', '.join(free)}'")
            if wrong_currency:
                faults.append(f"priced in {mode.currency.upper()}, not {currency.upper()}")

            price = f"{mode.min_price} {mode.currency.upper()}"
            if faults:
                problems += 1
                self.stdout.write(self.style.WARNING(
                    f"{mode.course_id}  {price}  ← {'; '.join(faults)}"
                ))
            else:
                self.stdout.write(self.style.SUCCESS(f"{mode.course_id}  {price}"))

        self.stdout.write("")
        if problems:
            self.stdout.write(self.style.WARNING(
                f"{problems} of {paid.count()} course(s) cannot be sold as configured."
            ))
            self.stdout.write(
                "A course is only paid-only once its free modes are gone. Remove them in "
                "Django admin under Course Modes, or from the shell."
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                f"{paid.count()} course(s) for sale, none reachable for free."
            ))
