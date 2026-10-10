from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0011_listing_email'),
    ]

    operations = [
        migrations.CreateModel(
            name='Country',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(help_text='Full name, e.g. United Arab Emirates', max_length=100)),
                ('short_name', models.CharField(help_text='In headings and menus, e.g. "UAE"', max_length=50)),
                ('in_name', models.CharField(help_text='As written after "in", e.g. "the UAE"', max_length=60)),
                ('slug', models.SlugField(help_text='First part of the URL: /uae/dubai/', unique=True)),
                ('code', models.CharField(help_text='ISO 3166 code, e.g. AE', max_length=2, unique=True)),
                ('currency', models.CharField(blank=True, help_text='ISO 4217 code, e.g. AED', max_length=3)),
                ('phone_code', models.CharField(blank=True, help_text='Without +, e.g. 971', max_length=4)),
                ('time_zone', models.CharField(blank=True, help_text='e.g. Asia/Dubai', max_length=50)),
                ('meta_description', models.TextField(blank=True)),
            ],
            options={
                'verbose_name_plural': 'countries',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='city',
            name='country',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='cities', to='listings.country'),
        ),
    ]
