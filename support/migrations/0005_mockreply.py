from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("support", "0004_add_draft_moderation")]

    operations = [
        migrations.CreateModel(
            name="MockReply",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idempotency_key", models.CharField(max_length=128, unique=True)),
                ("ticket_id", models.CharField(max_length=255)),
                ("message", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
