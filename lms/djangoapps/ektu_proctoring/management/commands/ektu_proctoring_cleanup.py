"""Delete recordings older than the retention period; the session records stay."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ... import services
from ...conf import conf
from ...models import Session


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=conf("RETENTION_DAYS"))
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        before = timezone.now() - timedelta(days=options["days"])
        old = Session.objects.filter(created__lt=before, purged_at=None).exclude(
            status__in=(Session.RECORDING, Session.STOPPING),
        )
        size = sum(rec.size for session in old for rec in session.recordings.all())
        self.stdout.write(f"{old.count()} sessions older than {options['days']} days, {size // 1_000_000} MB")
        if not options["dry_run"]:
            for session in old:
                services.purge(session)
