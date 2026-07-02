from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0007_otpverification_attempt_count_and_digest"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="whitelistedemail",
            new_name="api_whiteli_email_163043_idx",
            old_name="api_whiteli_email_org_31f7_idx",
        ),
    ]
