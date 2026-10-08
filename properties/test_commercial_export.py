from xml.etree import ElementTree as ET

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import PropertyForm
from .models import Property, PropertyImage, RealtorProfile
from .services.avito_service import AvitoExportService
from .services.cian_service import CianExportService


class CommercialExportSafetyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("commercial-export-owner", password="password")
        self.profile = RealtorProfile.objects.create(user=self.user, phone="+79990000000")
        self.client.force_login(self.user)

    def make_property(self, property_type, *, title):
        property = Property.objects.create(
            owner=self.user,
            title=title,
            property_type=property_type,
            status="published",
            price=1000000,
            currency="RUB",
            address="Москва, Тестовая улица, 1",
            description="Объект для проверки экспорта недвижимости.",
            area=50,
            avito_export=True,
            cian_export=True,
        )
        PropertyImage.objects.create(property=property, image=f"properties/{property_type}.jpg")
        return property

    def test_legacy_commercial_flags_cannot_put_ads_in_either_feed(self):
        residential = self.make_property("apartment", title="Жилая квартира")
        commercial = [
            self.make_property(property_type, title=f"Коммерческий {property_type}")
            for property_type in Property.COMMERCIAL_TYPES
        ]

        for service, url_name, item_tag in (
            (AvitoExportService, "avito_export", "Ad"),
            (CianExportService, "cian_export", "Object"),
        ):
            with self.subTest(service=service.__name__):
                valid, invalid = service.get_exportable([residential, *commercial], self.profile)
                self.assertEqual(valid, [residential])
                self.assertEqual({property.pk for property, _ in invalid}, {property.pk for property in commercial})
                self.assertTrue(all("пока недоступна" in errors[0] for _, errors in invalid))

                page = self.client.get(reverse(url_name))
                self.assertContains(page, "Коммерческие объекты пока не входят в XML")
                self.assertContains(page, "Готовые объекты")
                download = self.client.get(reverse(url_name), {"download": "1"})
                self.assertEqual(download.status_code, 200)
                items = ET.fromstring(download.content).findall(item_tag)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].findtext("Id" if item_tag == "Ad" else "ExternalId"), f"realestate-ai-{residential.pk}")

                with self.assertRaisesRegex(ValueError, "Коммерческая выгрузка"):
                    service.build_xml([residential, commercial[0]], self.profile, "https://example.test")

    def test_commercial_object_cannot_be_enabled_through_editor(self):
        property = self.make_property("office", title="Офис")
        form = PropertyForm({
            "title": property.title,
            "property_type": "office",
            "deal_type": "sale",
            "status": "published",
            "currency": "RUB",
            "avito_export": "on",
            "cian_export": "on",
            "avito_operation": "sell",
            "landing_template": "classic",
        }, instance=property, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        property.refresh_from_db()
        self.assertFalse(property.avito_export)
        self.assertFalse(property.cian_export)

        editor = self.client.get(reverse("edit_property", args=[property.pk]))
        detail = self.client.get(reverse("property_detail", args=[property.pk]))
        self.assertContains(editor, "Выгрузка коммерческих объектов в Avito и ЦИАН пока недоступна")
        self.assertContains(detail, "Коммерческая выгрузка в Avito и ЦИАН пока недоступна")
        self.assertNotContains(detail, "Скачать XML")

    def test_commercial_only_selection_has_no_downloadable_feed(self):
        self.make_property("warehouse", title="Склад")
        for url_name in ("avito_export", "cian_export"):
            with self.subTest(url_name=url_name):
                page = self.client.get(reverse(url_name))
                self.assertContains(page, "Не войдут в файл")
                self.assertNotContains(page, "Скачать XML")
                self.assertRedirects(self.client.get(reverse(url_name), {"download": "1"}), reverse(url_name))
