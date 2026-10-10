from django.db import migrations, models


def emirates(apps, schema_editor):
    apps.get_model("listings", "Country").objects.filter(code="AE").update(city_label="emirate or city")


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0014_city_country_required'),
    ]

    operations = [
        migrations.AddField(
            model_name='area',
            name='region',
            field=models.CharField(blank=True, help_text='Group on the city page, e.g. "Jumeirah & Al Barsha". Leave all of a city\'s areas blank for a plain A–Z list (load with apply_regions).', max_length=60),
        ),
        migrations.AddField(
            model_name='country',
            name='city_label',
            field=models.CharField(default='city', help_text='What its cities are called: "emirate or city" in the UAE', max_length=30),
        ),
        migrations.RunPython(emirates, migrations.RunPython.noop),
    ]
