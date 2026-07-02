from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0006_whitelistedemail_role_updates"),
    ]

    operations = [
        migrations.AddField(
            model_name="otpverification",
            name="attempt_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="otpverification",
            name="otp_code",
            field=models.CharField(max_length=128),
        ),
    ]
