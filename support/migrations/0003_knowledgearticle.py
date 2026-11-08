from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0002_supportrequest_dispatch_count_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="KnowledgeArticle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.SlugField(max_length=100, unique=True)),
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("keywords", models.JSONField(default=list)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["slug"]},
        ),
    ]
