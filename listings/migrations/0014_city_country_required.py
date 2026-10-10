from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0013_cities_to_country'),
    ]

    operations = [
        migrations.AlterField(
            model_name='city',
            name='country',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name='cities', to='listings.country'),
        ),
    ]
