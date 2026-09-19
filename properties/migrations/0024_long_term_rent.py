from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("properties", "0023_property_cian_export")]

    operations = [
        migrations.AddField(model_name="property", name="deal_type", field=models.CharField(choices=[("sale", "Продажа"), ("rent", "Долгосрочная аренда")], default="sale", max_length=10, verbose_name="Тип сделки")),
        migrations.AddField(model_name="property", name="rent_deposit", field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name="Залог")),
        migrations.AddField(model_name="property", name="rent_commission", field=models.PositiveIntegerField(blank=True, null=True, verbose_name="Комиссия, %")),
        migrations.AddField(model_name="property", name="utilities_terms", field=models.CharField(blank=True, choices=[("included", "Включены в стоимость"), ("separate", "Оплачиваются отдельно"), ("meters", "Отдельно по счётчикам")], max_length=20, verbose_name="Коммунальные платежи")),
        migrations.AddField(model_name="property", name="min_lease_months", field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name="Минимальный срок аренды, мес.")),
        migrations.AddField(model_name="property", name="available_from", field=models.DateField(blank=True, null=True, verbose_name="Свободен с")),
        migrations.AddField(model_name="property", name="furnished", field=models.BooleanField(default=False, verbose_name="Есть мебель")),
        migrations.AddField(model_name="property", name="pets_allowed", field=models.BooleanField(default=False, verbose_name="Можно с животными")),
        migrations.AddField(model_name="property", name="children_allowed", field=models.BooleanField(default=False, verbose_name="Можно с детьми")),
        migrations.AddField(model_name="lead", name="interest_type", field=models.CharField(choices=[("buy", "Покупка"), ("long_rent", "Долгосрочная аренда")], default="buy", max_length=20, verbose_name="Интерес клиента")),
    ]
