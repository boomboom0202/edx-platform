from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import opaque_keys.edx.django.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("invoice_id", models.CharField(db_index=True, max_length=64, unique=True)),
                ("course_id", opaque_keys.edx.django.models.CourseKeyField(
                    db_index=True, max_length=255)),
                ("course_mode", models.CharField(
                    help_text="The CourseMode slug the learner is buying, e.g. 'verified'.",
                    max_length=100)),
                ("amount", models.PositiveIntegerField()),
                ("currency", models.CharField(default="KZT", max_length=8)),
                ("status", models.CharField(
                    choices=[("pending", "Pending"), ("paid", "Paid"),
                             ("failed", "Failed"), ("cancelled", "Cancelled"),
                             ("refunded", "Refunded")],
                    db_index=True, default="pending", max_length=16)),
                ("reference", models.CharField(blank=True, default="", max_length=128)),
                ("card_mask", models.CharField(blank=True, default="", max_length=32)),
                ("failure_reason", models.CharField(blank=True, default="", max_length=255)),
                ("callback_payload", models.JSONField(blank=True, null=True)),
                ("enrolled", models.BooleanField(default=False)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("modified", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="halyk_payments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Halyk payment",
                "verbose_name_plural": "Halyk payments",
                "ordering": ("-created",),
            },
        ),
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(fields=["user", "course_id", "status"],
                               name="halyk_pay_user_course_idx"),
        ),
    ]
