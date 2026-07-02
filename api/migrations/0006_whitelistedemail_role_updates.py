from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0005_auditlog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="whitelistedemail",
            name="role",
            field=models.CharField(
                choices=[
                    ("school_admin", "School Admin"),
                    ("teacher", "Teacher"),
                    ("student", "Student"),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="whitelistedemail",
            unique_together=set(),
        ),
        migrations.AddIndex(
            model_name="whitelistedemail",
            index=models.Index(
                fields=["email", "organization"],
                name="api_whiteli_email_org_31f7_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="whitelistedemail",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_used=False),
                fields=("email", "organization"),
                name="uniq_pending_whitelist_email_org",
            ),
        ),
    ]
