from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Agency, AgencyMembership, Client, ClientInteraction, ClientReminder, Deal, OwnerInquiry, Property, PropertyOwnerLink


class PropertyOwnerWorkflowTests(TestCase):
    def setUp(self):
        self.realtor = User.objects.create_user("owner-workflow", password="password")
        self.other = User.objects.create_user("another-realtor", password="password")
        self.property = Property.objects.create(owner=self.realtor, title="Квартира у парка")
        self.client.force_login(self.realtor)

    def test_new_owner_is_linked_and_kept_out_of_buyer_board(self):
        response = self.client.post(reverse("property_owner_create", args=[self.property.pk]), {
            "name": "Мария", "phone": "+7 (999) 123-45-67", "role": "owner",
        })
        self.assertEqual(response.status_code, 302)
        contact = Client.objects.get(phone="+79991234567")
        self.assertEqual(contact.contact_role, "owner")
        self.assertEqual(PropertyOwnerLink.objects.get(property=self.property).client, contact)
        self.assertNotContains(self.client.get(reverse("client_list")), "Мария")
        self.assertContains(self.client.get(reverse("client_list") + "?role=owner"), "Мария")
        self.assertNotContains(self.client.get(reverse("client_board")), "Мария")
        self.assertContains(self.client.get(reverse("property_detail", args=[self.property.pk])), "Мария")
        owner_page = self.client.get(reverse("client_detail", args=[contact.pk]))
        self.assertContains(owner_page, "Квартира у парка")
        self.assertNotContains(owner_page, "Новая сделка")
        self.assertNotContains(owner_page, 'id="client-matching-tab"')

    def test_existing_buyer_can_also_be_owner_of_multiple_properties(self):
        contact = Client.objects.create(owner=self.realtor, name="Анна", phone="+79990000001")
        second = Property.objects.create(owner=self.realtor, title="Второй объект")
        for property in (self.property, second):
            response = self.client.post(reverse("property_owner_attach", args=[property.pk]), {
                "client": contact.pk, "role": "co_owner",
            })
            self.assertEqual(response.status_code, 302)
        contact.refresh_from_db()
        self.assertEqual(contact.contact_role, "both")
        self.assertEqual(contact.owned_properties.count(), 2)
        self.assertContains(self.client.get(reverse("client_list")), "Анна")
        self.assertContains(self.client.get(reverse("client_list") + "?role=owner"), "Анна")

    def test_owner_overview_limits_properties_and_full_list_includes_archive(self):
        contact = Client.objects.create(owner=self.realtor, name="Мария", phone="+79990000007", contact_role="owner")
        owned = [self.property]
        owned.extend(Property.objects.create(
            owner=self.realtor, title=f"Объект {index}", status="archived" if index == 3 else "draft",
        ) for index in range(1, 4))
        for property in owned:
            PropertyOwnerLink.objects.create(
                property=property, client=contact,
                agreement_expires_on=timezone.localdate() + timedelta(days=14),
            )
        Property.objects.create(owner=self.realtor, title="Не объект Марии")
        OwnerInquiry.objects.create(
            owner=self.realtor, client=contact, assigned_to=self.realtor,
            deal_type="sale", address="Тестовая улица, 12",
        )

        detail = self.client.get(reverse("client_detail", args=[contact.pk]))
        self.assertEqual(detail.context["owner_properties_total"], 4)
        self.assertEqual(len(detail.context["owner_properties"]), 3)
        self.assertContains(detail, f"{reverse('property_list')}?owner_client={contact.pk}")
        self.assertContains(detail, "Договорённость до")
        self.assertContains(detail, 'id="client-owner-inquiries-tab"')
        self.assertContains(detail, 'id="client-owner-properties-tab"')
        html = detail.content.decode()
        self.assertLess(html.index('id="client-history" role="tabpanel"'), html.index('id="client-owner-inquiries" role="tabpanel"'))
        self.assertLess(html.index('id="client-owner-inquiries" role="tabpanel"'), html.index('id="client-owner-properties" role="tabpanel"'))
        self.assertLess(html.index('id="client-owner-inquiries" role="tabpanel"'), html.index("Тестовая улица, 12"))
        self.assertLess(html.index('id="client-owner-properties" role="tabpanel"'), html.index("Договорённость до"))

        full_list = self.client.get(reverse("property_list"), {"owner_client": contact.pk})
        self.assertEqual(full_list.context["paginator"].count, 4)
        self.assertContains(full_list, "Объект 3")
        self.assertNotContains(full_list, "Не объект Марии")
        self.assertContains(full_list, f'name="owner_client" value="{contact.pk}"')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("property_list"), {"owner_client": contact.pk}).status_code, 404)

    def test_deal_shows_owner_as_other_side_of_property(self):
        owner_contact = Client.objects.create(owner=self.realtor, name="Собственник", phone="+79990000021", contact_role="owner")
        buyer = Client.objects.create(owner=self.realtor, name="Покупатель", phone="+79990000022")
        PropertyOwnerLink.objects.create(property=self.property, client=owner_contact)
        deal = Deal.objects.create(owner=self.realtor, responsible=self.realtor, client=buyer, property=self.property)
        response = self.client.get(reverse("deal_detail", args=[deal.pk]))
        self.assertContains(response, "Собственник")
        self.assertContains(response, owner_contact.phone)

    def test_owner_details_and_report_open_from_their_respective_property_tabs(self):
        contact = Client.objects.create(owner=self.realtor, name="Мария", phone="+79990000024", contact_role="owner")
        link = PropertyOwnerLink.objects.create(property=self.property, client=contact)

        response = self.client.get(reverse("property_detail", args=[self.property.pk]))
        html = response.content.decode()
        overview = html.split('id="overview-pane"', 1)[1].split('id="data-pane"', 1)[0]
        data = html.split('id="data-pane"', 1)[1].split('id="photos-pane"', 1)[0]
        publication = html.split('id="publication-pane"', 1)[1].split('id="owner-reports"', 1)[1]

        self.assertNotIn('id="property-owners"', overview)
        self.assertNotIn('id="property-deals"', overview)
        self.assertIn('id="property-owners"', data)
        self.assertIn(reverse("property_owner_settings", args=[self.property.pk]), data)
        self.assertNotIn(reverse("property_owner_update", args=[self.property.pk, link.pk]), data)
        self.assertNotIn(reverse("property_owner_interaction", args=[self.property.pk, link.pk]), data)
        self.assertNotIn(reverse("owner_report", args=[link.pk]), data)
        self.assertIn(reverse("owner_report", args=[link.pk]), publication)

        settings_page = self.client.get(reverse("property_owner_settings", args=[self.property.pk]))
        self.assertContains(settings_page, reverse("property_owner_update", args=[self.property.pk, link.pk]))
        self.assertContains(settings_page, "Условия работы")

        report = self.client.get(reverse("owner_report", args=[link.pk]))
        self.assertContains(report, f'{reverse("property_detail", args=[self.property.pk])}#owner-reports')

    def test_owner_and_work_terms_are_created_together(self):
        response = self.client.post(reverse("property_owner_settings", args=[self.property.pk]), {
            "contact_name": "Мария", "contact_phone": "+7 (999) 123-45-67", "role": "owner",
            "agreed_price": "7000000", "agreement_expires_on": "2026-12-31",
            "showing_instructions": "Предупредить за день", "publication_approved": "on",
        })
        self.assertRedirects(response, reverse("property_owner_settings", args=[self.property.pk]))
        link = PropertyOwnerLink.objects.get(property=self.property)
        self.assertEqual(link.client.name, "Мария")
        self.assertEqual(str(link.agreed_price), "7000000.00")
        self.assertEqual(link.showing_instructions, "Предупредить за день")
        self.assertIsNotNone(link.publication_approved_at)

        detail = self.client.get(reverse("property_detail", args=[self.property.pk]))
        self.assertContains(detail, "Предупредить за день")
        self.assertNotContains(detail, "Сохранить условия")

    def test_owner_settings_reuse_contact_and_keep_invalid_input_visible(self):
        contact = Client.objects.create(owner=self.realtor, name="Анна", phone="+79990000025")
        settings_url = reverse("property_owner_settings", args=[self.property.pk])
        invalid = self.client.post(settings_url, {
            "client": contact.pk, "contact_name": "Другое имя", "role": "co_owner",
            "agreed_price": "5000000",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, "Выберите контакт или заполните данные нового собственника.", status_code=400)
        self.assertFalse(PropertyOwnerLink.objects.exists())

        response = self.client.post(settings_url, {
            "client": contact.pk, "role": "co_owner", "agreed_price": "5000000",
        })
        self.assertRedirects(response, settings_url)
        link = PropertyOwnerLink.objects.get(property=self.property)
        self.assertEqual(link.client, contact)
        self.assertEqual(link.role, "co_owner")
        self.assertEqual(contact.owned_properties.count(), 1)
        contact.refresh_from_db()
        self.assertEqual(contact.contact_role, "both")

    def test_owner_contact_from_global_card_keeps_property_context(self):
        contact = Client.objects.create(owner=self.realtor, name="Мария", phone="+79990000024", contact_role="owner")
        PropertyOwnerLink.objects.create(property=self.property, client=contact)
        card = self.client.get(reverse("client_detail", args=[contact.pk]))
        self.assertContains(card, 'name="property"')
        self.assertContains(card, "Записать контакт")

        response = self.client.post(reverse("client_interaction_create", args=[contact.pk]), {
            "property": self.property.pk, "interaction_type": "call", "text": "Показ согласован",
        })
        self.assertRedirects(response, f'{reverse("client_detail", args=[contact.pk])}#client-history')
        interaction = ClientInteraction.objects.get(client=contact, interaction_type="call")
        self.assertEqual(interaction.property, self.property)
        self.assertEqual(interaction.text, "Показ согласован")

        foreign_property = Property.objects.create(owner=self.other, title="Чужой объект")
        self.client.post(reverse("client_interaction_create", args=[contact.pk]), {
            "property": foreign_property.pk, "interaction_type": "call", "text": "Чужая запись",
        })
        self.assertEqual(ClientInteraction.objects.filter(client=contact, interaction_type="call").count(), 1)

    def test_public_landing_does_not_expose_owner_contact(self):
        contact = Client.objects.create(owner=self.realtor, name="Секретный собственник", phone="+79990000023", contact_role="owner")
        PropertyOwnerLink.objects.create(property=self.property, client=contact, showing_instructions="Звонить через управляющего")
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        self.client.logout()
        response = self.client.get(reverse("public_landing", args=[self.property.landing_slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Секретный собственник")
        self.assertNotContains(response, "+79990000023")
        self.assertNotContains(response, "Звонить через управляющего")

    def test_duplicate_contact_does_not_create_second_record(self):
        Client.objects.create(owner=self.realtor, name="Мария", phone="+79991234567")
        self.client.post(reverse("property_owner_create", args=[self.property.pk]), {
            "name": "Мария снова", "phone": "+7 (999) 123-45-67", "role": "owner",
        })
        self.assertEqual(Client.objects.count(), 1)
        self.assertFalse(PropertyOwnerLink.objects.exists())

    @override_settings(TURNSTILE_ENABLED=False, PUBLIC_LEAD_RATE_LIMIT=0, PUBLIC_LEAD_RATE_LIMIT_PER_PROPERTY=0)
    @patch("properties.views.LeadNotificationService.notify_new_lead")
    def test_owner_who_responds_to_listing_becomes_both_without_duplicate(self, _notify):
        contact = Client.objects.create(
            owner=self.realtor, name="Мария", phone="+79991234567", contact_role="owner",
        )
        PropertyOwnerLink.objects.create(property=self.property, client=contact)
        self.property.landing_published = True
        self.property.save(update_fields=["landing_published"])
        self.client.logout()
        response = self.client.post(reverse("public_landing", args=[self.property.landing_slug]), {
            "contact_purpose": "viewing", "name": "Мария", "phone": "+7 (999) 123-45-67",
            "personal_data_consent": "on",
        })
        self.assertEqual(response.status_code, 200)
        contact.refresh_from_db()
        self.assertEqual(contact.contact_role, "both")
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(self.property.leads.get().client, contact)

    def test_contact_notes_terms_tasks_and_unlink_keep_history(self):
        contact = Client.objects.create(owner=self.realtor, name="Сергей", phone="+79990000002", contact_role="owner")
        link = PropertyOwnerLink.objects.create(property=self.property, client=contact)
        self.client.post(reverse("property_owner_interaction", args=[self.property.pk, link.pk]), {
            "interaction_type": "call", "text": "Показ согласован на пятницу",
        })
        interaction = ClientInteraction.objects.get(client=contact, property=self.property)
        self.assertEqual(interaction.text, "Показ согласован на пятницу")
        contact.refresh_from_db()
        self.assertIsNotNone(contact.first_contacted_at)
        self.assertContains(self.client.get(reverse("client_detail", args=[contact.pk])), "Показ согласован на пятницу")

        self.client.post(reverse("property_owner_update", args=[self.property.pk, link.pk]), {
            f"owner-{link.pk}-role": "owner", f"owner-{link.pk}-agreed_price": "7000000",
            f"owner-{link.pk}-publication_approved": "on",
            f"owner-{link.pk}-agreement_expires_on": "2026-12-31",
            f"owner-{link.pk}-showing_instructions": "Предупредить за день",
        })
        link.refresh_from_db()
        self.assertEqual(str(link.agreed_price), "7000000.00")
        self.assertIsNotNone(link.publication_approved_at)
        self.assertEqual(link.showing_instructions, "Предупредить за день")

        due_at = timezone.localtime(timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("task_create"), {
            "text": "Сообщить итог показа", "due_at": due_at,
            "client": contact.pk, "property": self.property.pk, "deal": "",
        })
        self.assertEqual(response.status_code, 302)
        task = ClientReminder.objects.get(text="Сообщить итог показа")
        self.assertEqual(task.property, self.property)
        self.assertContains(self.client.get(reverse("workday")), "Сообщить итог показа")
        self.assertContains(self.client.get(reverse("work_items")), "Квартира у парка · Сергей")

        self.client.post(reverse("property_owner_remove", args=[self.property.pk, link.pk]))
        self.assertFalse(PropertyOwnerLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(ClientInteraction.objects.filter(pk=interaction.pk).exists())
        self.assertTrue(ClientReminder.objects.filter(pk=task.pk).exists())

    def test_foreign_workspace_is_not_accessible(self):
        contact = Client.objects.create(owner=self.other, name="Чужой контакт", phone="+79990000003")
        own_contact = Client.objects.create(owner=self.realtor, name="Свой контакт", phone="+79990000004")
        self.assertEqual(self.client.post(reverse("property_owner_attach", args=[self.property.pk]), {
            "client": contact.pk, "role": "owner",
        }).status_code, 302)
        self.assertFalse(PropertyOwnerLink.objects.exists())
        link = PropertyOwnerLink.objects.create(property=self.property, client=own_contact)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse("property_detail", args=[self.property.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("property_owner_settings", args=[self.property.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("property_owner_update", args=[self.property.pk, link.pk]), {}).status_code, 404)
        self.assertEqual(self.client.post(reverse("property_owner_remove", args=[self.property.pk, link.pk])).status_code, 404)
        with self.assertRaises(ValidationError):
            PropertyOwnerLink.objects.create(property=self.property, client=contact)

    def test_team_property_rejects_private_contact_but_accepts_team_contact(self):
        agency = Agency.objects.create(owner=self.realtor, name="Агентство")
        AgencyMembership.objects.create(agency=agency, user=self.realtor, role="owner")
        AgencyMembership.objects.create(agency=agency, user=self.other, role="agent")
        property = Property.objects.create(owner=self.realtor, agency=agency, title="Общий объект")
        private = Client.objects.create(owner=self.realtor, name="Личный", phone="+79990000005")
        shared = Client.objects.create(owner=self.realtor, agency=agency, name="Общий", phone="+79990000006")
        self.client.post(reverse("property_owner_attach", args=[property.pk]), {"client": private.pk, "role": "owner"})
        self.assertFalse(PropertyOwnerLink.objects.filter(property=property).exists())
        self.client.force_login(self.other)
        self.client.post(reverse("property_owner_attach", args=[property.pk]), {"client": shared.pk, "role": "owner"})
        self.assertTrue(PropertyOwnerLink.objects.filter(property=property, client=shared).exists())
