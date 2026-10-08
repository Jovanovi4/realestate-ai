from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.rules import collect_usage
from billing.services import AccountService
from .forms import PropertyForm
from .models import Agency, AgencyMembership, Lead, Property


class CommercialPropertyPricingTests(TestCase):
    def test_existing_type_codes_remain_and_first_release_types_are_available(self):
        labels = dict(Property.PROPERTY_TYPES)
        self.assertEqual(
            {code: labels[code] for code in Property.COMMERCIAL_TYPES},
            {
                "office": "Офис",
                "retail": "Торговое помещение",
                "warehouse": "Склад",
                "commercial": "Помещение свободного назначения",
            },
        )
        form_choices = dict(PropertyForm().fields["property_type"].choices)
        self.assertEqual(form_choices["office"], "Офис")
        self.assertEqual(form_choices["commercial"], "Помещение свободного назначения")
        self.assertEqual(PropertyForm(instance=Property(property_type="commercial")).property_group, "commercial")
        self.assertEqual(PropertyForm(instance=Property(property_type="land")).property_group, "residential")

    def test_unit_price_is_derived_from_whole_property_price(self):
        property = Property(
            property_type="retail", deal_type="sale", price=Decimal("25000000.00"), area=75,
        )
        self.assertEqual(property.calculated_price_per_sqm, Decimal("333333.33"))
        property.deal_type = "rent"
        property.price = Decimal("250000.00")
        self.assertEqual(property.calculated_price_per_sqm, Decimal("3333.33"))
        property.area = 100
        self.assertEqual(property.calculated_price_per_sqm, Decimal("2500.00"))

    def test_unit_price_is_unavailable_without_positive_price_and_area(self):
        property = Property(property_type="office", price=Decimal("100000.00"))
        self.assertIsNone(property.calculated_price_per_sqm)
        property.area = 0
        self.assertIsNone(property.calculated_price_per_sqm)
        property.area = 20
        property.price = Decimal("0")
        self.assertIsNone(property.calculated_price_per_sqm)
        property.price = Decimal("100000.00")
        property.property_type = "apartment"
        self.assertIsNone(property.calculated_price_per_sqm)

    def test_rent_card_and_detail_show_monthly_total_and_calculated_rate(self):
        owner = User.objects.create_user("commercial-owner", password="password")
        property = Property.objects.create(
            owner=owner, title="Торговое помещение", property_type="retail",
            deal_type="rent", price=Decimal("180000.00"), area=60, currency="RUB",
        )
        self.client.force_login(owner)

        listing = self.client.get(reverse("property_list"))
        detail = self.client.get(reverse("property_detail", args=[property.pk]))

        self.assertContains(listing, "Торговое помещение")
        self.assertContains(listing, "Расчётно")
        self.assertContains(listing, "/ м² / мес.")
        self.assertContains(detail, "Аренда за месяц")
        self.assertContains(detail, "Расчётно за м² в месяц")

    def test_commercial_form_saves_fractional_area_and_keeps_team_links(self):
        owner = User.objects.create_user("team-commercial-owner", password="password")
        agency = Agency.objects.create(name="Команда", owner=owner)
        AgencyMembership.objects.create(agency=agency, user=owner, role="owner")
        self.client.force_login(owner)
        create_url = reverse("create_property")
        key = self.client.get(create_url).context["resource_request_key"]
        response = self.client.post(create_url, {
            "request_key": key,
            "title": "Склад у трассы",
            "property_type": "warehouse",
            "deal_type": "rent",
            "status": "draft",
            "avito_operation": "rent",
            "price": "180000",
            "currency": "RUB",
            "area": "57.35",
            "commercial_purpose": "Хранение товаров",
            "ceiling_height_m": "4.25",
            "power_kw": "35.50",
            "entrance_type": "separate",
            "parking_type": "on_site",
            "access_mode": "round_the_clock",
            "utilities_terms": "separate",
            "min_lease_months": "12",
            "landing_template": "classic",
        })
        self.assertEqual(response.status_code, 302)
        property = Property.objects.get(owner=owner)
        self.assertEqual(property.agency, agency)
        self.assertEqual(property.area, Decimal("57.35"))
        self.assertEqual(property.area_display, "57.35")
        self.assertEqual(property.calculated_price_per_sqm, Decimal("3138.62"))
        self.assertEqual(property.ceiling_height_m, Decimal("4.25"))
        self.assertEqual(property.power_kw, Decimal("35.50"))
        self.assertEqual(property.access_mode, "round_the_clock")
        self.assertEqual(property.parking_type, "on_site")
        self.assertEqual(collect_usage(AccountService.ensure_agency(agency)).active_properties, 1)
        lead = Lead.objects.create(property=property, name="Клиент", phone="+79990000000")
        self.assertEqual(lead.property.agency, agency)

        editor = self.client.get(reverse("edit_property", args=[property.pk]))
        detail = self.client.get(reverse("property_detail", args=[property.pk]))
        self.assertIn("propertyGroup: 'commercial'", editor.content.decode())
        self.assertContains(editor, "id_property_type_commercial")
        self.assertContains(editor, "Коммерческие характеристики")
        self.assertContains(detail, "57.35 м²")
        self.assertContains(detail, "Хранение товаров")
        self.assertContains(detail, "Круглосуточно")

    def test_switching_category_clears_incompatible_characteristics(self):
        owner = User.objects.create_user("category-owner", password="password")
        property = Property.objects.create(
            owner=owner, title="Офис", property_type="office", commercial_purpose="Офис услуг",
            ceiling_height_m=Decimal("3.20"), access_mode="schedule",
        )
        residential = PropertyForm({
            "title": "Квартира", "property_type": "apartment", "deal_type": "sale",
            "status": "draft", "currency": "RUB", "avito_operation": "sell",
            "landing_template": "classic", "commercial_purpose": "Старое назначение",
            "ceiling_height_m": "3.20", "access_mode": "schedule", "rooms": "2",
        }, instance=property, user=owner)
        self.assertTrue(residential.is_valid(), residential.errors)
        residential.save()
        property.refresh_from_db()
        self.assertEqual(property.rooms, 2)
        self.assertEqual(property.commercial_purpose, "")
        self.assertIsNone(property.ceiling_height_m)
        self.assertEqual(property.access_mode, "")

        commercial = PropertyForm({
            "title": "Склад", "property_type": "warehouse", "deal_type": "sale",
            "status": "draft", "currency": "RUB", "avito_operation": "sell",
            "landing_template": "classic", "rooms": "5",
            "commercial_purpose": "Хранение",
        }, instance=property, user=owner)
        self.assertTrue(commercial.is_valid(), commercial.errors)
        commercial.save()
        property.refresh_from_db()
        self.assertEqual(property.commercial_purpose, "Хранение")
        self.assertIsNone(property.rooms)

    def test_area_keeps_whole_number_display_and_rejects_negative_value(self):
        self.assertEqual(Property(area=Decimal("78.00")).area_display, "78")
        form = PropertyForm({
            "title": "Офис", "property_type": "office", "deal_type": "sale",
            "status": "draft", "currency": "RUB", "area": "-2.50",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("area", form.errors)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Property.objects.create(title="Отрицательная площадь", area=Decimal("-0.01"))

    def test_editing_existing_commercial_property_keeps_legacy_room_data(self):
        owner = User.objects.create_user("legacy-office-owner", password="password")
        property = Property.objects.create(
            owner=owner, title="Офис", property_type="office", rooms=4,
        )
        form = PropertyForm({
            "title": "Офис после ремонта", "property_type": "office", "deal_type": "sale",
            "status": "draft", "currency": "RUB", "avito_operation": "sell",
            "landing_template": "classic", "rooms": "8",
        }, instance=property, user=owner)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        property.refresh_from_db()
        self.assertEqual(property.rooms, 4)


class CommercialPropertyDisplayTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("commercial-display-owner", password="password")
        self.commercial = Property.objects.create(
            owner=self.owner, title="Офис для услуг", property_type="office", deal_type="rent",
            price=Decimal("180000.00"), currency="RUB", area=Decimal("60.00"),
            commercial_purpose="Клиентский офис", ceiling_height_m=Decimal("3.25"),
            power_kw=Decimal("25.50"), entrance_type="separate", parking_type="on_site",
            access_mode="round_the_clock", landing_published=True,
            rooms=5, land_area=Decimal("120"), furnished=True,
        )
        self.residential = Property.objects.create(
            owner=self.owner, title="Жилая квартира", property_type="apartment",
            deal_type="sale", price=Decimal("5000000.00"), currency="RUB",
            area=Decimal("50.00"), rooms=2, landing_published=True,
        )

    def test_list_segment_filters_results_and_keeps_removable_chip(self):
        self.client.force_login(self.owner)
        for segment, expected, hidden in (
            ("commercial", self.commercial, self.residential),
            ("residential", self.residential, self.commercial),
        ):
            with self.subTest(segment=segment):
                response = self.client.get(reverse("property_list"), {"segment": segment})
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item.pk for item in response.context["properties"]], [expected.pk])
                self.assertIn(expected.title, response.content.decode())
                self.assertNotIn(hidden.title, response.content.decode())
                self.assertIn("Сегмент:", response.content.decode())
                self.assertIn('name="segment"', response.content.decode())
        response = self.client.get(reverse("property_list"), {"segment": "commercial"}, HTTP_HX_REQUEST="true")
        self.assertIn("Клиентский офис", response.content.decode())
        self.assertIn("Аренда всего помещения за месяц", response.content.decode())

    def test_commercial_detail_has_full_monthly_price_and_characteristics(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("property_detail", args=[self.commercial.pk]))
        self.assertIn("Аренда всего помещения за месяц", response.content.decode())
        self.assertIn("Клиентский офис", response.content.decode())
        self.assertIn("кВт", response.content.decode())
        self.assertNotIn("<dt>Комнаты</dt>", response.content.decode())

    def test_three_commercial_landings_show_business_facts_without_residential_blocks(self):
        url = reverse("public_landing", args=[self.commercial.landing_slug])
        for template in ("classic", "modern", "premium"):
            with self.subTest(template=template):
                self.commercial.landing_template = template
                self.commercial.save(update_fields=["landing_template"])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                for text in (
                    "Аренда всего помещения за месяц", "Расчётно", "/ м² / мес.",
                    "Клиентский офис", "Высота потолков", "Мощность", "Вход",
                    "Парковка", "Режим доступа", "Круглосуточно", "Уточнить условия",
                ):
                    self.assertIn(text, response.content.decode())
                for text in ("Ипотечный калькулятор", "Комнаты", "Участок", "Мебель"):
                    self.assertNotIn(text, response.content.decode())

        residential_response = self.client.get(reverse("public_landing", args=[self.residential.landing_slug]))
        self.assertIn("Ипотечный калькулятор", residential_response.content.decode())
        self.assertIn("Комнаты", residential_response.content.decode())

        self.commercial.deal_type = "sale"
        self.commercial.price = Decimal("18000000.00")
        self.commercial.save(update_fields=["deal_type", "price"])
        sale_response = self.client.get(url)
        self.assertIn("Полная цена помещения", sale_response.content.decode())
        self.assertIn("/ м²", sale_response.content.decode())
        self.assertNotIn("Аренда всего помещения за месяц", sale_response.content.decode())

    @override_settings(TURNSTILE_ENABLED=False, PUBLIC_LEAD_RATE_LIMIT=0, PUBLIC_LEAD_RATE_LIMIT_PER_PROPERTY=0)
    @patch("properties.views.LeadNotificationService.notify_new_lead")
    def test_commercial_landing_response_creates_lead_for_property_owner(self, notify):
        agency = Agency.objects.create(name="Коммерческая команда", owner=self.owner)
        AgencyMembership.objects.create(agency=agency, user=self.owner, role="owner")
        self.commercial.agency = agency
        self.commercial.save(update_fields=["agency"])
        response = self.client.post(reverse("public_landing", args=[self.commercial.landing_slug]), {
            "contact_purpose": "rent_terms",
            "name": "Арендатор",
            "phone": "+7 999 123-45-67",
            "message": "Когда можно осмотреть помещение?",
            "personal_data_consent": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявка отправлена", response.content.decode())
        lead = Lead.objects.get(property=self.commercial)
        self.assertEqual(lead.assigned_to, self.owner)
        self.assertEqual(lead.client.agency, agency)
        self.assertEqual(lead.source, "landing")
        self.assertEqual(lead.interest_type, "long_rent")
        self.assertIsNotNone(lead.personal_data_consent_at)
        notify.assert_called_once()

        self.client.force_login(self.owner)
        self.assertIn("Арендатор", self.client.get(reverse("lead_detail", args=[lead.pk])).content.decode())
